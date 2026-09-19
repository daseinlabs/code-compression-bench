"""Wiring/readiness tests for the parsec_prod arm (raw product, zero interposition).

Pure stdlib + monkeypatch — no real proxy, no SDK. Covers readiness (reachable
prod proxy + entitlement-shaped gates), the raw_product/unlimited_turns policy,
proxy+plugin config building, and conv-id-filtered ledger summary.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from contextlib import closing
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from arms import parsec_prod as ppmod  # noqa: E402
from arms.parsec_prod import ParsecProdArm  # noqa: E402
from bench.arm import ArmKind  # noqa: E402
from bench.cc_runner import build_arm_config  # noqa: E402

_NEEDED = ("PARSEC_PROD_BASE_URL", "PARSEC_PLUGIN_DIR", "ANTHROPIC_API_KEY",
           "PARSEC_PROD_LEDGER", "PARSEC_BIN", "PARSEC_PROXY_STATUS_PATH")


def _clear(monkeypatch):
    for k in _NEEDED:
        monkeypatch.delenv(k, raising=False)


def _make_plugin_dir(tmp_path) -> str:
    d = tmp_path / "parsec_plugin"
    (d / ".claude-plugin").mkdir(parents=True)
    (d / ".claude-plugin" / "plugin.json").write_text('{"name": "parsec"}')
    return str(d)


def _closed_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # now closed -> connections refused
    return port


def test_policy_flags():
    arm = ParsecProdArm()
    assert arm.raw_product is True
    assert arm.unlimited_turns is True
    assert arm.kind == ArmKind.PROXY


def test_ready_skips_without_env(monkeypatch):
    _clear(monkeypatch)
    ok, reason = ParsecProdArm().ready()
    assert not ok
    assert "missing env" in reason
    assert "ANTHROPIC_API_KEY" in reason


def test_ready_skips_when_proxy_unreachable(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setenv("PARSEC_PLUGIN_DIR", _make_plugin_dir(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setenv("PARSEC_PROD_BASE_URL", f"http://127.0.0.1:{_closed_port()}")
    ok, reason = ParsecProdArm().ready()
    assert not ok
    assert "not reachable" in reason


def test_ready_ok_when_proxy_listening(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setenv("PARSEC_PLUGIN_DIR", _make_plugin_dir(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setattr(ppmod, "_probe_checkpoint", lambda base: (True, "sha256:live"))
    monkeypatch.setattr(ppmod, "_resolve_bin", lambda: None)
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        monkeypatch.setenv("PARSEC_PROD_BASE_URL", f"http://127.0.0.1:{port}")
        arm = ParsecProdArm()
        ok, reason = arm.ready()
    assert ok, reason
    assert arm.checkpoint_id == "sha256:live"


def test_build_arm_config_points_at_prod_proxy_with_plugin(monkeypatch, tmp_path):
    _clear(monkeypatch)
    plugin = _make_plugin_dir(tmp_path)
    monkeypatch.setenv("PARSEC_PLUGIN_DIR", plugin)
    monkeypatch.setenv("PARSEC_PROD_BASE_URL", "http://127.0.0.1:9911")
    arm = ParsecProdArm()
    cfg = build_arm_config(arm)
    # ANTHROPIC_BASE_URL -> the box's prod proxy (model_base_url)
    assert cfg.client_base_url == "http://127.0.0.1:9911"
    assert cfg.plugins == [{"type": "local", "path": os.path.abspath(plugin)}]
    assert "mcp__plugin_parsec_*__*" in cfg.allowed_tools
    assert cfg.disallowed_tools == []  # we disallow nothing (full headroom)


def test_start_run_returns_none_and_records_conv_id(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setenv("PARSEC_PLUGIN_DIR", _make_plugin_dir(tmp_path))

    class _Ctx:
        upstream_base_url = "http://gw"
        run_dir = str(tmp_path)
        run_id = "iid_run_parsec_prod"

    arm = ParsecProdArm()
    override = arm.start_run(_Ctx())
    assert override is None  # uses model_base_url(), no per-solve proxy to spawn
    assert arm.conv_id == "iid_run_parsec_prod"
    assert arm.ledger_path() is None  # never copies the shared box ledger


def test_ledger_summary_filters_by_conv_id(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setenv("PARSEC_PLUGIN_DIR", _make_plugin_dir(tmp_path))
    led = tmp_path / "prod_ledger.jsonl"
    rows = [
        {"conv_id": "run-A", "counterfactual_input_tokens": 300,
         "billed_input_tokens": 50, "billed_cache_read_tokens": 50,
         "billed_cache_write_tokens": 0, "billed_output_tokens": 10},
        {"conv_id": "run-B", "counterfactual_input_tokens": 999,
         "billed_input_tokens": 900, "billed_output_tokens": 10},
    ]
    led.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    monkeypatch.setenv("PARSEC_PROD_LEDGER", str(led))
    arm = ParsecProdArm()
    arm.conv_id = "run-A"
    s = arm.ledger_summary("run-A")
    # only run-A's row is summed
    assert s["requests"] == 1
    assert s["counterfactual_input_tokens"] == 300
    assert s["served_input_tokens"] == 100
    assert s["tokens_saved"] == 200
