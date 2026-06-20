"""Wiring tests for the woz arm (WithWoz/wozcode-plugin — a REAL Claude Code plugin).

Woz is a ToolArm that loads the WHOLE wozcode plugin via the SDK
``plugins=[{"type":"local","path":WOZ_PLUGIN_DIR}]`` so Claude Code activates Woz's
OWN ``code``/``explore`` subagents + ``code`` MCP server (tools as
``mcp__plugin_woz_code__*``); ``replace_tools`` drops the native file surface so the
agent works THROUGH Woz's tools. The plugin's MCP server serves Search/Edit/Recall/
Sql from a login session stored under ``~/.claude/wozcode/`` — established once via
``scripts/wozcode-cli.js login``.

These tests LOCK the audit fix: ``ready()`` must gate on a VALID login session, not
just on plugin-files + node, so a STALE or ABSENT session SKIPs cleanly (ok=False,
actionable reason) instead of being scheduled and dying as an infra failure for
EVERY task (the plugin tool calls return ``auth.login_required`` → empty patch).
We also lock the attach() contract (loads the plugin dir, the right tool globs,
replace_tools=True) and the existing plugin-tree / node gates.

We never touch the network or the real CLI. We build a fake plugin tree on a temp
dir and put a fake ``node`` on PATH (pointed at via WOZ_NODE) whose ``status``
output is scripted by an env var — so we exercise the REAL ``_session_authenticated``
+ ``ready()`` logic against deterministic CLI behaviour.

Runnable two ways:
    py -m pytest tests/test_woz_arm.py -q
    py tests/test_woz_arm.py            # standalone, prints PASS/FAIL
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

# make the repo root importable when run as a bare script (py tests/...).
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bench.arm import ArmKind, ToolArm, get_arm  # noqa: E402
from arms.woz import (  # noqa: E402
    WozArm,
    WozLoginError,
    WozStaleSessionError,
    _session_authenticated,
)


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


def _make_plugin_tree(root: Path) -> Path:
    """Create a minimal but structurally-valid Woz plugin tree under ``root``:
    ``.claude-plugin/plugin.json`` + ``agents/explore.md`` + ``scripts/wozcode-cli.js``
    (an empty stub — the FAKE node, not this file, decides the status output)."""
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        '{"name":"woz","version":"0.3.82"}', encoding="utf-8"
    )
    (root / "agents").mkdir(parents=True, exist_ok=True)
    (root / "agents" / "explore.md").write_text("# explore (haiku)\n", encoding="utf-8")
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "wozcode-cli.js").write_text("// stub\n", encoding="utf-8")
    return root


@contextmanager
def _fake_node(status_stdout: str = "Logged in as nick@example.com",
               status_rc: int = 0, version: str = "v22.23.0",
               login_stdout: str = "Authenticated as nick@example.com",
               login_rc: int = 0):
    """Put a fake ``node`` on disk that:
      - prints ``version`` for ``--version`` (exit 0),
      - prints ``status_stdout`` and exits ``status_rc`` for the ``status`` subcmd,
      - prints ``login_stdout`` and exits ``login_rc`` for the ``login`` subcmd,
      - exits 0 otherwise.
    The ``login`` branch lets us exercise ``setup()`` against deterministic CLI
    behaviour (success / stale-token / generic failure). Yields the fake node path
    (to set WOZ_NODE). Works on POSIX + Windows.
    """
    d = Path(tempfile.mkdtemp(prefix="woz_node_"))
    if os.name == "nt":
        node = d / "node.cmd"
        # %* is the full arg string; crude but sufficient (we only branch on tokens).
        # NB: check ``login`` BEFORE ``status`` is irrelevant (disjoint tokens), but
        # the login branch must come before the catch-all exit.
        node.write_text(
            "@echo off\r\n"
            f'echo %* | findstr /C:"--version" >nul && (echo {version} & exit /b 0)\r\n'
            f'echo %* | findstr /C:"status" >nul && (echo {status_stdout} & exit /b {status_rc})\r\n'
            f'echo %* | findstr /C:"login" >nul && (echo {login_stdout} & exit /b {login_rc})\r\n'
            "exit /b 0\r\n",
            encoding="utf-8",
        )
    else:
        node = d / "node"
        node.write_text(
            "#!/bin/sh\n"
            'for a in "$@"; do\n'
            f'  case "$a" in\n'
            f'    --version) echo "{version}"; exit 0;;\n'
            f'    status) echo "{status_stdout}"; exit {status_rc};;\n'
            f'    login) echo "{login_stdout}"; exit {login_rc};;\n'
            "  esac\n"
            "done\n"
            "exit 0\n",
            encoding="utf-8",
        )
        node.chmod(node.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    try:
        yield str(node)
    finally:
        pass


# ── identity: a ToolArm that loads the real plugin ───────────────────────────
def test_woz_is_a_tool_arm():
    arm = WozArm()
    assert arm.kind == ArmKind.TOOL
    assert isinstance(arm, ToolArm)
    assert arm.needs == ["WOZ_API_KEY"]


def test_registered_under_name_woz():
    arm = get_arm("woz")
    assert isinstance(arm, WozArm)
    assert arm.name == "woz"


# ── attach(): loads the plugin dir, right tool globs, replace_tools ───────────
def test_attach_loads_plugin_and_replaces_tools():
    with tempfile.TemporaryDirectory() as td:
        root = _make_plugin_tree(Path(td))
        with _env(WOZ_PLUGIN_DIR=str(root)):
            att = WozArm().attach()
    assert att.plugin_dir == str(root)
    assert att.plugin_tool_globs == ["mcp__plugin_woz_code__*"]
    assert att.replace_tools is True
    # We load a whole plugin, NOT a bare hand-spawned server.
    assert att.mcp_server_cmd is None


# ── the AUDIT FIX: ready() gates on a VALID session ──────────────────────────
def test_ready_true_with_valid_session():
    """Plugin files + node OK + an AUTHENTICATED status => READY."""
    with tempfile.TemporaryDirectory() as td:
        root = _make_plugin_tree(Path(td))
        with _fake_node(status_stdout="Logged in as nick@example.com", status_rc=0) as node:
            with _env(WOZ_API_KEY="k", WOZ_PLUGIN_DIR=str(root),
                      WOZ_NODE=node, WOZ_SKIP_SESSION_CHECK=None):
                ok, reason = WozArm().ready()
    assert ok is True, reason


def test_ready_false_when_session_stale():
    """The exact live failure: a stale session prints the /woz-login marker and
    exits non-zero. ready() must SKIP (not run the arm → no per-task infra death)."""
    with tempfile.TemporaryDirectory() as td:
        root = _make_plugin_tree(Path(td))
        with _fake_node(
            status_stdout="WozCode session is stale. Please log in again using /woz-login.",
            status_rc=1,
        ) as node:
            with _env(WOZ_API_KEY="k", WOZ_PLUGIN_DIR=str(root),
                      WOZ_NODE=node, WOZ_SKIP_SESSION_CHECK=None):
                ok, reason = WozArm().ready()
    assert ok is False
    assert "no valid Woz login session" in reason
    # actionable: tells the operator to (re)login / mint a fresh session.
    assert "login" in reason.lower()


def test_ready_false_when_not_logged_in():
    """Absent session: status prints 'Not logged in.' (the other live string)."""
    with tempfile.TemporaryDirectory() as td:
        root = _make_plugin_tree(Path(td))
        with _fake_node(status_stdout="Not logged in. Run `/woz-login` to authenticate.",
                        status_rc=1) as node:
            with _env(WOZ_API_KEY="k", WOZ_PLUGIN_DIR=str(root),
                      WOZ_NODE=node, WOZ_SKIP_SESSION_CHECK=None):
                ok, reason = WozArm().ready()
    assert ok is False
    assert "no valid Woz login session" in reason


def test_ready_false_when_status_exit_nonzero_no_marker():
    """A non-zero status with no known marker is still treated as unauthenticated
    (fail-closed: a sessionless arm must never be scheduled)."""
    with tempfile.TemporaryDirectory() as td:
        root = _make_plugin_tree(Path(td))
        with _fake_node(status_stdout="some other error", status_rc=7) as node:
            with _env(WOZ_API_KEY="k", WOZ_PLUGIN_DIR=str(root),
                      WOZ_NODE=node, WOZ_SKIP_SESSION_CHECK=None):
                ok, reason = WozArm().ready()
    assert ok is False
    assert "no valid Woz login session" in reason


# ── session check is bypassable for tests / out-of-band proof ────────────────
def test_skip_session_check_bypasses_probe():
    with _env(WOZ_SKIP_SESSION_CHECK="1"):
        ok, detail = _session_authenticated()
    assert ok is True
    assert "bypass" in detail.lower()


# ── setup(): the audit MINOR fix — a stale token gives a DISTINCT, actionable
#    error (WozStaleSessionError), not a generic WozLoginError; both fail closed ─
def test_setup_succeeds_on_login_ok():
    """A clean login (exit 0) returns normally — no exception, creds 'stored'."""
    with tempfile.TemporaryDirectory() as td:
        root = _make_plugin_tree(Path(td))
        with _fake_node(login_stdout="Authenticated as nick@example.com",
                        login_rc=0) as node:
            with _env(WOZ_API_KEY="k", WOZ_PLUGIN_DIR=str(root), WOZ_NODE=node):
                WozArm().setup()  # must not raise


def test_setup_raises_stale_session_error_on_stale_token():
    """The live failure shape: `login --token <stale website token>` exits non-zero
    and prints 'WozCode session is stale. Please log in again using /woz-login.'
    setup() must raise the DISTINCT WozStaleSessionError (operator sees the
    un-self-healable cause), which still IS-A WozLoginError (fail-closed mapping)."""
    with tempfile.TemporaryDirectory() as td:
        root = _make_plugin_tree(Path(td))
        with _fake_node(
            login_stdout="Error: WozCode session is stale. Please log in again using /woz-login.",
            login_rc=1,
        ) as node:
            with _env(WOZ_API_KEY="stale_website_token",
                      WOZ_PLUGIN_DIR=str(root), WOZ_NODE=node):
                raised = None
                try:
                    WozArm().setup()
                except WozLoginError as e:  # base class — proves it's still caught here
                    raised = e
    assert isinstance(raised, WozStaleSessionError), raised
    assert isinstance(raised, WozLoginError)  # fail-closed: runner mapping unchanged
    msg = str(raised).lower()
    assert "stale" in msg and "/woz-login" in msg
    assert "self-heal" in msg  # tells the operator setup() can't recover it


def test_setup_raises_generic_login_error_on_other_failure():
    """A non-stale login failure (e.g. a real network/key error with NO stale
    marker) stays a generic WozLoginError, NOT misclassified as stale."""
    with tempfile.TemporaryDirectory() as td:
        root = _make_plugin_tree(Path(td))
        with _fake_node(login_stdout="Error: network unreachable (ECONNREFUSED)",
                        login_rc=1) as node:
            with _env(WOZ_API_KEY="k", WOZ_PLUGIN_DIR=str(root), WOZ_NODE=node):
                raised = None
                try:
                    WozArm().setup()
                except WozLoginError as e:
                    raised = e
    assert isinstance(raised, WozLoginError)
    assert not isinstance(raised, WozStaleSessionError)


def test_stale_session_error_is_a_login_error_subclass():
    """Lock the type relationship the runner's catch site relies on."""
    assert issubclass(WozStaleSessionError, WozLoginError)


# ── pre-session gates still fire (don't regress the plugin-tree / node checks) ─
def test_ready_false_when_plugin_dir_unset():
    with _env(WOZ_API_KEY="k", WOZ_PLUGIN_DIR=None):
        ok, reason = WozArm().ready()
    assert ok is False
    assert "WOZ_PLUGIN_DIR" in reason


def test_ready_false_when_explore_agent_missing():
    """A plugin dir with plugin.json but no agents/explore.md is incomplete."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / ".claude-plugin").mkdir(parents=True)
        (root / ".claude-plugin" / "plugin.json").write_text('{"name":"woz"}', encoding="utf-8")
        with _env(WOZ_API_KEY="k", WOZ_PLUGIN_DIR=str(root)):
            ok, reason = WozArm().ready()
    assert ok is False
    assert "explore" in reason.lower()


def test_ready_false_when_api_key_absent():
    with _env(WOZ_API_KEY=None):
        ok, reason = WozArm().ready()
    assert ok is False


# ── _session_authenticated returns the reason on failure (no silent pass) ─────
def test_session_authenticated_fails_when_cli_missing():
    """No CLI on disk → NOT authenticated, with a reason (probe never passes blind)."""
    with tempfile.TemporaryDirectory() as td:
        # plugin dir without scripts/wozcode-cli.js
        root = Path(td)
        with _env(WOZ_PLUGIN_DIR=str(root), WOZ_SKIP_SESSION_CHECK=None):
            ok, detail = _session_authenticated()
    assert ok is False
    assert "wozcode-cli.js" in detail or "CLI missing" in detail


# ── standalone runner (py tests/test_woz_arm.py) ─────────────────────────────
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
