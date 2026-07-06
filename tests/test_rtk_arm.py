"""Wiring tests for the rtk arm (rtk-ai/rtk — a CLI binary run as a HOOK, not a proxy).

rtk ("Rust Token Killer", github.com/rtk-ai/rtk) is a single Rust binary that
compresses SHELL-COMMAND stdout. It has NO serve/proxy/--upstream mode; it
integrates with Claude Code ONLY as a PreToolUse hook that rewrites Bash commands
(``git status`` -> ``rtk git status``). An earlier audit caught the arm
mis-modeled as a network proxy (ANTHROPIC_BASE_URL -> 127.0.0.1:8802,
needs=['RTK_BASE_URL']) — a fiction rtk never ships. A SECOND audit caught the arm
re-implementing rtk's rewrite DECISION in Python (a hand-curated ~30-command set +
``_first_token`` parser) — an APPROXIMATION that under-wrapped (missed
``cat``->``read``, no-opped on pipes/&&-chains/env-prefixes). The real product,
since v0.24.0, delegates that decision to the binary's OWN ``rtk rewrite`` sub-
command (the "single source of truth for hooks"). These tests LOCK the correct
contract so a future edit can't regress to either fiction:

  * RtkArm is a HOOK arm: ``kind == BASELINE`` (model routes straight to the
    gateway like A0), it is NOT a ProxyArm, and it exposes NO model_base_url /
    headers / RTK_BASE_URL;
  * ``pre_tool_hook`` DELEGATES to ``rtk rewrite -- <cmd>`` and uses its stdout
    VERBATIM as the rewritten Bash command (no Python command list): it gates on
    STDOUT, not the exit code (``rtk rewrite`` exits non-zero for the rewritten
    case by design), returns None for non-Bash tools / empty commands / commands
    rtk leaves unchanged, and never touches Read/Grep/Glob;
  * ``ready()`` verifies the REAL binary via ``rtk --version`` AND smokes the
    actual ``rtk rewrite -- 'git status'`` integration path — it PASSES when a
    working >= 0.24.0 ``rtk`` is on PATH and SKIPs (ok=False, actionable reason)
    when the binary is absent, broken, or too old to ship ``rewrite`` (so a runner
    with a legacy rtk infra-fails the arm instead of silently never compressing);
  * the .env.example carries NO uncommented ``RTK_BASE_URL`` (the proxy fiction is
    gone), and the arm registers under the name ``rtk``.

Pure stdlib (a throwaway fake ``rtk`` script on a temp PATH that emulates the REAL
``rtk rewrite`` contract: prints the rewritten command on stdout, exits non-zero
for "rewritten" and exits 1 with no output for "no equivalent"); no network, no
SDK, no vendor code.

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
from arms.rtk import RtkArm  # noqa: E402


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


# A faithful emulation of the REAL `rtk rewrite` contract, observed live on
# rtk 0.42.4 (see arms/rtk.py): it prints the rewritten command to STDOUT and
# exits NON-ZERO (3) when rtk has an equivalent; it prints NOTHING and exits 1
# when it doesn't. Detection runs over the full set incl. cat->read, pipes,
# &&-chains and env-prefixes (and a no-op for `read`, builtins, cd, python, …).
# This table reproduces exactly what we probed from the binary.
_REWRITE_TABLE = {
    "git status": "rtk git status",
    "git status -s": "rtk git status -s",
    "git log --oneline": "rtk git log --oneline",
    "git commit -m x": "rtk git commit -m x",
    "git pull": "rtk git pull",
    "git diff": "rtk git diff",
    "ls -la src/": "rtk ls -la src/",
    "grep -rn TODO .": "rtk grep -rn TODO .",
    "pytest tests/ -q": "rtk pytest tests/ -q",
    "cargo test": "rtk cargo test",
    "go test ./...": "rtk go test ./...",
    "docker ps": "rtk docker ps",
    "rg pattern src": "rtk rg pattern src",
    # cat -> rtk READ (the divergence the Python approximation got wrong):
    "cat file.txt": "rtk read file.txt",
    # pipe / chain / env-prefix: rtk rewrites the INNER command(s) itself:
    "git status | head": "rtk git status | head",
    "cargo test && git push": "rtk cargo test && rtk git push",
    "FOO=bar git status": "FOO=bar rtk git status",
    # already-wrapped -> returned unchanged (idempotent, no double-wrap):
    "rtk git status": "rtk git status",
}
# Commands rtk has NO equivalent for -> empty stdout, exit 1 (no-op):
_NO_REWRITE = {
    "read foo", "read -r line < file", "echo hi", "cd src", "mkdir build",
    "python setup.py test", "make", "source venv/bin/activate", "./run.sh",
    "docker compose up", "(git status)", "env",
}


@contextmanager
def _fake_rtk_on_path(version: str = "rtk 0.42.4", version_rc: int = 0,
                      with_rewrite: bool = True):
    """Put a fake ``rtk`` on PATH that emulates the REAL binary's two relevant
    surfaces:

      * ``rtk --version``  -> prints ``version``, exits ``version_rc``;
      * ``rtk rewrite -- <cmd>`` -> looks ``<cmd>`` up in _REWRITE_TABLE; if found,
        prints the rewrite to stdout and exits 3 (the "rewritten" code); else
        prints nothing and exits 1 (the "no equivalent" code) — matching the live
        contract. When ``with_rewrite`` is False, the ``rewrite`` subcommand is
        UNKNOWN (errors to stderr, exits 2) to simulate a legacy < 0.24.0 binary.

    Yields the bin dir. Cross-platform (a Python-backed .cmd on Windows so the
    command lookup is metacharacter-safe — cmd.exe would mangle a literal ``|`` or
    ``&`` in a batch ``if`` comparison; a POSIX sh script elsewhere)."""
    d = tempfile.mkdtemp(prefix="rtk_bin_")
    pairs = list(_REWRITE_TABLE.items())
    if os.name == "nt":
        # On Windows, drive the fake from a tiny Python shim invoked by a .cmd
        # wrapper. Doing the table lookup in Python (exact string compare on the
        # raw command) sidesteps cmd.exe's reparsing of shell metacharacters like
        # ``|`` / ``&`` inside batch comparisons — which would otherwise break the
        # pipe/chain rewrite cases. The shim reproduces the SAME stdout/exit-code
        # contract as the sh fake.
        shim = Path(d) / "rtk_fake.py"
        shim_src = [
            "import sys",
            f"VERSION = {version!r}",
            f"VERSION_RC = {version_rc}",
            f"WITH_REWRITE = {with_rewrite}",
            f"TABLE = {dict(pairs)!r}",
            "argv = sys.argv[1:]",
            "if argv and argv[0] == '--version':",
            "    print(VERSION)",
            "    sys.exit(VERSION_RC)",
            "if argv and argv[0] == 'rewrite':",
            "    if not WITH_REWRITE:",
            "        sys.stderr.write(\"error: unrecognized subcommand 'rewrite'\\n\")",
            "        sys.exit(2)",
            "    rest = argv[1:]",
            "    if rest and rest[0] == '--':",
            "        rest = rest[1:]",
            "    cmd = ' '.join(rest)",
            "    rw = TABLE.get(cmd)",
            "    if rw is None:",
            "        sys.exit(1)",          # no equivalent -> empty stdout, rc 1
            "    print(rw)",
            "    sys.exit(3)",              # rewritten -> stdout + rc 3 (real contract)
            "sys.exit(1)",
        ]
        shim.write_text("\n".join(shim_src) + "\n", encoding="utf-8")
        script = Path(d) / "rtk.cmd"
        # Forward all args to the shim via the SAME interpreter running the tests.
        py = sys.executable.replace("/", "\\")
        script.write_text(
            "@echo off\r\n"
            f'"{py}" "{shim}" %*\r\n',
            encoding="utf-8",
        )
    else:
        script = Path(d) / "rtk"
        lines = ["#!/bin/sh"]
        lines.append('if [ "$1" = "--version" ]; then')
        lines.append(f"  echo '{version}'")
        lines.append(f"  exit {version_rc}")
        lines.append("fi")
        lines.append('if [ "$1" = "rewrite" ]; then')
        if with_rewrite:
            # the command is the LAST arg (after `rewrite --`).
            lines.append('  shift')
            lines.append('  [ "$1" = "--" ] && shift')
            lines.append('  cmd="$*"')
            lines.append('  case "$cmd" in')
            for raw, rw in pairs:
                esc = raw.replace("\\", "\\\\").replace('"', '\\"')
                rwe = rw.replace("\\", "\\\\").replace('"', '\\"')
                lines.append(f'    "{esc}") echo "{rwe}"; exit 3 ;;')
            lines.append("  esac")
            lines.append("  exit 1")
        else:
            lines.append("  echo \"error: unrecognized subcommand 'rewrite'\" 1>&2")
            lines.append("  exit 2")
        lines.append("fi")
        lines.append("exit 1")
        script.write_text("\n".join(lines) + "\n", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = d + os.pathsep + old
    try:
        yield d
    finally:
        os.environ["PATH"] = old


# ── identity: a HOOK arm, NOT a proxy (the first audit's core fix) ────────────
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


# ── pre_tool_hook DELEGATES to `rtk rewrite` (the product's own decision) ─────
def test_rewrites_git_status_via_delegation():
    arm = RtkArm()
    with _fake_rtk_on_path(), _env(RTK_BIN=None):
        out = arm.pre_tool_hook("Bash", {"command": "git status"})
    assert out == {"tool_input": {"command": "rtk git status"}}


def test_rewrites_full_command_set_via_delegation():
    """Every command the REAL `rtk rewrite` wraps is wrapped here — because we use
    its stdout verbatim, NOT a Python list. Includes cases the old hand-curated
    arm got wrong or skipped."""
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
        # the product remaps cat -> rtk READ (the Python approximation prepended a
        # bare `rtk cat`, diverging from the real product):
        "cat file.txt": "rtk read file.txt",
        # git pull / commit ARE wrapped — assert we follow the product surface:
        "git pull": "rtk git pull",
        "git commit -m x": "rtk git commit -m x",
    }
    with _fake_rtk_on_path(), _env(RTK_BIN=None):
        for cmd, want in cases.items():
            out = arm.pre_tool_hook("Bash", {"command": cmd})
            assert out is not None, cmd
            assert out["tool_input"]["command"] == want, cmd


def test_pipe_chain_envprefix_rewritten_by_product():
    """The old arm NO-OPPED on any pipe / &&-chain / env-prefix. The real
    `rtk rewrite` handles them itself (rewriting the INNER command(s)); delegating
    means the bench now sees those rewrites too — closing the under-compression
    gap the audit flagged."""
    arm = RtkArm()
    cases = {
        "git status | head": "rtk git status | head",
        "cargo test && git push": "rtk cargo test && rtk git push",
        "FOO=bar git status": "FOO=bar rtk git status",
    }
    with _fake_rtk_on_path(), _env(RTK_BIN=None):
        for cmd, want in cases.items():
            out = arm.pre_tool_hook("Bash", {"command": cmd})
            assert out is not None, cmd
            assert out["tool_input"]["command"] == want, cmd


def test_preserves_other_tool_input_keys():
    """A rewrite keeps non-command keys (timeout, description, ...) intact."""
    arm = RtkArm()
    inp = {"command": "git diff", "timeout": 30000, "description": "show diff"}
    with _fake_rtk_on_path(), _env(RTK_BIN=None):
        out = arm.pre_tool_hook("Bash", inp)
    assert out["tool_input"] == {
        "command": "rtk git diff", "timeout": 30000, "description": "show diff"
    }
    # and we don't mutate the caller's dict in place.
    assert inp["command"] == "git diff"


def test_honors_rtk_bin_override():
    """RTK_BIN pins a specific binary path; both ready() and the rewrite resolve it.
    Point RTK_BIN at the fake binary's absolute path and confirm the rewrite still
    delegates through it."""
    with _fake_rtk_on_path() as d:
        bin_path = str(Path(d) / ("rtk.cmd" if os.name == "nt" else "rtk"))
        with _env(RTK_BIN=bin_path):
            out = RtkArm().pre_tool_hook("Bash", {"command": "git status"})
    assert out == {"tool_input": {"command": "rtk git status"}}


# ── pre_tool_hook: NO rewrite where rtk's own `rtk rewrite` returns nothing ───
def test_non_bash_tools_untouched():
    arm = RtkArm()
    with _fake_rtk_on_path(), _env(RTK_BIN=None):
        assert arm.pre_tool_hook("Read", {"file_path": "/x"}) is None
        assert arm.pre_tool_hook("Grep", {"pattern": "x"}) is None
        assert arm.pre_tool_hook("Edit", {"file_path": "/x"}) is None


def test_unsupported_commands_untouched():
    """Commands `rtk rewrite` has no equivalent for (empty stdout) -> no rewrite.
    The DECISION is rtk's, not ours — we just honor an empty stdout."""
    arm = RtkArm()
    with _fake_rtk_on_path(), _env(RTK_BIN=None):
        for cmd in ("python setup.py test", "make", "cd src", "mkdir build",
                    "source venv/bin/activate", "echo hi", "./run.sh",
                    "docker compose up"):
            assert arm.pre_tool_hook("Bash", {"command": cmd}) is None, cmd


def test_read_builtin_not_wrapped_because_product_says_so():
    """`read` is a bash builtin the product does NOT wrap (the agent uses the
    native Read tool). The exclusion is now the PRODUCT's decision (rtk rewrite
    returns nothing), not a Python opt-out — so it's faithful by construction."""
    arm = RtkArm()
    with _fake_rtk_on_path(), _env(RTK_BIN=None):
        assert arm.pre_tool_hook("Bash", {"command": "read foo"}) is None
        assert arm.pre_tool_hook("Bash", {"command": "read -r line < file"}) is None


def test_already_wrapped_not_double_wrapped():
    """`rtk rewrite` returns an already-`rtk `-prefixed command unchanged; since the
    output equals the input, we return None (no double-wrap)."""
    arm = RtkArm()
    with _fake_rtk_on_path(), _env(RTK_BIN=None):
        assert arm.pre_tool_hook("Bash", {"command": "rtk git status"}) is None


def test_empty_command_untouched():
    arm = RtkArm()
    with _fake_rtk_on_path(), _env(RTK_BIN=None):
        assert arm.pre_tool_hook("Bash", {"command": ""}) is None
        assert arm.pre_tool_hook("Bash", {"command": "   "}) is None


def test_no_rtk_binary_runs_unchanged():
    """If the binary can't be resolved at hook time, we run the command unchanged
    (the product hook's `$(rtk rewrite "$CMD") || exit 0` fallback). ready() gates
    this case before a real run; this is the belt-and-suspenders safety net."""
    arm = RtkArm()
    with _env(RTK_BIN="rtk_definitely_not_installed_xyz"):
        assert arm.pre_tool_hook("Bash", {"command": "git status"}) is None


# ── ready(): verify the REAL binary AND the `rtk rewrite` integration path ────
def test_ready_true_when_binary_and_rewrite_present():
    with _fake_rtk_on_path("rtk 0.42.4", with_rewrite=True), _env(RTK_BIN=None):
        ok, reason = RtkArm().ready()
    assert ok is True, reason
    assert "ok" in reason
    # the reason proves we actually smoked the rewrite path:
    assert "rewrite" in reason


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
    with _fake_rtk_on_path("boom", version_rc=3), _env(RTK_BIN=None):
        ok, reason = RtkArm().ready()
    assert ok is False
    assert "exited 3" in reason or "broken" in reason.lower()


def test_ready_false_when_rewrite_subcommand_missing():
    """The minor-audit fix: a < 0.24.0 binary passes `--version` but LACKS
    `rtk rewrite`. ready() must catch it BEFORE a paid panel and SKIP with the
    >= 0.24.0 requirement named — mirroring how woz.ready() checks the real login
    session, not just node presence."""
    with _fake_rtk_on_path("rtk 0.20.0", with_rewrite=False), _env(RTK_BIN=None):
        ok, reason = RtkArm().ready()
    assert ok is False
    assert "0.24.0" in reason
    assert "rewrite" in reason


# ── cross-doc: the proxy fiction (RTK_BASE_URL) is gone from .env.example ─────
def test_env_example_has_no_uncommented_rtk_base_url():
    """The first audit's gap: .env.example must NOT carry an uncommented
    RTK_BASE_URL — rtk is a hook arm, not a proxy; a stray URL would mislead
    provisioning into standing up a fake rtk proxy that the product never ships."""
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
