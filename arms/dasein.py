"""dasein — thin client for the hosted Dasein compression service (ProxyArm + harness hooks).

The Dasein arm reaches a hosted compression service over the wire; this public repo contains no
vendor internals. It works across two seams:

  (1) PROXY seam — Claude Code points ANTHROPIC_BASE_URL at the service (DASEIN_BASE_URL); the
      service processes each turn server-side and forwards to the run's usage gateway (its
      configured upstream, DASEIN_UPSTREAM_BASE), relaying native Anthropic SSE. This module's
      ProxyArm surface (`model_base_url`/`headers`) routes there and forwards a stable per-run
      conversation id (CCB_RUN_ID) so the service keys one live session per run.

  (2) HARNESS-HOOK seam — two agent-loop-owned hooks a passive proxy cannot provide, realized via
      the Arm-interface hooks the runner wires into Claude Code:
        * `step0_injection(instance, repo_dir)` — returns an optional turn-0 brief string the
          runner appends to the system prompt at step 0;
        * `stop_decision(transcript_state)` — returns an optional verdict when the agent would
          stop: finalize (let the loop end) or continue-with-steering.

CLEAN-ROOM: this public repo must NOT import any vendor internals. The hooks SHELL OUT to the
service's hook-runner CLI (which lives in the vendor's private repo), passing JSON on stdin and
reading JSON on stdout. The runner command is `DASEIN_HOOK_CMD`; when it is unset or the runner
errors, the hooks fail OPEN (no brief / no verdict) so the arm degrades to the stock Claude Code
scaffold rather than crashing a paid run.

READINESS: ready() gates on the full configuration — the proxy seam (DASEIN_API_KEY +
DASEIN_BASE_URL), the hook seam (DASEIN_HOOK_CMD, with a live runner), and the native upstream
(DASEIN_UPSTREAM_BASE) — and pings the runner, mirroring the other arms' ready(): a missing or
half-installed runner SKIPs cleanly with a precise reason rather than silently running a partial
configuration.

Env:
  DASEIN_API_KEY       — bearer token for the hosted service.
  DASEIN_BASE_URL      — Anthropic-speaking base URL of the service (its upstream = the gateway).
  DASEIN_HOOK_CMD      — argv for the service's hook-runner CLI; enables the step0/stop hooks.
                         ready() gates on it + a live ping.
  DASEIN_UPSTREAM_BASE — the per-run usage-gateway URL the runner prints; the service forwards the
                         native Anthropic body here. ready() gates on it being present.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading

from bench.arm import ProxyArm, StopDecision, register


# Steering the runner's Stop hook feeds back to the agent when the stop verdict is
# continue-not-yet-submittable.
_CONTINUE_STEER = (
    "Your work is not yet complete: there is no submittable edit on disk that resolves the issue. "
    "Keep going — make the concrete code change that fixes the failing test, then stop.")


def _parse_hook_json(stdout: str) -> dict | None:
    """Extract the runner's JSON object from `stdout`, tolerating a stray banner prefix.

    The runner CLI emits JSON-only on stdout, but it can import libraries that print a banner
    BEFORE our redirect takes hold (e.g. mini-swe-agent's "👋 This is mini-swe-agent ..." line). So
    we don't assume the whole stream is JSON: try a straight parse first, else scan for a balanced
    top-level `{...}` object (the JSON we wrote). Returns the dict, or None if none is present."""
    s = (stdout or "").strip()
    if not s:
        return None
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    # Scan each top-level '{' in FORWARD order and return the FIRST that yields a balanced, parseable
    # object (the JSON we wrote is the last LINE but is itself a single top-level object — starting at
    # its OPENING brace, not an inner one, is what makes the whole object parse). String-aware so a
    # '}' inside a value (e.g. "c": "}") doesn't close the object early.
    for start in (i for i, ch in enumerate(s) if ch == "{"):
        depth, in_str, esc = 0, False, False
        for j in range(start, len(s)):
            ch = s[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(s[start:j + 1])
                        if isinstance(obj, dict):
                            return obj
                    except Exception:
                        pass  # this start's object isn't valid JSON; try the next top-level '{'
                    break  # consumed a balanced object from this start; move to the next start
    return None


@register("dasein")
class DaseinArm(ProxyArm):
    name = "dasein"
    # The proxy seam needs the service URL + key; the hook seam needs the runner CLI; the proxy's
    # native upstream needs the gateway base. ready() gates on all of them (see module docstring).
    needs = ["DASEIN_API_KEY", "DASEIN_BASE_URL", "DASEIN_HOOK_CMD", "DASEIN_UPSTREAM_BASE"]

    def __init__(self) -> None:
        # per-(instance) problem statement cache so stop_decision (which only gets cwd/session_id)
        # can pass the task to the runner. Keyed by repo_dir; thread-safe (the runner may fire
        # hooks from async callbacks).
        self._problem_by_dir: dict[str, str] = {}
        self._lock = threading.Lock()

    # ── readiness gate (the full configuration must be wired) ─────────────────
    def ready(self) -> tuple[bool, str]:
        """Ready iff the full arm can run: the proxy seam (DASEIN_API_KEY + DASEIN_BASE_URL), the
        hook seam (DASEIN_HOOK_CMD, with a LIVE runner), and the native upstream
        (DASEIN_UPSTREAM_BASE) are ALL provisioned.

        Why gate this hard (mirroring woz.ready()/rtk.ready()): with DASEIN_HOOK_CMD unset, the two
        harness hooks fail-open to None and silently vanish; with DASEIN_UPSTREAM_BASE unset, the
        proxy seam RuntimeErrors on the first /v1/messages request. Either way a faithful run is
        impossible, so we SKIP cleanly with a precise reason instead of measuring a partial
        configuration as the whole.

        The env presence is checked by super().ready() (all four are in `needs`); on top of that we
        PING the runner CLI (`<DASEIN_HOOK_CMD> ping`) and require it to report ready (`ok`), so a
        half-installed runner SKIPs here rather than degrading to a no-op for every task."""
        ok, reason = super().ready()        # all four `needs` env vars present & non-empty
        if not ok:
            return ok, reason
        argv = self._hook_cmd()
        if not argv:
            # DASEIN_HOOK_CMD is set (super().ready passed) but unparseable.
            return False, (
                f"DASEIN_HOOK_CMD is set but could not be parsed into an argv: "
                f"{os.environ.get('DASEIN_HOOK_CMD')!r}. It must be the runner CLI invocation.")
        ping = self._run_hook("ping", {})
        if ping is None:
            return False, (
                f"the hook-runner CLI did not respond to a `ping` ({' '.join(argv)} ping). The "
                f"agent-loop hooks cannot run, so this would silently degrade to a proxy-only run. "
                f"Deploy the hosted service's runner and point DASEIN_HOOK_CMD at it.")
        if not ping.get("ok"):
            return False, (
                f"the hook runner is reachable but reports not ready — the runner box is missing a "
                f"required component. Fix the runner install before running.")
        return True, f"ok (runner live: {' '.join(argv)})"

    # ── ProxyArm surface ──────────────────────────────────────────────────────
    def model_base_url(self) -> str:
        # Stripped of trailing slash so the Anthropic path join is clean.
        return os.environ.get("DASEIN_BASE_URL", "").rstrip("/")

    def headers(self) -> dict[str, str]:
        key = os.environ.get("DASEIN_API_KEY", "")
        # Send the key both ways (standard bearer + a vendor header) + a stable conversation id so
        # the service keys ONE live session per run, rather than falling back to the task-head hash.
        # CCB_RUN_ID (set per (instance,arm) by the runner) is stable for the whole solve, so it is
        # the natural conversation id.
        conv_id = os.environ.get("CCB_RUN_ID") or os.environ.get("DASEIN_CONV_ID") or ""
        h = {
            "Authorization": f"Bearer {key}",
            "X-Dasein-Api-Key": key,
        }
        if conv_id:
            h["X-Dasein-Conversation-Id"] = conv_id
        return h

    # ── harness hooks (agent-loop-owned: turn-0 brief + stop verdict) ─────────
    def step0_injection(self, instance: dict, repo_dir: str) -> str | None:
        """Return an optional turn-0 brief from the service's hook runner (`step0`).

        Shells out to the runner, which returns an optional brief string. Fail-open: no runner / any
        error -> None (stock scaffold)."""
        problem = (instance or {}).get("problem_statement") or ""
        with self._lock:
            self._problem_by_dir[repo_dir] = problem      # cache for stop_decision
        payload = {
            "problem_statement": problem,
            "repo_dir": repo_dir,
            # the repo slug (e.g. 'django/django') is passed through to the runner; SWE-bench
            # instances carry it. ac_config (optional) points the runner at its config yaml.
            "repo": (instance or {}).get("repo") or "",
            "instance_id": (instance or {}).get("instance_id") or "",
        }
        ac_config = os.environ.get("DASEIN_AC_CONFIG")
        if ac_config:
            payload["ac_config"] = ac_config
        out = self._run_hook("step0", payload)
        if not out:
            return None
        brief = out.get("brief") or ""
        return brief or None

    def stop_decision(self, transcript_state: dict) -> StopDecision | None:
        """Return the runner's stop verdict over the repo's on-disk state (`adjudicate`).

        FINALIZE -> StopDecision(finalize=True): there is a submittable edit that resolves the issue;
        let the loop end. CONTINUE -> keep going. Abstain (None) when the runner is unavailable or
        errors — never pin the loop."""
        cwd = (transcript_state or {}).get("cwd") or ""
        if not cwd:
            return None
        with self._lock:
            problem = self._problem_by_dir.get(cwd, "")
        tpath = (transcript_state or {}).get("transcript_path") or ""
        out = self._run_hook("adjudicate", {"problem_statement": problem, "repo_dir": cwd,
                                            "transcript_path": tpath})
        if not out:
            return None                                    # runner unavailable -> abstain
        verdict = (out.get("verdict") or "").upper()
        if verdict == "FINALIZE":
            return StopDecision(finalize=True)
        if verdict == "CONTINUE":
            return None  # CUT: don't inject 'keep going' — it can override a correct agent stop and grind to the cap. Trust the agent's stop; the runner's only lever is FINALIZE.
        return None                                        # ungrounded -> abstain (loop stops)

    # ── the subprocess bridge to the service runner (clean-room: no import) ────
    def _hook_cmd(self) -> list[str] | None:
        raw = os.environ.get("DASEIN_HOOK_CMD")
        if not raw:
            return None
        try:
            # posix=False on Windows so backslashes in a path (C:\...) aren't eaten as escapes;
            # the runner box is Linux (posix splitting) where DASEIN_HOOK_CMD uses forward slashes.
            return shlex.split(raw, posix=(os.name != "nt"))
        except Exception:
            return None

    def _run_hook(self, command: str, payload: dict) -> dict | None:
        """Invoke `<DASEIN_HOOK_CMD> <command>` with JSON on stdin; parse JSON stdout. Fail-open
        (None) on any error so a misconfigured runner never crashes the solve.

        The runner CLI guarantees stdout is JSON-only (it redirects library banners to stderr), but
        we parse DEFENSIVELY: it imports libraries that may print a banner to stdout (e.g.
        mini-swe-agent's "👋 ..." line), so we extract a balanced JSON object from stdout rather than
        assuming the whole stream is JSON. A stray prefix line therefore never fails the parse."""
        argv = self._hook_cmd()
        if not argv:
            return None
        try:
            proc = subprocess.run(
                argv + [command], input=json.dumps(payload), capture_output=True,
                text=True, timeout=float(os.environ.get("DASEIN_HOOK_TIMEOUT_S", "300")))
            if proc.returncode != 0 or not proc.stdout:
                return None
            return _parse_hook_json(proc.stdout)
        except Exception:
            return None
