"""rtk — open-source CLI proxy, self-hosted (ProxyArm).

RTK is an open-source CLI proxy you launch locally; it presents an
OpenAI-compatible endpoint, compresses the prompt, and forwards to the shared
model (OPENAI_BASE_URL / MODEL). This arm only routes litellm at the local
proxy; see selfhost/docker-compose.yml and arms/README.md for launch notes.

Env:
  RTK_BASE_URL  — base URL of the local RTK proxy (default http://127.0.0.1:8802).
"""

from __future__ import annotations

import os

from bench.arm import ProxyArm, register

DEFAULT_BASE_URL = "http://127.0.0.1:8802"


@register("rtk")
class RtkArm(ProxyArm):
    name = "rtk"
    needs = ["RTK_BASE_URL"]

    def model_base_url(self) -> str:
        return os.environ.get("RTK_BASE_URL", DEFAULT_BASE_URL).rstrip("/")

    def headers(self) -> dict[str, str]:
        # Self-hosted: upstream auth is configured in the proxy itself.
        return {}
