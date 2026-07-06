"""edgee — Edgee open-source AI gateway, self-hosted (ProxyArm).

Edgee (github.com/edgee-ai/edgee, Apache-2.0) is an open-source AI gateway written
in Rust with a token-compression engine for Claude Code / Codex. Its
``edgee local-gateway`` (and the ``--local-gateway`` flag on ``edgee launch claude``)
runs a headless HTTP gateway that routes ``POST /v1/messages`` through Edgee's
**Anthropic passthrough** + the Claude ``CompressionLayer``: it is **Anthropic-native**,
so Claude Code's content blocks / ``tool_use`` / ``tool_result`` / ``cache_control``
pass through **unchanged** (the passthrough service forwards the request body + headers
verbatim and only compresses tool-output noise). The documented Claude Code path is
``edgee launch claude`` / ``ANTHROPIC_BASE_URL=<local-gateway> claude``.

FORK (one surgical change — see selfhost/edgee/): real Edgee's local gateway hardcodes
the Anthropic upstream to ``https://api.anthropic.com``. The passthrough config has
always exposed a ``with_base_url`` override (unit-tested upstream), but
``local_gateway::start()`` built the service with the DEFAULT config, so the upstream
could not be retargeted. Our fork (selfhost/edgee/anthropic_upstream.patch, pinned to
edgee-cli 0.2.9) wires that existing override into ``start()`` from the env var
``EDGEE_ANTHROPIC_UPSTREAM`` — nothing else changes (the compression engine + passthrough
are the real product). Build it with ``selfhost/edgee/build.sh``.

PROVISIONING (the bench topology): launch the forked gateway with
``EDGEE_ANTHROPIC_UPSTREAM=<gateway_url>`` (the run gateway the runner prints), e.g.
``GATEWAY_URL=<gateway_url> EDGEE_PORT=8787 bash selfhost/edgee/launch.sh``. The chain is
``Claude Code -> edgee local-gateway (compresses /v1/messages) -> gateway -> Vertex``.

Env:
  EDGEE_BASE_URL  — base URL of the local Edgee gateway Claude Code points at
                    (default http://127.0.0.1:8787 — edgee local-gateway's real
                    default port; was 8801 — a fictitious port, audit gap).
"""

from __future__ import annotations

import os
import socket
from urllib.parse import urlparse

from bench.arm import ProxyArm, register

# edgee local-gateway's real default port is 8787 (crates/cli/src/commands/
# local_gateway.rs, default_value_t = 8787). (Was 8801 — fictitious; audit gap.)
DEFAULT_BASE_URL = "http://127.0.0.1:8787"


@register("edgee")
class EdgeeArm(ProxyArm):
    name = "edgee"
    # Self-hosted, no API key: the local gateway holds no model key (its UPSTREAM —
    # our run gateway — is configured via EDGEE_ANTHROPIC_UPSTREAM at launch, and the
    # gateway bridges to Vertex via ADC). Require the base URL explicitly so a dead
    # port can't be silently hit.
    needs = ["EDGEE_BASE_URL"]

    def model_base_url(self) -> str:
        return os.environ.get("EDGEE_BASE_URL", DEFAULT_BASE_URL).rstrip("/")

    def headers(self) -> dict[str, str]:
        # The forked local gateway forwards the client's own auth verbatim (the
        # passthrough path injects no credentials); the upstream (our gateway) is
        # configured INSIDE Edgee via EDGEE_ANTHROPIC_UPSTREAM, not as a client header.
        return {}

    def ready(self) -> tuple[bool, str]:
        """Ready iff EDGEE_BASE_URL is set AND the local gateway is actually listening
        (a real TCP probe), so a dead/un-launched gateway SKIPs cleanly with a launch
        hint instead of passing the env gate and infra-failing every paid run."""
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
                f"Edgee local gateway not reachable at {host}:{port} ({e.__class__.__name__}). "
                f"Build the fork (selfhost/edgee/build.sh) then launch it: "
                f"GATEWAY_URL=<gateway_url> EDGEE_PORT={port} bash selfhost/edgee/launch.sh."
            )
