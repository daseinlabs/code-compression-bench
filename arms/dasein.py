"""dasein — the hosted v9 A3S product (ProxyArm + harness hooks).

Dasein's product is **v9 A3S**: a server-side compression pipeline (scout turn-0 brief ->
cold-retrieval step-0 TOC -> curator every turn -> no-reread -> governor + SUBMIT adjudicator)
that runs the REAL `adaptive_context` stack. Under the Claude Code harness the pipeline splits
across two seams:

  (1) PROXY seam (server-side, always-on) — Claude Code points ANTHROPIC_BASE_URL at the Dasein
      service; the service curates/governs the prompt each turn and forwards to the run gateway
      (its configured upstream), relaying native Anthropic SSE. This module's ProxyArm surface
      (`model_base_url`/`headers`) routes there, and forwards a stable per-(instance,arm)
      conversation id so the service holds ONE live curator/governor/adjudicator per run.

  (2) HARNESS-HOOK seam (agent-loop-owned) — scout, cold-retrieval and the SUBMIT
      adjudicator's FINALIZE/CONTINUE loop-control are NOT things a passive proxy can do (an
      agentic walk; owning the loop's stop). They are realized via the Arm-interface harness
      hooks the runner wires into Claude Code:
        * `step0_injection(instance, repo_dir)` — runs the REAL v9 scout (haiku-4-5, budget 12)
          over the repo, else the cold-retrieval TOC, and returns the turn-0 brief the runner
          appends to Claude Code's system prompt at step 0;
        * `stop_decision(transcript_state)` — runs the REAL AdjudicatorSubmit over the repo's
          on-disk git diff each time the agent would stop, returning FINALIZE (let it end) or
          CONTINUE-with-steering (the runner's Stop hook blocks the stop and feeds the steering
          back). This is how the harness-owned loop gets the submit-adjudicator's verdict.

CLEAN-ROOM: this public repo must NOT import `adaptive_context`. The scout/cold-retrieval/
adjudicator are the REAL product, so the hooks SHELL OUT to the dasein-compression-service's
`service.harness_runners` CLI (which lives in the private repo and imports the real product),
passing JSON on stdin and reading JSON on stdout. The runner command is `DASEIN_HOOK_CMD`
(e.g. `/srv/dasein/.venv/bin/python -m service.harness_runners`). When it's unset or the runner
errors, the hooks fail OPEN — no brief / a CONTINUE that never pins the loop — so the arm degrades
to the stock Claude Code scaffold rather than crashing a paid run.

FAITHFULNESS GATE (why ready() requires the hooks, not just the proxy)
---------------------------------------------------------------------
v9 A3S has SIX stages across the two seams: (proxy) curator, no-reread, governor; (agent-loop)
scout, cold-retrieval, the SUBMIT adjudicator's loop control. Three of those six live ONLY in the
harness-hook seam. If ``DASEIN_HOOK_CMD`` is unset, ``step0_injection`` and ``stop_decision`` both
fail-open to ``None`` — so the scout brief, the cold-retrieval TOC, AND the FINALIZE/CONTINUE loop
control all vanish with NO error, and the run would still be LABELED "dasein v9 A3S" while measuring
only half the product. Likewise, if ``DASEIN_UPSTREAM_BASE`` is unprovisioned, the always-on proxy
seam itself ``RuntimeError``s on the first ``/v1/messages`` request (it has no gateway to forward to).
So ``ready()`` GATES on BOTH (and pings the runner CLI), mirroring ``woz.ready()``/``rtk.ready()``:
a missing hook runner / unprovisioned upstream SKIPs cleanly with a precise, actionable reason —
never schedules a proxy-only run mislabeled as the full v9 product.

Env:
  DASEIN_API_KEY      — bearer token for the hosted service (dsk_...).
  DASEIN_BASE_URL     — Anthropic-speaking base URL of the Dasein service (its upstream = the gateway).
  DASEIN_HOOK_CMD     — argv for the harness-runner CLI; enables scout/cold/adjudicator (the agent-loop
                        half of v9). REQUIRED for a faithful v9 run — ready() gates on it + a live ping.
  DASEIN_UPSTREAM_BASE — the per-run usage-gateway URL the runner prints; the service forwards the
                        native Anthropic body here. REQUIRED — the proxy seam RuntimeErrors without it.
                        ready() gates on it being present (its value is the gateway, known at run time;
                        the gate only checks the operator wired the provisioning step).
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading

from bench.arm import ProxyArm, StopDecision, register


# CONTINUE steering the runner's Stop hook feeds back to the agent when the SUBMIT adjudicator
# says the work is not yet submittable (mirrors AdjudicatorSubmit's CONTINUE semantics).
_CONTINUE_STEER = (
    "Your work is not yet complete: there is no submittable edit on disk that resolves the issue. "
    "Keep going — make the concrete code change that fixes the failing test, then stop.")


@register("dasein")
class DaseinArm(ProxyArm):
    name = "dasein"
    # The proxy seam needs the service URL + key; the agent-loop seam (scout/cold/adjudicator) needs
    # the runner CLI; the proxy's native upstream needs the gateway base. ready() gates on ALL of
    # them so a proxy-only run is never mislabeled as the full v9 product (see module docstring).
    needs = ["DASEIN_API_KEY", "DASEIN_BASE_URL", "DASEIN_HOOK_CMD", "DASEIN_UPSTREAM_BASE"]

    def __init__(self) -> None:
        # per-(instance) problem statement cache so stop_decision (which only gets cwd/session_id)
        # can pass the task to the adjudicator runner. Keyed by repo_dir; thread-safe (the runner
        # may fire hooks from async callbacks).
        self._problem_by_dir: dict[str, str] = {}
        self._lock = threading.Lock()

    # ── readiness gate (faithful v9 requires BOTH seams wired) ────────────────
    def ready(self) -> tuple[bool, str]:
        """Ready iff the FULL v9 A3S can run: the proxy seam (DASEIN_API_KEY + DASEIN_BASE_URL),
        the agent-loop seam (DASEIN_HOOK_CMD, with a LIVE runner that imports the real scout / cold /
        submit-adjudicator), and the native upstream (DASEIN_UPSTREAM_BASE) are ALL provisioned.

        Why gate this hard (mirroring woz.ready()/rtk.ready()): with DASEIN_HOOK_CMD unset, the two
        harness hooks fail-open to None — the scout brief, cold-retrieval TOC, and FINALIZE/CONTINUE
        loop control silently vanish, yet the run is still labeled v9; with DASEIN_UPSTREAM_BASE
        unset, the always-on proxy seam RuntimeErrors on the first /v1/messages request. Either way
        a faithful v9 run is impossible, so we SKIP cleanly with a precise reason instead of
        measuring a fraction of the product as the whole.

        The env presence is checked by super().ready() (DASEIN_HOOK_CMD/DASEIN_UPSTREAM_BASE are in
        `needs`); on top of that we PING the runner CLI (`<DASEIN_HOOK_CMD> ping`) and require it to
        report the submit-adjudicator importable (`ok`), so a half-installed runner — missing
        adaptive_context / the dasein pkg on the runner box — SKIPs here rather than dying as an
        empty-brief / always-CONTINUE no-op for every task."""
        ok, reason = super().ready()        # all four `needs` env vars present & non-empty
        if not ok:
            return ok, reason
        argv = self._hook_cmd()
        if not argv:
            # DASEIN_HOOK_CMD is set (super().ready passed) but unparseable.
            return False, (
                f"DASEIN_HOOK_CMD is set but could not be parsed into an argv: "
                f"{os.environ.get('DASEIN_HOOK_CMD')!r}. It must be the runner CLI invocation, e.g. "
                f"'/srv/dasein/.venv/bin/python -m service.harness_runners'.")
        ping = self._run_hook("ping", {})
        if ping is None:
            return False, (
                f"the v9 harness-runner CLI did not respond to a `ping` "
                f"({' '.join(argv)} ping). The agent-loop half of v9 (scout turn-0 brief, "
                f"cold-retrieval TOC, SUBMIT adjudicator loop control) cannot run, so this would "
                f"silently degrade to a proxy-only run mislabeled as v9. Deploy the "
                f"dasein-compression-service (adaptive_context + the dasein pkg) on the runner box "
                f"and point DASEIN_HOOK_CMD at its venv python -m service.harness_runners.")
        if not ping.get("ok"):
            stages = ping.get("stages") or {}
            missing = [k for k in ("scout", "cold", "adjudicate") if not stages.get(k)]
            return False, (
                f"the v9 harness runner is reachable but a required stage is not importable "
                f"(missing: {', '.join(missing) or 'adjudicate'}). The runner box is missing part of "
                f"the real product (adaptive_context.meta.adjudicator_submit / optimizer.scout / "
                f"candidates.repo_index2 / the dasein pkg). Fix the runner venv before running — a "
                f"v9 run without the SUBMIT adjudicator is not the product.")
        stages = ping.get("stages") or {}
        warn = ""
        if not stages.get("scout"):
            warn += " WARN scout not importable (cold-retrieval fallback only);"
        if not stages.get("cold"):
            warn += " WARN cold-retrieval not importable;"
        return True, f"ok (v9 runner live: {' '.join(argv)}){warn}"

    # ── ProxyArm surface ──────────────────────────────────────────────────────
    def model_base_url(self) -> str:
        # Stripped of trailing slash so the Anthropic path join is clean.
        return os.environ.get("DASEIN_BASE_URL", "").rstrip("/")

    def headers(self) -> dict[str, str]:
        key = os.environ.get("DASEIN_API_KEY", "")
        # Send the key both ways (standard bearer + a vendor header) + a stable conversation id so
        # the service keys ONE live A3S triple per run (curator/governor/adjudicator), rather than
        # falling back to the task-head hash. CCB_RUN_ID (set per (instance,arm) by the runner) is
        # stable for the whole solve, so it is the natural conversation id (audit gap: statefulness).
        conv_id = os.environ.get("CCB_RUN_ID") or os.environ.get("DASEIN_CONV_ID") or ""
        h = {
            "Authorization": f"Bearer {key}",
            "X-Dasein-Api-Key": key,
        }
        if conv_id:
            h["X-Dasein-Conversation-Id"] = conv_id
        return h

    # ── harness hooks (agent-loop-owned v9 A3S: scout/cold + submit adjudicator) ──
    def step0_injection(self, instance: dict, repo_dir: str) -> str | None:
        """Run the REAL v9 scout (else cold-retrieval) and return the turn-0 brief.

        Shells out to `service.harness_runners step0` (the real product). The runner does the
        isolated haiku walk over the repo's own tools and returns the blast-radius pack + framing
        (or the cold-retrieval TOC). Fail-open: no runner / any error -> None (stock scaffold)."""
        problem = (instance or {}).get("problem_statement") or ""
        with self._lock:
            self._problem_by_dir[repo_dir] = problem      # cache for stop_decision
        payload = {
            "problem_statement": problem,
            "repo_dir": repo_dir,
            # the repo SLUG (e.g. 'django/django') drives cold-retrieval's hosted-index query;
            # SWE-bench instances carry it. ac_config (optional) points the runner at the cfg yaml.
            "repo": (instance or {}).get("repo") or "",
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
        """Run the REAL SUBMIT adjudicator over the repo's on-disk diff and return the verdict.

        FINALIZE -> StopDecision(finalize=True): the agent has a submittable edit that resolves the
        issue; let the loop end. CONTINUE -> StopDecision(finalize=False, directive=...): nothing
        submittable yet; the runner blocks the stop and steers the agent to keep going. Abstain
        (None) when the runner is unavailable/errors — never pin the loop."""
        cwd = (transcript_state or {}).get("cwd") or ""
        if not cwd:
            return None
        with self._lock:
            problem = self._problem_by_dir.get(cwd, "")
        out = self._run_hook("adjudicate", {"problem_statement": problem, "repo_dir": cwd})
        if not out:
            return None                                    # runner unavailable -> abstain
        verdict = (out.get("verdict") or "").upper()
        if verdict == "FINALIZE":
            return StopDecision(finalize=True)
        if verdict == "CONTINUE":
            return StopDecision(finalize=False, directive=_CONTINUE_STEER)
        return None                                        # ungrounded -> abstain (loop stops)

    # ── the subprocess bridge to the real product (clean-room: no import) ─────
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
        (None) on any error so a misconfigured runner never crashes the solve."""
        argv = self._hook_cmd()
        if not argv:
            return None
        try:
            proc = subprocess.run(
                argv + [command], input=json.dumps(payload), capture_output=True,
                text=True, timeout=float(os.environ.get("DASEIN_HOOK_TIMEOUT_S", "300")))
            if proc.returncode != 0 or not proc.stdout:
                return None
            return json.loads(proc.stdout)
        except Exception:
            return None
