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

Env:
  DASEIN_API_KEY   — bearer token for the hosted service (dsk_...).
  DASEIN_BASE_URL  — Anthropic-speaking base URL of the Dasein service (its upstream = the gateway).
  DASEIN_HOOK_CMD  — (optional) argv for the harness-runner CLI; enables scout/cold/adjudicator.
                     Unset -> proxy-only (curator/no-reread/governor still run server-side).
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
    needs = ["DASEIN_API_KEY", "DASEIN_BASE_URL"]

    def __init__(self) -> None:
        # per-(instance) problem statement cache so stop_decision (which only gets cwd/session_id)
        # can pass the task to the adjudicator runner. Keyed by repo_dir; thread-safe (the runner
        # may fire hooks from async callbacks).
        self._problem_by_dir: dict[str, str] = {}
        self._lock = threading.Lock()

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
