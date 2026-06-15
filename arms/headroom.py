"""headroom — open-source, LiteLLM-native reversible compression, self-hosted (ProxyArm).

Headroom is an open-source, OpenAI-compatible compression layer with a
LiteLLM-native integration. Its compression is reversible (lossless round-trip
of the message array). Self-hosted, it exposes an OpenAI-compatible endpoint,
compresses the prompt, and forwards to the shared model. This arm routes
litellm at the local Headroom endpoint; see selfhost/docker-compose.yml and
arms/README.md for launch notes.

Env:
  HEADROOM_BASE_URL  — base URL of the local Headroom endpoint
                       (default http://127.0.0.1:8803).
"""

from __future__ import annotations

import os

from bench.arm import ProxyArm, register

DEFAULT_BASE_URL = "http://127.0.0.1:8803"


@register("headroom")
class HeadroomArm(ProxyArm):
    name = "headroom"
    needs = ["HEADROOM_BASE_URL"]

    def model_base_url(self) -> str:
        return os.environ.get("HEADROOM_BASE_URL", DEFAULT_BASE_URL).rstrip("/")

    def headers(self) -> dict[str, str]:
        # Self-hosted: upstream model auth is configured inside Headroom.
        return {}
