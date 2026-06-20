"""Unit tests for the OPTIONAL harness-level Arm hooks + their cc_runner wiring.

An arm may declare three behaviours the Claude Agent SDK supports — a step-0
system-prompt injection, a PreToolUse rewrite, and a Stop loop-decision — WITHOUT
editing cc_runner's core. These tests prove the contract the Dasein + RTK fix
agents rely on:

  * an arm with ``pre_tool_hook`` REWRITES a Bash call (git status -> rtk git status);
  * an arm with ``step0_injection`` PREPENDS its brief to the system prompt;
  * an arm with ``stop_decision`` can END the loop (and can CONTINUE it w/ steering);
  * an arm that overrides NONE of them leaves the SDK options byte-for-byte as before
    (baseline / proxy / woz are unaffected).

The Claude Agent SDK is NOT installed on the dev box (cc_runner imports it lazily),
so we stub a MINIMAL ``claude_agent_sdk`` module: a ``ClaudeAgentOptions`` that just
captures its kwargs, a ``HookMatcher`` dataclass matching the real shape, and an
async ``query`` that simulates the SDK loop — it fires the registered PreToolUse
hook on a Bash tool call (applying ``updatedInput`` exactly as the CLI would) and
then the Stop hook to decide whether to continue. This exercises the REAL wiring in
``bench.cc_runner._build_harness_hooks`` and ``_run_sdk`` end-to-end at the SDK seam.

Runnable two ways:
    py -m pytest tests/test_harness_hooks.py -q
    py tests/test_harness_hooks.py            # standalone, prints PASS/FAIL
"""

from __future__ import annotations

import asyncio
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# make the repo root importable when run as a bare script (py tests/...).
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ── stub the claude_agent_sdk the lazy imports reach for ─────────────────────
def _install_sdk_stub() -> dict:
    """Install a minimal fake ``claude_agent_sdk`` and return a capture dict.

    The capture dict records the last ClaudeAgentOptions built and a scripted
    transcript of what the simulated SDK did (the rewritten tool input, the stop
    verdict), so a test can assert the hooks fired correctly. Idempotent — a fresh
    stub is installed each call so tests don't bleed state.
    """
    capture: dict = {"options": None, "events": []}

    @dataclass
    class HookMatcher:
        matcher: Optional[str] = None
        hooks: list = field(default_factory=list)
        timeout: Optional[float] = None

    class ClaudeAgentOptions:
        # Mirror the real kwargs cc_runner passes; just stash them for assertions.
        def __init__(self, **kw):
            self.__dict__.update(kw)
            capture["options"] = self

    @dataclass
    class ResultMessage:
        subtype: str = "success"
        is_error: bool = False
        num_turns: int = 1
        total_cost_usd: float = 0.0
        usage: dict = field(default_factory=dict)
        result: str = ""

    @dataclass
    class AssistantMessage:
        content: list = field(default_factory=list)

    async def query(*, prompt, options):
        """Simulate the SDK loop: run the registered PreToolUse + Stop hooks.

        1. The agent wants to run a Bash tool. Fire every PreToolUse matcher's
           hooks with the tool name + input; honour ``updatedInput`` exactly as
           the CLI does (replace the tool_input that runs).
        2. The agent then tries to STOP. Fire every Stop hook; if any returns
           ``decision == "block"`` the loop CONTINUES (we record the steering and
           do NOT yield the terminal result on the first pass). On the second
           pass ``stop_hook_active`` is True so the loop is allowed to end.
        """
        hooks = getattr(options, "hooks", None) or {}

        # (1) PreToolUse on a Bash 'git status' call.
        pre = hooks.get("PreToolUse") or []
        tool_input = {"command": "git status"}
        for matcher in pre:
            for cb in matcher.hooks:
                out = await cb(
                    {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                     "tool_input": dict(tool_input)},
                    "tool-use-1",
                    {"signal": None},
                )
                spec = (out or {}).get("hookSpecificOutput", {})
                if "updatedInput" in spec:
                    tool_input = spec["updatedInput"]
        capture["events"].append(("tool_input", tool_input))
        yield AssistantMessage(content=[])

        # (2) Stop: consult the Stop hooks; honour decision=block (continue once).
        stop = hooks.get("Stop") or []
        continued = False
        stop_active = False
        for _pass in range(2):
            blocked = False
            for matcher in stop:
                for cb in matcher.hooks:
                    out = await cb(
                        {"hook_event_name": "Stop", "stop_hook_active": stop_active,
                         "session_id": "sess-1", "cwd": "/tmp"},
                        None, {"signal": None},
                    )
                    if (out or {}).get("decision") == "block":
                        blocked = True
                        capture["events"].append(("stop_block", (out or {}).get("reason")))
            if blocked:
                continued = True
                stop_active = True  # next pass is a forced continuation
                continue
            break
        capture["events"].append(("continued", continued))
        yield ResultMessage()

    mod = types.ModuleType("claude_agent_sdk")
    mod.HookMatcher = HookMatcher
    mod.ClaudeAgentOptions = ClaudeAgentOptions
    mod.ResultMessage = ResultMessage
    mod.AssistantMessage = AssistantMessage
    mod.query = query
    sys.modules["claude_agent_sdk"] = mod
    return capture


# ── arms under test (each overrides exactly one optional hook) ───────────────
def _arms():
    """Build the test arms fresh against the current bench.arm module."""
    from bench.arm import Arm, ArmKind, StopDecision

    class RewriteArm(Arm):
        name = "rewrite_test"
        kind = ArmKind.BASELINE

        def pre_tool_hook(self, tool_name, tool_input):
            # RTK-style: wrap a git command in the compressed `rtk` runner.
            if tool_name == "Bash":
                cmd = (tool_input or {}).get("command", "")
                if cmd.startswith("git "):
                    new = dict(tool_input)
                    new["command"] = "rtk " + cmd
                    return {"tool_input": new}
            return None

    class Step0Arm(Arm):
        name = "step0_test"
        kind = ArmKind.BASELINE
        BRIEF = "SCOUT BRIEF: the fix lives in src/foo.py; TOC: [a, b, c]"

        def step0_injection(self, instance, repo_dir):
            return self.BRIEF

    class FinalizeArm(Arm):
        name = "finalize_test"
        kind = ArmKind.BASELINE

        def stop_decision(self, transcript_state):
            return StopDecision(finalize=True)

    class ContinueArm(Arm):
        name = "continue_test"
        kind = ArmKind.BASELINE
        DIRECTIVE = "Your diff is incomplete — keep going."

        def stop_decision(self, transcript_state):
            return StopDecision(finalize=False, directive=self.DIRECTIVE)

    return RewriteArm, Step0Arm, FinalizeArm, ContinueArm


# ─────────────────────────────── tests ──────────────────────────────────────
def test_base_arm_hooks_are_noops():
    """The base Arm (and the baseline control) override NO optional hook."""
    from bench.arm import BaselineArm
    b = BaselineArm()
    assert b.step0_injection({}, "/tmp") is None
    assert b.pre_tool_hook("Bash", {"command": "ls"}) is None
    assert b.stop_decision({"stop_hook_active": False}) is None
    assert b.has_harness_hooks() is False


def test_has_harness_hooks_detects_each_override():
    """has_harness_hooks() is True iff an arm overrides ANY optional hook."""
    RewriteArm, Step0Arm, FinalizeArm, _ = _arms()
    assert RewriteArm().has_harness_hooks() is True
    assert Step0Arm().has_harness_hooks() is True
    assert FinalizeArm().has_harness_hooks() is True


def test_build_harness_hooks_noop_for_baseline():
    """An arm that overrides nothing leaves arm_cfg's hook wiring empty."""
    _install_sdk_stub()
    from bench.arm import BaselineArm
    from bench.cc_runner import _ArmConfig, _build_harness_hooks
    cfg = _ArmConfig()
    _build_harness_hooks(BaselineArm(), cfg, {"instance_id": "x"}, "/tmp")
    assert cfg.system_prompt_append is None
    assert cfg.sdk_hooks is None


def test_pre_tool_hook_rewrites_bash_call():
    """An arm with pre_tool_hook REWRITES Bash 'git status' -> 'rtk git status'."""
    capture = _install_sdk_stub()
    RewriteArm, *_ = _arms()
    from bench.cc_runner import _ArmConfig, _build_harness_hooks
    cfg = _ArmConfig()
    _build_harness_hooks(RewriteArm(), cfg, {}, "/tmp")
    assert cfg.sdk_hooks is not None and "PreToolUse" in cfg.sdk_hooks

    # Drive the seam: the stub query fires the PreToolUse hook on a Bash call.
    out = _run_sdk_via_stub(cfg)
    tool_inputs = [v for k, v in capture["events"] if k == "tool_input"]
    assert tool_inputs and tool_inputs[-1]["command"] == "rtk git status", tool_inputs
    assert out is not None  # the run completed (terminal ResultMessage seen)


def test_step0_injection_prepends_to_system_prompt():
    """An arm with step0_injection appends its brief to the claude_code preset."""
    capture = _install_sdk_stub()
    _, Step0Arm, *_ = _arms()
    from bench.cc_runner import _ArmConfig, _build_harness_hooks
    cfg = _ArmConfig()
    _build_harness_hooks(Step0Arm(), cfg, {}, "/tmp")
    assert cfg.system_prompt_append == Step0Arm.BRIEF

    _run_sdk_via_stub(cfg)
    opts = capture["options"]
    sp = opts.system_prompt
    assert isinstance(sp, dict), sp
    assert sp["type"] == "preset" and sp["preset"] == "claude_code"
    assert sp["append"] == Step0Arm.BRIEF


def test_stop_decision_finalize_ends_loop():
    """stop_decision(finalize=True) lets the loop END — no continuation."""
    capture = _install_sdk_stub()
    _, _, FinalizeArm, _ = _arms()
    from bench.cc_runner import _ArmConfig, _build_harness_hooks
    cfg = _ArmConfig()
    _build_harness_hooks(FinalizeArm(), cfg, {}, "/tmp")
    assert cfg.sdk_hooks is not None and "Stop" in cfg.sdk_hooks

    _run_sdk_via_stub(cfg)
    continued = [v for k, v in capture["events"] if k == "continued"][-1]
    assert continued is False, "finalize=True must NOT continue the loop"
    assert not [v for k, v in capture["events"] if k == "stop_block"]


def test_stop_decision_continue_blocks_with_directive():
    """stop_decision(finalize=False, directive=...) CONTINUES with steering."""
    capture = _install_sdk_stub()
    _, _, _, ContinueArm = _arms()
    from bench.cc_runner import _ArmConfig, _build_harness_hooks
    cfg = _ArmConfig()
    _build_harness_hooks(ContinueArm(), cfg, {}, "/tmp")

    _run_sdk_via_stub(cfg)
    continued = [v for k, v in capture["events"] if k == "continued"][-1]
    assert continued is True, "finalize=False must CONTINUE the loop"
    reasons = [v for k, v in capture["events"] if k == "stop_block"]
    assert ContinueArm.DIRECTIVE in reasons, reasons


def test_include_hook_events_set_when_arm_declares_hooks():
    """include_hook_events flips on when an arm contributes harness hooks."""
    capture = _install_sdk_stub()
    RewriteArm, *_ = _arms()
    from bench.cc_runner import _ArmConfig, _build_harness_hooks
    cfg = _ArmConfig()
    _build_harness_hooks(RewriteArm(), cfg, {}, "/tmp")
    _run_sdk_via_stub(cfg)
    assert capture["options"].include_hook_events is True


# ── helper: run _run_sdk against the stubbed SDK ─────────────────────────────
def _run_sdk_via_stub(cfg):
    """Invoke bench.cc_runner._run_sdk with the SDK-stub installed.

    Supplies the minimal kwargs _run_sdk needs; the stubbed ``query`` drives the
    PreToolUse + Stop hooks and yields a terminal ResultMessage so _run_sdk
    returns normally.
    """
    from bench.cc_runner import _run_sdk
    return asyncio.run(_run_sdk(
        prompt="solve it",
        cwd="/tmp",
        model="claude-sonnet-4-5",
        arm_cfg=cfg,
        client_base_url="http://127.0.0.1:9/",
        run_id="rid-test",
        call_cap=10,
        env_overrides={},
    ))


# ── standalone runner (py tests/test_harness_hooks.py) ───────────────────────
def _run_standalone() -> int:
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL  {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
