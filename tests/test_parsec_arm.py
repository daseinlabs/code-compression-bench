"""Wiring/readiness tests for the parsec arm (proxy + plugin, spawned per solve).

Pure stdlib + monkeypatch — never spawns a real binary, never touches the SDK.
Covers: readiness reasons + fail-closed brain gate, proxy+plugin config building,
and the savings-ledger/v0 summary math with synthetic rows.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# make the repo root importable when run as a bare script (py tests/...).
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from arms import parsec as pmod  # noqa: E402
from arms.parsec import ParsecArm, summarize_savings_ledger  # noqa: E402
from bench.arm import ArmKind  # noqa: E402
from bench.cc_runner import build_arm_config  # noqa: E402

_NEEDED = ("PARSEC_BIN", "PARSEC_PLUGIN_DIR", "PARSEC_CREDENTIALS",
           "PARSEC_BENCH_CKPT_SHA256", "PARSEC_PROXY_STATUS_PATH")


def _clear(monkeypatch):
    for k in _NEEDED:
        monkeypatch.delenv(k, raising=False)


def _make_plugin_dir(tmp_path) -> str:
    d = tmp_path / "parsec_plugin"
    (d / ".claude-plugin").mkdir(parents=True)
    (d / ".claude-plugin" / "plugin.json").write_text('{"name": "parsec"}')
    return str(d)


# ── readiness ────────────────────────────────────────────────────────────────
def test_ready_skips_without_env(monkeypatch):
    _clear(monkeypatch)
    ok, reason = ParsecArm().ready()
    assert not ok
    assert "missing env" in reason
    assert "PARSEC_BIN" in reason


def test_ready_skips_when_bin_unresolvable(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setenv("PARSEC_BIN", str(tmp_path / "nope-not-a-file"))
    monkeypatch.setenv("PARSEC_PLUGIN_DIR", _make_plugin_dir(tmp_path))
    monkeypatch.setattr(pmod, "_resolve_bin", lambda: None)  # nothing on PATH either
    ok, reason = ParsecArm().ready()
    assert not ok
    assert "does not resolve" in reason


def test_ready_skips_without_plugin_json(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setenv("PARSEC_BIN", "parsec")
    monkeypatch.setenv("PARSEC_PLUGIN_DIR", str(tmp_path / "empty"))
    (tmp_path / "empty").mkdir()
    monkeypatch.setattr(pmod, "_resolve_bin", lambda: "/usr/bin/parsec")
    ok, reason = ParsecArm().ready()
    assert not ok
    assert "not an installed parsec plugin" in reason


def test_ready_skips_without_credentials(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setenv("PARSEC_BIN", "parsec")
    monkeypatch.setenv("PARSEC_PLUGIN_DIR", _make_plugin_dir(tmp_path))
    monkeypatch.setattr(pmod, "_resolve_bin", lambda: "/usr/bin/parsec")
    monkeypatch.setattr(pmod, "_credentials_src", lambda: str(tmp_path / "no-creds.json"))
    ok, reason = ParsecArm().ready()
    assert not ok
    assert "credentials not found" in reason


def _prep_probe(monkeypatch, tmp_path, ckpt_result):
    """Set up a ready() that reaches the live brain probe, stubbing the spawn."""
    monkeypatch.setenv("PARSEC_BIN", "parsec")
    monkeypatch.setenv("PARSEC_PLUGIN_DIR", _make_plugin_dir(tmp_path))
    cred = tmp_path / "credentials.json"
    cred.write_text("{}")
    monkeypatch.setattr(pmod, "_resolve_bin", lambda: "/usr/bin/parsec")
    monkeypatch.setattr(pmod, "_credentials_src", lambda: str(cred))
    monkeypatch.setattr(pmod, "_parsec_version", lambda b: "parsec 0.2.19")
    monkeypatch.setattr(pmod._ParsecProxy, "start", lambda self, **kw: self)
    monkeypatch.setattr(pmod._ParsecProxy, "stop", lambda self: None)
    monkeypatch.setattr(pmod, "_probe_checkpoint", lambda base: ckpt_result)


def test_ready_fail_closed_when_no_brain(monkeypatch, tmp_path):
    _clear(monkeypatch)
    _prep_probe(monkeypatch, tmp_path, (False, ""))
    ok, reason = ParsecArm().ready()
    assert not ok
    assert "no brain checkpoint" in reason
    assert "passthrough" in reason  # never a mislabeled passthrough run


def test_ready_ok_records_checkpoint_and_version(monkeypatch, tmp_path):
    _clear(monkeypatch)
    _prep_probe(monkeypatch, tmp_path, (True, "sha256:abc123"))
    arm = ParsecArm()
    ok, reason = arm.ready()
    assert ok
    assert arm.checkpoint_id == "sha256:abc123"
    assert arm.parsec_version == "parsec 0.2.19"
    assert "abc123" in reason


def test_ready_ckpt_mismatch_is_contaminated(monkeypatch, tmp_path):
    _clear(monkeypatch)
    _prep_probe(monkeypatch, tmp_path, (True, "sha256:abc123"))
    monkeypatch.setenv("PARSEC_BENCH_CKPT_SHA256", "sha256:different")
    ok, reason = ParsecArm().ready()
    assert not ok
    assert "contaminated" in reason


# ── config building (proxy + plugin) ─────────────────────────────────────────
def test_build_arm_config_is_proxy_plus_plugin(monkeypatch, tmp_path):
    _clear(monkeypatch)
    plugin = _make_plugin_dir(tmp_path)
    monkeypatch.setenv("PARSEC_PLUGIN_DIR", plugin)
    arm = ParsecArm()
    cfg = build_arm_config(arm)
    assert arm.kind == ArmKind.PROXY
    # plugin loaded via SDK plugins=[{"type":"local","path":...}]
    assert cfg.plugins == [{"type": "local", "path": os.path.abspath(plugin)}]
    # native tools kept (replace_tools False) + plugin globs + Agent allow-listed
    assert "Read" in cfg.allowed_tools and "Bash" in cfg.allowed_tools
    assert "Agent" in cfg.allowed_tools
    assert "mcp__plugin_parsec_*__*" in cfg.allowed_tools
    assert cfg.disallowed_tools == []
    # proxy base is only known at start_run() time -> empty here
    assert cfg.client_base_url in (None, "")


# ── ledger summary math ──────────────────────────────────────────────────────
_SYNTH_ROWS = [
    {"counterfactual_input_tokens": 1000, "billed_input_tokens": 100,
     "billed_output_tokens": 50, "billed_cache_read_tokens": 200,
     "billed_cache_write_tokens": 10},
    # null-probe row: excluded from tokens_saved, still counts billed + fail_open
    {"counterfactual_input_tokens": None, "billed_input_tokens": 80,
     "billed_output_tokens": 40, "billed_cache_read_tokens": 0,
     "billed_cache_write_tokens": 0, "fail_open": True},
    {"counterfactual_input_tokens": 500, "billed_input_tokens": 50,
     "billed_output_tokens": 25, "billed_cache_read_tokens": 100,
     "billed_cache_write_tokens": 5},
]


def test_summarize_savings_ledger_math():
    t = summarize_savings_ledger(_SYNTH_ROWS)
    assert t["contract_version"] == "savings-ledger/v0"
    assert t["requests"] == 3
    assert t["probed_requests"] == 2
    assert t["null_probe_requests"] == 1
    assert t["counterfactual_input_tokens"] == 1500
    # served = billed_input + cache_read + cache_write, summed over all 3 rows
    assert t["served_input_tokens"] == 310 + 80 + 155
    assert t["billed_input_tokens"] == 230
    assert t["billed_output_tokens"] == 115
    assert t["billed_cache_read_tokens"] == 300
    assert t["billed_cache_write_tokens"] == 15
    # tokens_saved from PROBED rows only: (1000-310) + (500-155)
    assert t["tokens_saved"] == 690 + 345
    assert t["fail_opens"] == 1
    assert abs(t["savings_rate"] - (1035 / 1500)) < 1e-9


def test_ledger_summary_reads_file(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setenv("PARSEC_PLUGIN_DIR", _make_plugin_dir(tmp_path))
    led = tmp_path / "ledger.jsonl"
    led.write_text("\n".join(json.dumps(r) for r in _SYNTH_ROWS) + "\n")
    arm = ParsecArm()
    arm._ledger_path = str(led)
    s = arm.ledger_summary("rid-x")
    assert s["requests"] == 3
    assert s["tokens_saved"] == 1035


def test_ledger_summary_empty_when_no_ledger(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setenv("PARSEC_PLUGIN_DIR", _make_plugin_dir(tmp_path))
    arm = ParsecArm()
    assert arm.ledger_summary("rid-x") == {}
