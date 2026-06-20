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
                 verdict: str = "CONTINUE", broken: bool = False, ping_ok: bool = True,
                 name: str = "fake_runner.py") -> str:
    """Write a fake harness-runner CLI and return a DASEIN_HOOK_CMD argv string for it.

    The fake mimics `service.harness_runners`: argv[1] is the command (ping|step0|adjudicate), JSON
    on stdin, JSON on stdout. `broken=True` exits non-zero (to exercise the fail-open path);
    `ping_ok=False` makes the `ping` command report the submit-adjudicator missing (to exercise the
    ready() readiness gate on a half-installed runner)."""
    script = tmpdir / name
    body = (
        "import sys, json\n"
        f"BROKEN = {broken!r}\n"
        f"BRIEF = {step0_brief!r}\n"
        f"VERDICT = {verdict!r}\n"
        f"PING_OK = {ping_ok!r}\n"
        "if BROKEN:\n"
        "    sys.exit(3)\n"
        "cmd = sys.argv[1] if len(sys.argv) > 1 else ''\n"
        "payload = json.loads(sys.stdin.read() or '{}')\n"
        "if cmd == 'ping':\n"
        "    out = {'ok': PING_OK, 'scout': PING_OK, 'cold': PING_OK, 'adjudicate': PING_OK,\n"
        "           'stages': {'scout': PING_OK, 'cold': PING_OK, 'adjudicate': PING_OK}}\n"
        "elif cmd == 'step0':\n"
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


def _full_env(tmp_path: Path, **kw) -> None:
    """Set the full v9 env (proxy + hook + upstream) so ready() can pass; tests tweak from here."""
    os.environ["DASEIN_API_KEY"] = "dsk_test"
    os.environ["DASEIN_BASE_URL"] = "https://dasein.example/"
    os.environ["DASEIN_UPSTREAM_BASE"] = "http://127.0.0.1:9999"
    os.environ["DASEIN_HOOK_CMD"] = _fake_runner(tmp_path, **kw)


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


# ── ready() faithfulness gate (BOTH seams must be wired, mirroring woz/rtk) ────
def _clear_dasein_env():
    for k in ("DASEIN_API_KEY", "DASEIN_BASE_URL", "DASEIN_HOOK_CMD", "DASEIN_UPSTREAM_BASE",
              "CCB_RUN_ID", "DASEIN_CONV_ID"):
        os.environ.pop(k, None)


def test_ready_ok_with_full_v9_wiring(tmp_path):
    _clear_dasein_env()
    _full_env(tmp_path)                       # proxy + live runner ping + upstream base
    ok, reason = _arm().ready()
    assert ok is True, reason
    assert "v9 runner live" in reason


def test_ready_skips_without_hook_cmd(tmp_path):
    """DASEIN_HOOK_CMD unset -> scout/cold/adjudicator vanish -> SKIP (never a proxy-only 'v9')."""
    _clear_dasein_env()
    os.environ["DASEIN_API_KEY"] = "dsk_test"
    os.environ["DASEIN_BASE_URL"] = "https://dasein.example/"
    os.environ["DASEIN_UPSTREAM_BASE"] = "http://127.0.0.1:9999"
    # DASEIN_HOOK_CMD deliberately unset
    ok, reason = _arm().ready()
    assert ok is False
    assert "DASEIN_HOOK_CMD" in reason


def test_ready_skips_without_upstream_base(tmp_path):
    """DASEIN_UPSTREAM_BASE unset -> the proxy seam RuntimeErrors on first /v1/messages -> SKIP."""
    _clear_dasein_env()
    os.environ["DASEIN_API_KEY"] = "dsk_test"
    os.environ["DASEIN_BASE_URL"] = "https://dasein.example/"
    os.environ["DASEIN_HOOK_CMD"] = _fake_runner(tmp_path)
    # DASEIN_UPSTREAM_BASE deliberately unset
    ok, reason = _arm().ready()
    assert ok is False
    assert "DASEIN_UPSTREAM_BASE" in reason


def test_ready_skips_when_runner_ping_unreachable(tmp_path):
    """DASEIN_HOOK_CMD set but the runner errors on ping -> SKIP (half-installed runner)."""
    _clear_dasein_env()
    os.environ["DASEIN_API_KEY"] = "dsk_test"
    os.environ["DASEIN_BASE_URL"] = "https://dasein.example/"
    os.environ["DASEIN_UPSTREAM_BASE"] = "http://127.0.0.1:9999"
    os.environ["DASEIN_HOOK_CMD"] = _fake_runner(tmp_path, broken=True)  # exits non-zero on any cmd
    ok, reason = _arm().ready()
    assert ok is False
    assert "did not respond to a `ping`" in reason


def test_ready_skips_when_adjudicator_not_importable(tmp_path):
    """Runner reachable but the SUBMIT adjudicator stage isn't importable -> SKIP (not the product)."""
    _clear_dasein_env()
    os.environ["DASEIN_API_KEY"] = "dsk_test"
    os.environ["DASEIN_BASE_URL"] = "https://dasein.example/"
    os.environ["DASEIN_UPSTREAM_BASE"] = "http://127.0.0.1:9999"
    os.environ["DASEIN_HOOK_CMD"] = _fake_runner(tmp_path, ping_ok=False)
    ok, reason = _arm().ready()
    assert ok is False
    assert "required stage is not importable" in reason


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
