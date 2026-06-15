"""dasein — hosted, keyed compression service (ProxyArm).

Dasein runs an OpenAI-compatible endpoint that compresses the prompt
server-side (the proprietary curator/GNN lives there, NOT in this repo) and
forwards to the underlying model. This module is a THIN client only: it points
litellm at DASEIN_BASE_URL and attaches the API key as a bearer header. No
compression logic, no model logic — just routing + auth.

Env:
  DASEIN_API_KEY   — bearer token for the hosted service (dsk_...).
  DASEIN_BASE_URL  — OpenAI-compatible base URL of the Dasein endpoint.
"""

from __future__ import annotations

import os

from bench.arm import ProxyArm, register


@register("dasein")
class DaseinArm(ProxyArm):
    name = "dasein"
    needs = ["DASEIN_API_KEY", "DASEIN_BASE_URL"]

    def model_base_url(self) -> str:
        # Stripped of trailing slash so litellm/openai path joins are clean.
        return os.environ.get("DASEIN_BASE_URL", "").rstrip("/")

    def headers(self) -> dict[str, str]:
        key = os.environ.get("DASEIN_API_KEY", "")
        # Send the key both ways: standard OpenAI bearer + a vendor header, so
        # the gateway can authenticate however it expects without a client change.
        return {
            "Authorization": f"Bearer {key}",
            "X-Dasein-Api-Key": key,
        }
