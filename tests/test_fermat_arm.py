"""Wiring/readiness tests for the fermat arm (Quotient Labs shim, cli_path).

Pure stdlib + monkeypatch — subprocess is stubbed, no SDK. Covers the entitlement
fail-closed gate, version recording, cli_path wiring, and that we impose NO tool
restrictions (Fermat's own tool-gate does that).
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from arms import fermat as fmod  # noqa: E402
from arms.fermat import FermatArm  # noqa: E402
from bench.arm import ArmKind  # noqa: E402
from bench.cc_runner import build_arm_config  # noqa: E402


class _R:
    def __init__(self, rc=0, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


def _fake_run(whoami_out="", whoami_rc=0, version_rc=0):
    def run(args, **kw):
        if "--version" in args:
            return _R(version_rc, "fermat 0.1.10")
        if "whoami" in args:
            return _R(whoami_rc, whoami_out)
        return _R(0, "")
    return run


def _shim(tmp_path) -> str:
    p = tmp_path / "claude"           # the fermat `claude` shim
    p.write_text("#!/usr/bin/env bash\n")
    return str(p)


def _prep(monkeypatch, tmp_path, **run_kw):
    monkeypatch.setenv("FERMAT_SHIM", _shim(tmp_path))
    monkeypatch.setattr(fmod, "_resolve_fermat_cli", lambda: "fermat")
    monkeypatch.setattr(fmod, "_fermat_version", lambda cli: "fermat 0.1.10")
    monkeypatch.setattr(fmod.subprocess, "run", _fake_run(**run_kw))
    # SDK is absent on the dev box; pretend ClaudeAgentOptions exposes a cli-path field
    monkeypatch.setattr(fmod, "_sdk_cli_path_field", lambda: "cli_path")


def test_ready_skips_without_shim(monkeypatch):
    monkeypatch.delenv("FERMAT_SHIM", raising=False)
    ok, reason = FermatArm().ready()
    assert not ok
    assert "missing env" in reason and "FERMAT_SHIM" in reason


def test_ready_skips_when_shim_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("FERMAT_SHIM", str(tmp_path / "no-such-claude"))
    ok, reason = FermatArm().ready()
    assert not ok
    assert "does not exist" in reason


def test_ready_skips_when_version_fails(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path, version_rc=1)
    ok, reason = FermatArm().ready()
    assert not ok
    assert "--version" in reason


def test_ready_skips_when_not_entitled(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path, whoami_out="Not logged in. Please run `fermat login`.")
    ok, reason = FermatArm().ready()
    assert not ok
    assert "not entitled" in reason.lower()
    assert "vanilla" in reason  # would silently run vanilla otherwise


def test_ready_skips_when_whoami_nonzero(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path, whoami_out="some banner", whoami_rc=2)
    ok, reason = FermatArm().ready()
    assert not ok
    assert "not entitled" in reason.lower()


def test_ready_ok_when_entitled(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path,
          whoami_out="Logged in as nick@turnuptalent.ai (plan: pro, active)")
    arm = FermatArm()
    ok, reason = arm.ready()
    assert ok, reason
    assert arm.fermat_version == "fermat 0.1.10"
    assert "confirmed" in reason


def test_cli_path_points_at_shim(monkeypatch, tmp_path):
    shim = _shim(tmp_path)
    monkeypatch.setenv("FERMAT_SHIM", shim)
    arm = FermatArm()
    assert arm.cli_path == shim


def test_imposes_no_tool_restrictions(monkeypatch, tmp_path):
    monkeypatch.setenv("FERMAT_SHIM", _shim(tmp_path))
    arm = FermatArm()
    cfg = build_arm_config(arm)
    assert arm.kind == ArmKind.BASELINE
    # model goes direct to the gateway (Fermat's proxy upstream); no vendor URL here
    assert cfg.client_base_url in (None, "")
    # we disallow nothing and load no plugin — Fermat's own tool-gate governs tools
    assert cfg.disallowed_tools == []
    assert cfg.plugins == []


def test_setup_sets_real_claude_bin_when_discoverable(monkeypatch, tmp_path):
    monkeypatch.setenv("FERMAT_SHIM", _shim(tmp_path))
    monkeypatch.delenv("FERMAT_CLAUDE_BIN", raising=False)
    monkeypatch.setattr(fmod.shutil, "which",
                        lambda n: "/opt/claude-anthropic" if n == "claude-anthropic" else None)
    arm = FermatArm()
    arm.setup()
    import os
    assert os.environ.get("FERMAT_CLAUDE_BIN") == "/opt/claude-anthropic"
