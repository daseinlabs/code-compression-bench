"""Wiring tests for the rtk arm (rtk-ai/rtk — a CLI binary run as a HOOK, not a proxy).

rtk ("Rust Token Killer", github.com/rtk-ai/rtk) is a single Rust binary that
compresses SHELL-COMMAND stdout. It has NO serve/proxy/--upstream mode; it
integrates with Claude Code ONLY as a PreToolUse hook that rewrites Bash commands
(``git status`` -> ``rtk git status``). An earlier audit caught the arm
mis-modeled as a network proxy (ANTHROPIC_BASE_URL -> 127.0.0.1:8802,
needs=['RTK_BASE_URL']) — a fiction rtk never ships. These tests LOCK the correct
contract so a future edit can't regress it back to a proxy:

  * RtkArm is a HOOK arm: ``kind == BASELINE`` (model routes straight to the
    gateway like A0), it is NOT a ProxyArm, and it exposes NO model_base_url /
    headers / RTK_BASE_URL;
  * ``pre_tool_hook`` REWRITES a Bash ``<cmd>`` into ``rtk <cmd>`` for the base
    commands rtk wraps (git/ls/grep/pytest/cargo/...), and returns None (leaves
    the call untouched) for non-Bash tools, unsupported commands, already-wrapped
    commands, and shell forms a single ``rtk `` prefix can't safely front
    (pipes, &&-chains, env-assignments, subshells);
  * ``ready()`` verifies the REAL binary via ``rtk --version`` — it PASSES when a
    working ``rtk`` is on PATH and SKIPs (ok=False, actionable reason) when the
    binary is absent or broken, so a runner without rtk infra-fails the arm
    instead of silently measuring the native agent;
  * the .env.example carries NO uncommented ``RTK_BASE_URL`` (the proxy fiction is
    gone), and the arm registers under the name ``rtk``.

Pure stdlib (a throwaway fake ``rtk`` script on a temp PATH); no network, no SDK,
no vendor code.

Runnable two ways:
    py -m pytest tests/test_rtk_arm.py -q
    py tests/test_rtk_arm.py            # standalone, prints PASS/FAIL
"""

from __future__ import annotations

import os
import re
import stat
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

# make the repo root importable when run as a bare script (py tests/...).
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bench.arm import Arm, ArmKind, ProxyArm, get_arm  # noqa: E402
from arms.rtk import RtkArm, _first_token  # noqa: E402


@contextmanager
def _env(**kv):
    """Temporarily set/clear env vars, restoring the prior state after."""
    saved = {k: os.environ.get(k) for k in kv}
    try:
        for k, v in kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextmanager
def _fake_rtk_on_path(version: str = "rtk 0.28.2", rc: int = 0):
    """Put a working fake ``rtk`` (or ``rtk.cmd`` on Windows) on PATH that prints
    ``version`` and exits ``rc`` for ``rtk --version``. Yields the bin dir."""
    d = tempfile.mkdtemp(prefix="rtk_bin_")
    if os.name == "nt":
        # ready() shells out via subprocess; a .cmd is resolvable by shutil.which.
        script = Path(d) / "rtk.cmd"
        script.write_text(f"@echo {version}\r\n@exit /b {rc}\r\n", encoding="utf-8")
    else:
        script = Path(d) / "rtk"
        script.write_text(f"#!/bin/sh\necho '{version}'\nexit {rc}\n", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = d + os.pathsep + old
    try:
        yield d
    finally:
        os.environ["PATH"] = old


# ── identity: a HOOK arm, NOT a proxy (the audit's core fix) ──────────────────
def test_rtk_is_a_hook_arm_not_a_proxy():
    arm = RtkArm()
    # BASELINE kind => model goes straight to the gateway like A0 (no proxy seam).
    assert arm.kind == ArmKind.BASELINE
    assert not isinstance(arm, ProxyArm)
    # The proxy fiction is gone: no model_base_url, no RTK_BASE_URL need.
    assert not hasattr(arm, "model_base_url")
    assert "RTK_BASE_URL" not in (arm.needs or [])
    assert arm.needs == []


def test_rtk_declares_a_harness_hook():
    """The arm contributes a PreToolUse hook (so the runner wires it)."""
    arm = RtkArm()
    assert arm.has_harness_hooks() is True
    # specifically the pre_tool_hook is overridden away from the base no-op.
    assert getattr(type(arm), "pre_tool_hook") is not getattr(Arm, "pre_tool_hook")


def test_registered_under_name_rtk():
    arm = get_arm("rtk")
    assert isinstance(arm, RtkArm)
    assert arm.name == "rtk"


# ── pre_tool_hook: rewrite Bash <cmd> -> rtk <cmd> for supported commands ──────
def test_rewrites_git_status():
    arm = RtkArm()
    with _env(RTK_BIN=None):
        out = arm.pre_tool_hook("Bash", {"command": "git status"})
    assert out == {"tool_input": {"command": "rtk git status"}}


def test_rewrites_supported_base_commands():
    arm = RtkArm()
    cases = {
        "git log --oneline": "rtk git log --oneline",
        "ls -la src/": "rtk ls -la src/",
        "grep -rn TODO .": "rtk grep -rn TODO .",
        "pytest tests/ -q": "rtk pytest tests/ -q",
        "cargo test": "rtk cargo test",
        "go test ./...": "rtk go test ./...",
        "docker ps": "rtk docker ps",
        "rg pattern src": "rtk rg pattern src",
    }
    with _env(RTK_BIN=None):
        for cmd, want in cases.items():
            out = arm.pre_tool_hook("Bash", {"command": cmd})
            assert out is not None, cmd
            assert out["tool_input"]["command"] == want, cmd


def test_preserves_other_tool_input_keys():
    """A rewrite keeps non-command keys (timeout, description, ...) intact."""
    arm = RtkArm()
    inp = {"command": "git diff", "timeout": 30000, "description": "show diff"}
    with _env(RTK_BIN=None):
        out = arm.pre_tool_hook("Bash", inp)
    assert out["tool_input"] == {
        "command": "rtk git diff", "timeout": 30000, "description": "show diff"
    }
    # and we don't mutate the caller's dict in place.
    assert inp["command"] == "git diff"


def test_honors_rtk_bin_override():
    arm = RtkArm()
    with _env(RTK_BIN="/opt/rtk/bin/rtk"):
        out = arm.pre_tool_hook("Bash", {"command": "git status"})
    assert out["tool_input"]["command"] == "/opt/rtk/bin/rtk git status"


# ── pre_tool_hook: NO rewrite where rtk's own hook no-ops ─────────────────────
def test_non_bash_tools_untouched():
    arm = RtkArm()
    assert arm.pre_tool_hook("Read", {"file_path": "/x"}) is None
    assert arm.pre_tool_hook("Grep", {"pattern": "x"}) is None
    assert arm.pre_tool_hook("Edit", {"file_path": "/x"}) is None


def test_unsupported_commands_untouched():
    arm = RtkArm()
    for cmd in ("python setup.py test", "make", "cd src", "mkdir build",
                "source venv/bin/activate", "echo hi", "./run.sh"):
        assert arm.pre_tool_hook("Bash", {"command": cmd}) is None, cmd


def test_already_wrapped_not_double_wrapped():
    arm = RtkArm()
    assert arm.pre_tool_hook("Bash", {"command": "rtk git status"}) is None


def test_rtk_subcommand_only_tokens_not_wrapped():
    """Faithfulness narrowing: rtk's own SUBCOMMANDS (``rtk smart``/``rtk json``/
    ``rtk env``/...) and the bash builtin ``read`` are NOT standalone host binaries
    the agent shells out to, so we must NOT prepend ``rtk `` to a bare ``env``/
    ``log``/``json``/``read`` line — that would wrap an UNRELATED host command (the
    POSIX ``env`` utility, a project ``log`` script). The real rtk hook keys on its
    recognized wrappable EXECUTABLES, not its own subcommand verbs."""
    arm = RtkArm()
    for cmd in (
        "read -r line < file",        # bash builtin (agent uses native Read tool)
        "env",                        # POSIX env utility, NOT `rtk env`
        "env FOO=bar python x.py",    # env as a launcher prefix
        "json",                       # `rtk json` is a subcommand, not a host bin
        "log show",                   # a project/host `log`, not `rtk log`
        "deps",                       # `rtk deps` subcommand
        "smart src/",                 # `rtk smart` subcommand
        "summary",                    # `rtk summary` subcommand
    ):
        assert arm.pre_tool_hook("Bash", {"command": cmd}) is None, cmd


def test_unsafe_shell_forms_untouched():
    """A single ``rtk `` prefix is only correct for a bare leading command; pipes,
    chains, subshells and env-assignments are left untouched (rtk's hook no-ops)."""
    arm = RtkArm()
    for cmd in (
        "git status | head",          # pipe
        "git status && git diff",     # &&-chain
        "$(git rev-parse HEAD)",      # command substitution
        "(git status)",               # subshell
        "FOO=bar git status",         # env-assignment prefix
        "",                           # empty
        "   ",                        # whitespace-only
    ):
        assert arm.pre_tool_hook("Bash", {"command": cmd}) is None, cmd


def test_first_token_helper():
    assert _first_token("git status") == "git"
    assert _first_token("  ls -la ") == "ls"
    assert _first_token("git status | head") is None
    assert _first_token("FOO=bar cmd") is None
    assert _first_token("rtk git status") is None
    assert _first_token("") is None


# ── ready(): verify the REAL binary via `rtk --version` ──────────────────────
def test_ready_true_when_binary_present():
    with _fake_rtk_on_path("rtk 0.28.2"), _env(RTK_BIN=None):
        ok, reason = RtkArm().ready()
    assert ok is True, reason
    assert "ok" in reason


def test_ready_false_when_binary_absent():
    # Point RTK_BIN at a name that won't resolve anywhere.
    with _env(RTK_BIN="rtk_definitely_not_installed_xyz"):
        ok, reason = RtkArm().ready()
    assert ok is False
    # reason must tell the operator how to install the real product.
    assert "rtk-ai/rtk" in reason
    assert "install" in reason.lower()


def test_ready_false_when_binary_broken():
    # A binary that exits non-zero on --version is a broken install.
    with _fake_rtk_on_path("boom", rc=3), _env(RTK_BIN=None):
        ok, reason = RtkArm().ready()
    assert ok is False
    assert "exited 3" in reason or "broken" in reason.lower()


# ── cross-doc: the proxy fiction (RTK_BASE_URL) is gone from .env.example ─────
def test_env_example_has_no_uncommented_rtk_base_url():
    """The audit gap: .env.example must NOT carry an uncommented RTK_BASE_URL —
    rtk is a hook arm, not a proxy; a stray URL would mislead provisioning into
    standing up a fake rtk proxy that the product never ships."""
    env_example = (_ROOT / ".env.example").read_text(encoding="utf-8")
    # No active (uncommented) RTK_BASE_URL assignment anywhere.
    assert not re.search(r"^RTK_BASE_URL=", env_example, re.MULTILINE), \
        "RTK_BASE_URL must not be an active env var (rtk is a hook arm, not a proxy)"


# ── standalone runner (py tests/test_rtk_arm.py) ─────────────────────────────
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
