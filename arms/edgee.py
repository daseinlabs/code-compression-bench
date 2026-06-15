"""edgee — open-source Rust gateway, self-hosted (ProxyArm).

Edgee is an open-source edge/data gateway you run yourself. Configured as a
compression proxy it exposes an OpenAI-compatible endpoint, compresses the
prompt, and forwards to the shared model (OPENAI_BASE_URL / MODEL). This arm
just routes litellm at the local gateway; see selfhost/docker-compose.yml and
arms/README.md for how to launch it.

Env:
  EDGEE_BASE_URL  — base URL of the local Edgee proxy (default http://127.0.0.1:8801).
"""

from __future__ import annotations

import os

from bench.arm import ProxyArm, register

DEFAULT_BASE_URL = "http://127.0.0.1:8801"


@register("edgee")
class EdgeeArm(ProxyArm):
    name = "edgee"
    # Self-hosted: no API key needed. ready() falls back to the default URL, so
    # we require the env var explicitly to avoid silently hitting a dead port.
    needs = ["EDGEE_BASE_URL"]

    def model_base_url(self) -> str:
        return os.environ.get("EDGEE_BASE_URL", DEFAULT_BASE_URL).rstrip("/")

    def headers(self) -> dict[str, str]:
        # Self-hosted proxy authenticates upstream with its own configured
        # OPENAI_API_KEY (see docker-compose); the client needs no auth header.
        return {}
