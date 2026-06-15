"""bear — The Token Company compression API (TransformArm).

bear rewrites the message array client-side: it POSTs the chat messages to the
Token Company compress API with a target ratio (COMPRESSION_TARGET_RATIO,
default 0.5), gets back a shorter message array, and hands that to the scaffold
which then calls the model on the NORMAL endpoint. The arm never touches the
model itself — only the prompt.

Clean-room: this is a generic HTTP client against a public vendor API. No
proprietary compression logic lives here; the compression happens on bear's
servers.

Env:
  BEAR_API_KEY   — bearer token for the compress API.
  BEAR_BASE_URL  — base URL of the Token Company API.
  COMPRESSION_TARGET_RATIO — target output/input ratio in (0, 1]. Default 0.5.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from bench.arm import Messages, TransformArm, register

# compress endpoint path appended to BEAR_BASE_URL
_COMPRESS_PATH = "/v1/compress"
_TIMEOUT_S = 60


def _target_ratio() -> float:
    raw = os.environ.get("COMPRESSION_TARGET_RATIO", "0.5")
    try:
        r = float(raw)
    except ValueError:
        return 0.5
    # clamp to a sane (0, 1] band; >1 would mean "expand", which is meaningless here
    if r <= 0:
        return 0.5
    return min(r, 1.0)


@register("bear")
class BearArm(TransformArm):
    name = "bear"
    needs = ["BEAR_API_KEY", "BEAR_BASE_URL"]

    def transform(self, messages: Messages) -> Messages:
        """Compress the message array via the bear API; fall back to the input
        unchanged on any error so a transient API hiccup never silently drops a
        run's prompt (the runner records the call either way)."""
        base = os.environ.get("BEAR_BASE_URL", "").rstrip("/")
        key = os.environ.get("BEAR_API_KEY", "")
        if not base or not key:
            return messages

        payload = json.dumps({
            "messages": messages,
            "target_ratio": _target_ratio(),
            "model": os.environ.get("MODEL", ""),
        }).encode("utf-8")
        req = urllib.request.Request(
            base + _COMPRESS_PATH,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            # network error / bad JSON: degrade to identity, do not crash the run
            return messages

        # Accept the canonical {"messages": [...]} response; tolerate a bare list.
        out = body.get("messages") if isinstance(body, dict) else body
        if isinstance(out, list) and out:
            return out
        return messages
