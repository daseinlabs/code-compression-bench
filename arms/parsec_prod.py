"""parsec_prod — the SHIPPED parsec product with ZERO harness interposition
(ProxyArm + plugin, raw product).

This arm is for INTERNAL measurement: the product exactly as a user runs it. No
CCB gateway, no harness hooks, no bridge token, no run-id header — the fullest
headroom. Claude Code:

  * loads the parsec plugin (same PARSEC_PLUGIN_DIR mechanism as the ``parsec`` arm);
  * points ANTHROPIC_BASE_URL at the box's LONG-RUNNING user-level parsec proxy
    started by the product's own setup (env ``PARSEC_PROD_BASE_URL``), whose
    upstream is ``api.anthropic.com`` directly with the real key;
  * is handed the REAL ``ANTHROPIC_API_KEY`` (not the dummy bridge token) so the
    prod proxy can authenticate upstream;
  * runs with ``max_turns=None`` (CALL_CAP override → unlimited).

Cost/usage for this arm comes from (a) the SDK result's ``total_cost_usd`` +
usage (recorded on the RunRecord as ``reported_cost_usd``) and (b) the product
ledger ``$HOME/.parsec/ledger.jsonl`` filtered by this run's conversation id.
The prod proxy keys conversations by its own id (the CC session), NOT the CCB
run-id — mapping that id to a run is a box-smoke item; we record ``conv_id`` as
the CCB run tag and filter the ledger by it best-effort (see README).

Grade with the same official grader; RunRecords are marked ``arm=parsec_prod``.

ENV:
  PARSEC_PROD_BASE_URL   base URL of the box's long-running parsec proxy
                         (e.g. http://127.0.0.1:PORT). REQUIRED.
  PARSEC_PLUGIN_DIR      installed parsec Claude Code plugin dir. REQUIRED.
  ANTHROPIC_API_KEY      the REAL key, threaded to Claude Code for this arm. REQUIRED.
  PARSEC_PROD_LEDGER     product ledger path (default: $HOME/.parsec/ledger.jsonl).
  PARSEC_BIN             optional; used only to record the binary version.
  PARSEC_PROXY_STATUS_PATH  optional status route to read the brain checkpoint_id.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from bench.arm import ProxyArm, register

from arms.parsec import (
    _parsec_version,
    _probe_checkpoint,
    _read_ledger_rows,
    _resolve_bin,
    summarize_savings_ledger,
)


def _prod_ledger_path() -> str:
    return os.environ.get(
        "PARSEC_PROD_LEDGER",
        str(Path(os.path.expanduser("~")) / ".parsec" / "ledger.jsonl"),
    )


@register("parsec_prod")
class ParsecProdArm(ProxyArm):
    name = "parsec_prod"
    needs = ["PARSEC_PROD_BASE_URL", "PARSEC_PLUGIN_DIR", "ANTHROPIC_API_KEY"]

    # ZERO harness interposition + unlimited turns (see module docstring).
    raw_product = True
    unlimited_turns = True

    def __init__(self) -> None:
        self.plugin_dir = os.environ.get("PARSEC_PLUGIN_DIR")
        self.plugin_tool_globs = ["mcp__plugin_parsec_*__*", "mcp__parsec*__*"]
        self.replace_tools = False
        self.checkpoint_id = ""
        self.parsec_version = ""
        self.conv_id = ""

    def model_base_url(self) -> str:
        # The box's long-running product proxy; its upstream is api.anthropic.com
        # directly with the real key (configured by the product's own setup).
        return os.environ.get("PARSEC_PROD_BASE_URL", "").rstrip("/")

    def headers(self) -> dict[str, str]:
        return {}

    def ready(self) -> tuple[bool, str]:
        ok, reason = super().ready()  # PARSEC_PROD_BASE_URL + PLUGIN_DIR + API_KEY set
        if not ok:
            return ok, reason
        plugin_dir = self.plugin_dir or ""
        if not os.path.isfile(os.path.join(plugin_dir, ".claude-plugin", "plugin.json")):
            return False, (f"PARSEC_PLUGIN_DIR={plugin_dir!r} is not an installed parsec "
                           f"plugin (missing .claude-plugin/plugin.json)")
        base = self.model_base_url()
        u = urlparse(base)
        host = u.hostname or "127.0.0.1"
        port = u.port or (443 if u.scheme == "https" else 80)
        try:
            with socket.create_connection((host, port), timeout=3):
                pass
        except OSError as e:
            return False, (f"prod parsec proxy not reachable at {host}:{port} "
                           f"({e.__class__.__name__}) — start it via the product setup "
                           f"(parsec:setup) before running this arm")
        bin_path = _resolve_bin()
        if bin_path:
            self.parsec_version = _parsec_version(bin_path)
        # best-effort brain checkpoint (the prod proxy's status surface is a box-smoke item)
        try:
            _, ckpt = _probe_checkpoint(base)
            if ckpt:
                self.checkpoint_id = ckpt
        except Exception:
            pass
        return True, (f"ok (prod proxy={base}, plugin={plugin_dir}, "
                      f"ckpt={self.checkpoint_id or '?'}, ver={self.parsec_version or '?'})")

    def start_run(self, ctx) -> Optional[str]:
        # No proxy to spawn (the product proxy is already running); record the run
        # tag as our conv id. Return None -> client_base_url stays model_base_url().
        self.conv_id = ctx.run_id
        return None

    def ledger_path(self) -> Optional[str]:
        # Do NOT copy the shared box ledger wholesale (it mixes every run); the
        # summary reads + filters it in place instead.
        return None

    def ledger_summary(self, run_id: str = "") -> dict:
        path = _prod_ledger_path()
        if not os.path.exists(path):
            return {}
        rows = _read_ledger_rows(path, conv_id=self.conv_id or run_id)
        if not rows:
            return {}
        return summarize_savings_ledger(rows)
