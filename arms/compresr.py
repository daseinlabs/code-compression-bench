"""compresr — Compresr "Context Gateway" (ProxyArm).

Compresr ships an open-source, OpenAI-compatible proxy ("Context Gateway", a Go
binary) that sits between the agent and the model, compacting tool outputs and
conversation history before they reach the model. We run it as a proxy and point
litellm at it; a Compresr API key (cmp_...) authenticates when using their hosted
gateway (omit it for a purely local self-host).

Clean-room: this arm only holds a base_url + optional auth header. All compression
happens inside Compresr's gateway, on the other side of the wire.

Env:
  COMPRESR_BASE_URL  — base URL of the Context Gateway proxy (default http://127.0.0.1:8804).
  COMPRESR_API_KEY   — optional cmp_ key for the hosted gateway (omit for local self-host).

Self-host: see selfhost/docker-compose.yml and arms/README.md.
"""

from __future__ import annotations

import os

from bench.arm import ProxyArm, register

DEFAULT_BASE_URL = "http://127.0.0.1:8804"


@register("compresr")
class CompresrArm(ProxyArm):
    name = "compresr"
    # Require the base URL explicitly so we never silently hit a dead port; the
    # key is optional (only needed for the hosted gateway).
    needs = ["COMPRESR_BASE_URL"]

    def model_base_url(self) -> str:
        return os.environ.get("COMPRESR_BASE_URL", DEFAULT_BASE_URL).rstrip("/")

    def headers(self) -> dict[str, str]:
        key = os.environ.get("COMPRESR_API_KEY", "")
        return {"Authorization": f"Bearer {key}"} if key else {}
