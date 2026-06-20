"""compresr — Compresr "Context Gateway" (ProxyArm).

Compresr (compresr.ai, YC W2026; github.com/Compresr-ai/Context-Gateway, Go,
Apache-2.0) is an open-source reverse proxy that compacts conversation history +
tool outputs before they reach the model. It is **Anthropic-native** (also
OpenAI/Gemini/Bedrock): it detects format and routes ``/v1/messages``-shaped
Anthropic traffic, so Claude Code's content blocks / ``tool_use`` / ``tool_result``
/ ``cache_control`` pass through. Documented Claude Code path:
``ANTHROPIC_BASE_URL=http://localhost:18081 claude``.

PROVISIONING (the bench topology): the gateway's UPSTREAM Anthropic endpoint must
be THIS run's gateway, set on the Compresr process via
``ANTHROPIC_PROVIDER_URL=<gateway_url>`` (default ``https://api.anthropic.com``).
Chain: ClaudeCode -> Compresr (compacts) -> our gateway -> Vertex.

COST CAVEAT (measurement): Compresr's preemptive summarizer itself calls an LLM
(claude-haiku-4-5) by egressing to ``api.compresr.ai`` — that leg does NOT pass
through our bottom-bridge gateway, so its tokens/cost are NOT captured in this
arm's usage rows. Note it when reading Compresr's cost numbers.

Clean-room: this arm only holds the gateway base_url. ``COMPRESR_BASE_URL`` /
``COMPRESR_API_KEY`` in the real product are the *compression-service* upstream
credentials (the gateway's call OUT to api.compresr.ai), configured ON THE GATEWAY
at provisioning — NOT a bearer the client sends. We therefore expose the local
gateway endpoint as COMPRESR_GATEWAY_URL and do not inject a client bearer.

Env:
  COMPRESR_GATEWAY_URL — base URL of the local Context Gateway proxy Claude Code
                         points at (default http://127.0.0.1:18081 — the product's
                         real default port; was 8804 — audit gap).
"""

from __future__ import annotations

import os
import socket
from urllib.parse import urlparse

from bench.arm import ProxyArm, register

# Compresr's real default gateway port is 18081 (cmd/configs/fast_setup.yaml). (Was 8804.)
DEFAULT_BASE_URL = "http://127.0.0.1:18081"


@register("compresr")
class CompresrArm(ProxyArm):
    name = "compresr"
    # The local gateway endpoint Claude Code points at. COMPRESR_BASE_URL/_API_KEY
    # (the compression-service creds) live ON THE GATEWAY, not here.
    needs = ["COMPRESR_GATEWAY_URL"]

    def model_base_url(self) -> str:
        return os.environ.get(
            "COMPRESR_GATEWAY_URL",
            os.environ.get("COMPRESR_BASE_URL", DEFAULT_BASE_URL),
        ).rstrip("/")

    def headers(self) -> dict[str, str]:
        # The gateway uses Claude Code's own auth (skip_api_key_setup); it does NOT
        # expect a cmp_ bearer from the client. No client header.
        return {}

    def ready(self) -> tuple[bool, str]:
        """Ready iff the gateway endpoint is set AND actually listening (real TCP
        probe), so a dead/un-launched gateway SKIPs instead of burning paid runs."""
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
                f"Compresr gateway not reachable at {host}:{port} ({e.__class__.__name__}). "
                f"Launch the Context Gateway (GATEWAY_PORT={port}) with "
                f"ANTHROPIC_PROVIDER_URL=<gateway_url>."
            )
