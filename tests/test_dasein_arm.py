"""Unit tests for the dasein arm's v9 A3S wiring (ProxyArm surface + the two harness hooks).

The dasein arm runs v9 A3S across two seams: a PROXY seam (server-side curator/no-reread/governor,
reached by pointing ANTHROPIC_BASE_URL at the service) and a HARNESS-HOOK seam (the agent-loop-owned
scout/cold-retrieval at step 0 and the SUBMIT adjudicator's FINALIZE/CONTINUE loop control). The
hook seam SHELLS OUT to the private dasein-compression-service's `service.harness_runners` CLI
(clean-room: this repo never imports adaptive_context). These tests prove:

  * the proxy surface points at DASEIN_BASE_URL and forwards a stable conversation id;
  * step0_injection runs the runner and returns its brief (and caches the problem for the adjudicator);
  * stop_decision maps the runner's FINALIZE -> end-loop, CONTINUE -> keep-going-with-steering;
  * BOTH hooks fail OPEN (no runner / a broken runner -> None) so a paid run never crashes;
  * the arm NEVER imports adaptive_context (the clean-room invariant).

The runner is faked with a tiny python script invoked exactly as the real CLI is (argv + JSON stdin
-> JSON stdout), so we exercise the REAL subprocess bridge in arms/dasein.py end-to-end.

Runnable two ways:
    py -m pytest tests/test_dasein_arm.py -q
    py tests/test_dasein_arm.py            # standalone, prints PASS/FAIL
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _fake_runner(tmpdir: Path, *, step0_brief: str = "SCOUT BRIEF: edit src/foo.py",
                 verdict: str = "CONTINUE", broken: bool = False) -> str:
    """Write a fake harness-runner CLI and return a DASEIN_HOOK_CMD argv string for it.

    The fake mimics `service.harness_runners`: argv[1] is the command (step0|adjudicate), JSON on
    stdin, JSON on stdout. `broken=True` exits non-zero (to exercise the fail-open path)."""
    script = tmpdir / "fake_runner.py"
    body = (
        "import sys, json\n"
        f"BROKEN = {broken!r}\n"
        f"BRIEF = {step0_brief!r}\n"
        f"VERDICT = {verdict!r}\n"
        "if BROKEN:\n"
        "    sys.exit(3)\n"
        "cmd = sys.argv[1] if len(sys.argv) > 1 else ''\n"
        "payload = json.loads(sys.stdin.read() or '{}')\n"
        "if cmd == 'step0':\n"
        "    out = {'brief': BRIEF, 'stats': {'reported': True}, 'source': 'scout',\n"
        "           'echo_repo': payload.get('repo_dir')}\n"
        "elif cmd == 'adjudicate':\n"
        "    out = {'verdict': VERDICT, 'reason': 'test', 'has_edit': True,\n"
        "           'echo_problem': payload.get('problem_statement')}\n"
        "else:\n"
        "    out = {'error': 'unknown'}\n"
        "sys.stdout.write(json.dumps(out))\n"
    )
    script.write_text(body, encoding="utf-8")
    return f"{sys.executable} {script}"


def _arm():
    import arms  # noqa: F401 — registers every arm on import
    from bench.arm import get_arm
    return get_arm("dasein")


# ── proxy surface ─────────────────────────────────────────────────────────────
def test_proxy_base_url_and_conv_id_header():
    os.environ["DASEIN_BASE_URL"] = "https://dasein.example/"
    os.environ["DASEIN_API_KEY"] = "dsk_test"
    os.environ["CCB_RUN_ID"] = "run-xyz"
    arm = _arm()
    assert arm.model_base_url() == "https://dasein.example"  # trailing slash stripped
    h = arm.headers()
    assert h["Authorization"] == "Bearer dsk_test"
    assert h["X-Dasein-Api-Key"] == "dsk_test"
    assert h["X-Dasein-Conversation-Id"] == "run-xyz"        # stable conv id forwarded


def test_kind_and_declared_hooks():
    from bench.arm import Arm
    arm = _arm()
    assert arm.kind.value == "proxy"
    assert arm.has_harness_hooks() is True
    assert getattr(type(arm), "step0_injection") is not getattr(Arm, "step0_injection")
    assert getattr(type(arm), "stop_decision") is not getattr(Arm, "stop_decision")
    # the arm is NOT a pre-tool rewrite arm (that's rtk) — must stay the base no-op
    assert getattr(type(arm), "pre_tool_hook") is getattr(Arm, "pre_tool_hook")


# ── step0_injection (scout/cold via the runner) ───────────────────────────────
def test_step0_injection_runs_runner(tmp_path):
    os.environ["DASEIN_HOOK_CMD"] = _fake_runner(tmp_path, step0_brief="SCOUT: foo.py is the spot")
    arm = _arm()
    brief = arm.step0_injection({"problem_statement": "fix the bug"}, str(tmp_path / "repo"))
    assert brief == "SCOUT: foo.py is the spot"


def test_step0_injection_failopen_without_runner(tmp_path):
    os.environ.pop("DASEIN_HOOK_CMD", None)
    arm = _arm()
    assert arm.step0_injection({"problem_statement": "x"}, str(tmp_path)) is None


def test_step0_injection_failopen_on_broken_runner(tmp_path):
    os.environ["DASEIN_HOOK_CMD"] = _fake_runner(tmp_path, broken=True)
    arm = _arm()
    assert arm.step0_injection({"problem_statement": "x"}, str(tmp_path)) is None


# ── stop_decision (SUBMIT adjudicator via the runner) ─────────────────────────
def test_stop_decision_finalize_ends_loop(tmp_path):
    os.environ["DASEIN_HOOK_CMD"] = _fake_runner(tmp_path, verdict="FINALIZE")
    arm = _arm()
    cwd = str(tmp_path / "repo")
    # the arm caches the problem from step0 so the adjudicator gets it; prime it.
    arm.step0_injection({"problem_statement": "fix it"}, cwd)
    dec = arm.stop_decision({"cwd": cwd, "session_id": "s1", "stop_hook_active": False})
    assert dec is not None and dec.finalize is True


def test_stop_decision_continue_blocks_with_steering(tmp_path):
    os.environ["DASEIN_HOOK_CMD"] = _fake_runner(tmp_path, verdict="CONTINUE")
    arm = _arm()
    cwd = str(tmp_path / "repo")
    dec = arm.stop_decision({"cwd": cwd, "session_id": "s1", "stop_hook_active": False})
    assert dec is not None and dec.finalize is False
    assert dec.directive and "Keep going" in dec.directive


def test_stop_decision_failopen_without_runner(tmp_path):
    os.environ.pop("DASEIN_HOOK_CMD", None)
    arm = _arm()
    assert arm.stop_decision({"cwd": str(tmp_path), "session_id": "s"}) is None


def test_stop_decision_abstains_without_cwd(tmp_path):
    os.environ["DASEIN_HOOK_CMD"] = _fake_runner(tmp_path, verdict="FINALIZE")
    arm = _arm()
    assert arm.stop_decision({"session_id": "s"}) is None    # no cwd -> abstain


# ── clean-room invariant ──────────────────────────────────────────────────────
def test_arm_module_does_not_import_adaptive_context():
    """The arm may NAME adaptive_context in prose (it explains the clean-room rule) but must never
    IMPORT it. Parse the AST and assert no import statement references the package."""
    import ast
    src = (_ROOT / "arms" / "dasein.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                assert not n.name.startswith("adaptive_context"), \
                    "clean-room: arms/dasein.py must NOT import adaptive_context"
        elif isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("adaptive_context"), \
                "clean-room: arms/dasein.py must NOT import adaptive_context"


# ── standalone runner ─────────────────────────────────────────────────────────
def _run_standalone() -> int:
    import tempfile
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failures = 0
    for name, fn in tests:
        try:
            import inspect
            if "tmp_path" in inspect.signature(fn).parameters:
                with tempfile.TemporaryDirectory() as d:
                    fn(Path(d))
            else:
                fn()
            print(f"PASS  {name}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL  {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
