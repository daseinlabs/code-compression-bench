"""headroom — open-source self-hosted context-compression proxy (ProxyArm).

Headroom (github.com/chopratejas/headroom, PyPI ``headroom-ai``) is an
open-source "Context Optimization Layer". Its proxy is **dual-protocol** and
**natively handles the Anthropic Messages API** at ``POST /v1/messages`` (as well
as OpenAI ``/v1/chat/completions``): Claude Code's native content blocks /
``tool_use`` / ``tool_result`` / ``cache_control`` pass through unchanged — it
"only modifies the input messages". So the documented Claude Code path is exactly
``ANTHROPIC_BASE_URL=<headroom> claude`` — NO client-side translation is required
(an earlier note calling it "OpenAI-compatible only" was WRONG; audit-corrected).
Compression is real: the 6-signal IntelligentContext scorer (recency, semantic
similarity, error indicators, forward references, token density, learned TOIN)
plus SmartCrusher/CodeCompressor.

Launch (self-host): ``pip install "headroom-ai[all]"`` then
``headroom proxy --port 8787 --mode token --anthropic-api-url <UPSTREAM>``.

PROVISIONING (the bench topology): Headroom's UPSTREAM Anthropic endpoint must be
THIS run's gateway, set via ``ANTHROPIC_TARGET_API_URL=<gateway_url>`` (or the
``--anthropic-api-url`` flag). The gateway speaks Anthropic on its front, so the
chain is  ClaudeCode -> Headroom (compresses /v1/messages) -> gateway -> Vertex.

Env:
  HEADROOM_BASE_URL  — base URL of the local Headroom proxy (default
                       http://127.0.0.1:8787 — the product's real default port).
"""

from __future__ import annotations

import os
import socket
from urllib.parse import urlparse

from bench.arm import ProxyArm, register

# Headroom's real default proxy port is 8787 (docs/CLI). (Was 8803 — audit gap.)
DEFAULT_BASE_URL = "http://127.0.0.1:8787"


@register("headroom")
class HeadroomArm(ProxyArm):
    name = "headroom"
    needs = ["HEADROOM_BASE_URL"]

    def model_base_url(self) -> str:
        return os.environ.get("HEADROOM_BASE_URL", DEFAULT_BASE_URL).rstrip("/")

    def headers(self) -> dict[str, str]:
        # Self-hosted: the upstream (our gateway) is configured INSIDE Headroom via
        # ANTHROPIC_TARGET_API_URL / --anthropic-api-url, not as a client header.
        return {}

    def ready(self) -> tuple[bool, str]:
        """Ready iff HEADROOM_BASE_URL is set AND the proxy is actually listening —
        a real TCP probe, so a dead/un-launched proxy SKIPs cleanly instead of
        passing the gate and infra-failing every paid run (audit gap)."""
        ok, reason = super().ready()
        if not ok:
            return ok, reason
        u = urlparse(self.model_base_url())
        host, port = u.hostname or "127.0.0.1", u.port or (443 if u.scheme == "https" else 80)
        try:
            with socket.create_connection((host, port), timeout=2):
                return True, "ok"
        except OSError as e:
            return False, (
                f"Headroom proxy not reachable at {host}:{port} ({e.__class__.__name__}). "
                f"Launch it: headroom proxy --port {port} --mode token "
                f"--anthropic-api-url <gateway_url>."
            )
