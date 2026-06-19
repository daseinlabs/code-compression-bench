"""A logging bridge for the Anthropic Messages API with TWO upstream modes.

WHY THIS EXISTS
---------------
The bench drives headless Claude Code (via the Python Claude Agent SDK) as the
ONE fixed agent for every arm. The SDK reports a run-level cost
(``ResultMessage.total_cost_usd``) and a coarse token ``usage`` dict, but it does
NOT expose the PER-REQUEST cache split (cache_creation / cache_read) that our
cache-aware pricing (``bench.pricing``) and the gate2 KPI bundle want. The model
provider DOES report that split on every ``/v1/messages`` response — we just
never see it, because Claude Code talks to the model directly.

So we interpose ONE gateway, and it is the SINGLE BOTTOM BRIDGE to the model.
Claude Code (or a vendor compression proxy ABOVE us) talks Anthropic
``/v1/messages`` to this gateway; we capture the post-compression usage off the
response and append one ``schema.CallUsage`` row per request to a per-run JSONL,
keyed by an ``x-ccb-run-id`` header. Because we sit at the BOTTOM of the chain we
always see the REAL usage the model billed — even when a vendor proxy compressed
the prompt above us.

TWO UPSTREAM MODES (env/arg ``CCB_GATEWAY_MODE``; default ``vertex``)
--------------------------------------------------------------------
  * ``vertex``      — the gateway IS the Vertex bridge. It receives Anthropic
                      ``/v1/messages`` (+ ``/v1/messages/count_tokens``) and calls
                      ``litellm.completion(model="vertex_ai/claude-sonnet-4-6",
                      vertex_project=..., vertex_location=..., ...)`` with ADC auth
                      (no API key). The inbound Anthropic request is translated to
                      litellm args; the litellm response is translated back to an
                      Anthropic Messages response (JSON or SSE). This is the seam
                      that lets the WHOLE chain speak Anthropic (Claude Code and
                      the vendor proxies only speak the Anthropic API) while the
                      real model runs claude-sonnet on Vertex via ADC.
  * ``passthrough`` — the original behaviour: forward each request verbatim to a
                      configured upstream base URL (urllib), a passive observer of
                      the wire. Used for an Anthropic-direct upstream or to chain
                      to another Anthropic-speaking endpoint.

WHAT IT CAPTURES (Anthropic usage shape)
----------------------------------------
From each response's ``usage`` object (non-streaming JSON body, OR the final
``message_delta``/``message_start`` of an SSE stream — or, in Vertex mode, the
litellm response's ``usage``):
  - input_tokens                -> uncached new input (Anthropic reports the
                                   UNCACHED portion here, cache split separate)
  - output_tokens               -> completion tokens
  - cache_creation_input_tokens -> cache WRITE
  - cache_read_input_tokens     -> cache READ
We normalize ``prompt_tokens`` to the FULL billable input (uncached + write +
read) so it matches the contract ``pricing.real_cache_cost`` expects — the same
normalization ``bench.runner`` did off the litellm usage object.

ROBUSTNESS (a paid run must never be hung by the gateway)
---------------------------------------------------------
Every failure mode degrades safely. In passthrough mode a parse error, a non-JSON
body, an unexpected content-type, a usage object the upstream didn't send — none
of these break the proxied call; the client gets the upstream's bytes back
unchanged and we merely fail to LOG a usage row. In Vertex mode a translation or
litellm error returns a clean Anthropic-shaped error response (never a hang), and
we still log whatever usage we managed to read. Streaming responses are streamed
through so the SDK sees tokens as they arrive; we tee the bytes to a usage parser
on the side.

CLEAN-ROOM
----------
The module itself is pure stdlib (``http.server``, ``urllib``, ``json``,
``threading``). It has NO ``adaptive_context`` import and NO vendor SDK. The ONE
heavy dependency — ``litellm`` — is imported LAZILY, only on the first Vertex-mode
request, so ``import bench.usage_gateway`` / ``import bench.cc_runner`` and the
passthrough mode work on a box without litellm installed. ``bench.schema`` is
imported only for the ``CallUsage`` TypedDict shape (stdlib dataclasses/typing).
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

# CallUsage is a TypedDict (pure typing) — importing it pulls in no heavy deps.
from bench.schema import CallUsage

# ── upstream modes ────────────────────────────────────────────────────────────
MODE_VERTEX = "vertex"            # gateway IS the litellm->Vertex bridge (default)
MODE_PASSTHROUGH = "passthrough"  # gateway forwards verbatim to an upstream URL

# ── Vertex defaults (claude-sonnet on Vertex via litellm + ADC) ──────────────
# Auth is Application Default Credentials on the box (gcloud auth application-default
# login) — NO API key. project/location are overridable via env to retarget without
# a code change; the model id is the Vertex Claude id litellm routes to Vertex.
VERTEX_MODEL = os.environ.get("VERTEX_MODEL", "vertex_ai/claude-sonnet-4-6")
VERTEX_PROJECT = os.environ.get("VERTEX_PROJECT", "dasein-473321")
VERTEX_LOCATION = os.environ.get("VERTEX_LOCATION", "us-east5")


# ── header for the run-id tag ────────────────────────────────────────────────
# Claude Code forwards ANTHROPIC_CUSTOM_HEADERS onto every model request, so the
# runner sets a run-id header there and the gateway reads it to TAG each usage
# row into the right per-run JSONL. (Case-insensitive: http.server lowercases.)
RUN_ID_HEADER = "x-ccb-run-id"

# Hop-by-hop headers we must NOT forward (RFC 7230 §6.1) — plus Host (we set our
# own to the upstream) and Content-Length (recomputed by urllib from the body).
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
    "host", "content-length", "accept-encoding",
}


# ── usage extraction (the cache split, normalized) ───────────────────────────
def _i(d: dict, key: str) -> int:
    """Read an int field off a usage dict, treating missing/None as 0."""
    v = d.get(key)
    return int(v or 0)


def extract_usage(usage: Optional[dict], latency_s: float = 0.0) -> Optional[CallUsage]:
    """One CallUsage row from an Anthropic ``usage`` object.

    Anthropic's usage shape (the ``usage`` object on a ``/v1/messages`` response,
    or on the ``message_start``/``message_delta`` SSE events):
        {"input_tokens": N,                  # uncached NEW input only
         "output_tokens": M,
         "cache_creation_input_tokens": W,   # cache WRITE
         "cache_read_input_tokens": R}       # cache READ

    We emit ``prompt_tokens`` as the FULL billable input (uncached + write +
    read), the contract ``pricing.real_cache_cost`` expects. Cache keys are
    emitted only when the provider reported them (presence is the signal pricing
    uses to pick the real-cache path); absent fields are omitted.

    Returns ``None`` if ``usage`` is missing or carries no token data at all —
    the caller then logs nothing (passthrough).
    """
    if not isinstance(usage, dict):
        return None
    inp = _i(usage, "input_tokens")
    out = _i(usage, "output_tokens")
    has_write = usage.get("cache_creation_input_tokens") is not None
    has_read = usage.get("cache_read_input_tokens") is not None
    # nothing usable — don't fabricate a row
    if inp == 0 and out == 0 and not has_write and not has_read:
        return None

    write = _i(usage, "cache_creation_input_tokens")
    read = _i(usage, "cache_read_input_tokens")
    # Anthropic reports input_tokens as the UNCACHED portion; fold the cache
    # buckets back in so prompt_tokens == full billable input.
    row: CallUsage = {
        "prompt_tokens": inp + write + read,
        "completion_tokens": out,
        "latency_s": round(latency_s, 3),
    }
    if has_write:
        row["cache_creation_input_tokens"] = write
    if has_read:
        row["cache_read_input_tokens"] = read
    return row


def usage_from_sse(body_text: str) -> Optional[dict]:
    """Reconstruct the final Anthropic ``usage`` from a raw SSE stream body.

    The Messages API streams usage across two events:
      - ``message_start`` carries the input side (input_tokens + the cache split)
        in ``message.usage``;
      - ``message_delta`` carries the cumulative ``output_tokens`` (and a final
        usage echo) in a top-level ``usage``.
    We merge them: take the input/cache fields from message_start's usage and the
    output_tokens from the last message_delta (it reports the running total).

    Returns the merged usage dict, or ``None`` if the stream carried no usage
    (then the caller logs nothing). Robust to interleaved non-data lines, the
    ``data: [DONE]`` sentinel, and unparseable JSON (skipped).
    """
    merged: dict = {}
    saw = False
    for line in body_text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            evt = json.loads(payload)
        except (ValueError, TypeError):
            continue
        etype = evt.get("type")
        if etype == "message_start":
            u = (evt.get("message", {}) or {}).get("usage")
            if isinstance(u, dict):
                # input side + initial output side
                merged.update(u)
                saw = True
        elif etype == "message_delta":
            u = evt.get("usage")
            if isinstance(u, dict):
                # message_delta.usage carries the running output_tokens (and
                # sometimes a cache echo). output_tokens here is authoritative.
                for k in ("output_tokens", "cache_creation_input_tokens",
                          "cache_read_input_tokens", "input_tokens"):
                    if u.get(k) is not None:
                        merged[k] = u[k]
                saw = True
    return merged if saw else None


def _usage_obj_to_anthropic(usage: Any) -> dict:
    """Normalize a litellm ``Usage`` (OpenAI-shaped) into the Anthropic usage dict.

    ``litellm.completion`` returns an OpenAI-shaped ``ModelResponse``; its
    ``usage`` carries the Anthropic cache split in TWO places (litellm stuffs it
    both on private attrs and into ``prompt_tokens_details``):
      - write: ``usage._cache_creation_input_tokens`` OR
               ``usage.prompt_tokens_details.cache_creation_tokens``
      - read : ``usage._cache_read_input_tokens`` OR
               ``usage.prompt_tokens_details.cached_tokens``
    OpenAI ``prompt_tokens`` is the FULL input (cached + uncached); Anthropic's
    ``input_tokens`` is the UNCACHED portion only. We emit the Anthropic shape
    (``input_tokens`` = uncached new input, cache split separate) so the SAME
    ``extract_usage`` path handles both modes. Reads attr- OR dict-shaped usage.
    """
    if usage is None:
        return {}

    def g(key: str) -> Optional[int]:
        v = getattr(usage, key, None)
        if v is None and isinstance(usage, dict):
            v = usage.get(key)
        return int(v) if isinstance(v, (int, float)) else None

    prompt = g("prompt_tokens") or 0
    completion = g("completion_tokens") or 0

    # cache WRITE
    write = g("_cache_creation_input_tokens")
    if write is None:
        write = g("cache_creation_input_tokens")
    # cache READ
    read = g("_cache_read_input_tokens")
    if read is None:
        read = g("cache_read_input_tokens")

    # fall back to prompt_tokens_details (attr or dict) for either field.
    details = getattr(usage, "prompt_tokens_details", None)
    if details is None and isinstance(usage, dict):
        details = usage.get("prompt_tokens_details")
    if details is not None:
        if read is None:
            r = getattr(details, "cached_tokens", None)
            if r is None and isinstance(details, dict):
                r = details.get("cached_tokens")
            read = int(r) if isinstance(r, (int, float)) else read
        if write is None:
            w = getattr(details, "cache_creation_tokens", None)
            if w is None and isinstance(details, dict):
                w = details.get("cache_creation_tokens")
            write = int(w) if isinstance(w, (int, float)) else write

    write = write or 0
    read = read or 0
    # OpenAI prompt_tokens is FULL input; Anthropic input_tokens is UNCACHED only.
    uncached = max(prompt - write - read, 0)
    out: dict = {"input_tokens": uncached, "output_tokens": completion}
    # emit cache keys only when the provider reported a split (presence is the
    # signal extract_usage/pricing use to pick the real-cache path).
    if write:
        out["cache_creation_input_tokens"] = write
    if read:
        out["cache_read_input_tokens"] = read
    return out


# ── Anthropic Messages <-> litellm.completion translation (Vertex mode) ───────
def anthropic_request_to_litellm_args(body: dict, model: str,
                                      project: str, location: str) -> dict:
    """Map an inbound Anthropic ``/v1/messages`` request body -> litellm kwargs.

    Anthropic Messages requests carry ``model``, ``messages`` (content blocks),
    ``system`` (string OR a list of text blocks), ``max_tokens``, ``tools``,
    ``tool_choice``, ``stream``, ``temperature``, ``top_p``, ``top_k``,
    ``stop_sequences``. litellm's Anthropic-family path accepts the SAME
    Anthropic-native ``messages``/``system``/``tools`` shapes (it translates them
    to the Vertex Anthropic API itself), so this is a near-passthrough: we force
    the Vertex routing (model id + project + location for ADC auth) and forward
    the recognized fields untouched. ``max_tokens`` is required by Anthropic, so
    it is always present; we default it defensively.
    """
    args: dict = {
        "model": model,
        "vertex_project": project,
        "vertex_location": location,
        "messages": body.get("messages") or [],
        "max_tokens": int(body.get("max_tokens") or 4096),
        "stream": bool(body.get("stream")),
    }
    # system: Anthropic allows a bare string OR a list of text blocks; pass through.
    if body.get("system") is not None:
        args["system"] = body["system"]
    # optional generation controls — forward only when present (don't fabricate).
    for k in ("temperature", "top_p", "top_k", "stop_sequences", "tools",
              "tool_choice", "metadata"):
        if body.get(k) is not None:
            args[k] = body[k]
    # ask litellm to surface usage on a streamed response too (so we can capture
    # the cache split in streaming mode without re-deriving it from text).
    if args["stream"]:
        args["stream_options"] = {"include_usage": True}
    return args


def _content_blocks_from_choice(message: Any) -> list:
    """Build Anthropic ``content`` blocks from a litellm choice ``message``.

    A litellm message has ``content`` (text, may be None) and ``tool_calls`` (a
    list of OpenAI tool-call dicts). We emit Anthropic blocks: a ``text`` block
    for the text (when non-empty) and a ``tool_use`` block per tool call (parsing
    the JSON arguments string into an ``input`` object). Order: text first, then
    tool calls — matching how Claude returns interleaved tool use.
    """
    blocks: list = []
    text = getattr(message, "content", None)
    if text is None and isinstance(message, dict):
        text = message.get("content")
    if text:
        blocks.append({"type": "text", "text": text})

    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls is None and isinstance(message, dict):
        tool_calls = message.get("tool_calls")
    for tc in (tool_calls or []):
        fn = getattr(tc, "function", None)
        if fn is None and isinstance(tc, dict):
            fn = tc.get("function")
        name = getattr(fn, "name", None) or (fn.get("name") if isinstance(fn, dict) else None)
        raw_args = getattr(fn, "arguments", None)
        if raw_args is None and isinstance(fn, dict):
            raw_args = fn.get("arguments")
        try:
            tool_input = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
        except (ValueError, TypeError):
            tool_input = {}
        tc_id = getattr(tc, "id", None) or (tc.get("id") if isinstance(tc, dict) else None)
        blocks.append({
            "type": "tool_use",
            "id": tc_id or "toolu_unknown",
            "name": name or "tool",
            "input": tool_input if isinstance(tool_input, dict) else {},
        })
    return blocks


# OpenAI finish_reason -> Anthropic stop_reason
_STOP_REASON = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "end_turn",
}


def litellm_response_to_anthropic(resp: Any, model: str) -> dict:
    """Map a (non-streaming) litellm ``ModelResponse`` -> an Anthropic Messages dict.

    Preserves ``content`` (text + tool_use blocks), ``stop_reason``, and ``usage``
    (with the cache split, via ``_usage_obj_to_anthropic``). The shape matches a
    real ``/v1/messages`` response so Claude Code / the SDK parse it unchanged.
    """
    choices = getattr(resp, "choices", None)
    if choices is None and isinstance(resp, dict):
        choices = resp.get("choices")
    choice0 = (choices or [{}])[0]
    message = getattr(choice0, "message", None)
    if message is None and isinstance(choice0, dict):
        message = choice0.get("message")
    finish = getattr(choice0, "finish_reason", None)
    if finish is None and isinstance(choice0, dict):
        finish = choice0.get("finish_reason")

    usage_obj = getattr(resp, "usage", None)
    if usage_obj is None and isinstance(resp, dict):
        usage_obj = resp.get("usage")
    anth_usage = _usage_obj_to_anthropic(usage_obj)

    resp_id = getattr(resp, "id", None) or (resp.get("id") if isinstance(resp, dict) else None)
    return {
        "id": resp_id or "msg_vertex",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": _content_blocks_from_choice(message) if message is not None else [],
        "stop_reason": _STOP_REASON.get((finish or "stop"), "end_turn"),
        "stop_sequence": None,
        "usage": anth_usage,
    }


def _sse_event(event: str, data: dict) -> bytes:
    """One Anthropic-style SSE event frame: ``event: <e>\\ndata: <json>\\n\\n``."""
    return (f"event: {event}\n"
            f"data: {json.dumps(data, separators=(',', ':'))}\n\n").encode("utf-8")


def litellm_stream_to_anthropic_sse(stream: Any, model: str):
    """Translate a litellm streaming response into Anthropic SSE event bytes.

    litellm yields OpenAI-shaped ``ModelResponseStream`` chunks (delta.content /
    delta.tool_calls + a final usage when ``include_usage`` is set). We emit the
    Anthropic event sequence the Messages API streams:

        message_start -> content_block_start -> content_block_delta* ->
        content_block_stop -> message_delta (stop_reason + usage) -> message_stop

    Yields ``bytes`` (encoded SSE frames). The merged usage (with the cache split)
    is carried on the final ``message_delta`` so ``usage_from_sse``/``extract_usage``
    capture it exactly as for a real Anthropic stream. Tool-call deltas are
    accumulated and flushed as a single ``input_json_delta`` block (sufficient for
    usage capture and a faithful content relay; the agent re-parses tool input
    from the assembled JSON).

    Robust: a malformed chunk is skipped; a stream that ends without usage still
    closes the event sequence cleanly.
    """
    msg_id = "msg_vertex_stream"
    # message_start: input-side usage is unknown until the end on Vertex/litellm,
    # so start with zeros and correct it on the final message_delta.
    yield _sse_event("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id, "type": "message", "role": "assistant", "model": model,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    })
    yield _sse_event("content_block_start", {
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "text", "text": ""},
    })

    final_usage_obj = None
    finish_reason = "stop"
    saw_text = False
    for chunk in stream:
        try:
            choices = getattr(chunk, "choices", None)
            if choices is None and isinstance(chunk, dict):
                choices = chunk.get("choices")
            if choices:
                c0 = choices[0]
                delta = getattr(c0, "delta", None)
                if delta is None and isinstance(c0, dict):
                    delta = c0.get("delta")
                fr = getattr(c0, "finish_reason", None)
                if fr is None and isinstance(c0, dict):
                    fr = c0.get("finish_reason")
                if fr:
                    finish_reason = fr
                piece = getattr(delta, "content", None)
                if piece is None and isinstance(delta, dict):
                    piece = delta.get("content")
                if piece:
                    saw_text = True
                    yield _sse_event("content_block_delta", {
                        "type": "content_block_delta", "index": 0,
                        "delta": {"type": "text_delta", "text": piece},
                    })
            u = getattr(chunk, "usage", None)
            if u is None and isinstance(chunk, dict):
                u = chunk.get("usage")
            if u is not None:
                final_usage_obj = u
        except Exception:  # noqa: BLE001 — never let one bad chunk break the stream
            continue

    _ = saw_text  # text presence not needed past here; block 0 always opened/closed
    yield _sse_event("content_block_stop", {"type": "content_block_stop", "index": 0})

    anth_usage = _usage_obj_to_anthropic(final_usage_obj)
    # message_delta carries the authoritative final usage (incl. cache split) the
    # SSE usage parser reads. Anthropic puts only the CUMULATIVE output_tokens (and
    # a cache echo) on message_delta.usage; the input side rides on message_start —
    # but our message_start sent zeros, so we echo the FULL usage here and let
    # usage_from_sse merge it (it takes message_delta fields as authoritative).
    yield _sse_event("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": _STOP_REASON.get(finish_reason, "end_turn"),
                  "stop_sequence": None},
        "usage": anth_usage or {"output_tokens": 0},
    })
    yield _sse_event("message_stop", {"type": "message_stop"})


# ── the per-run usage sink (thread-safe append to JSONL) ──────────────────────
class UsageSink:
    """Appends CallUsage rows to a per-run JSONL, keyed by run id.

    One JSONL file per run id under ``log_dir`` (``<run_id>.usage.jsonl``). Writes
    are serialized by a lock (the gateway is multithreaded). A write failure is
    swallowed (logged to stderr) so it never breaks the proxied call.
    """

    def __init__(self, log_dir: str) -> None:
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def path_for(self, run_id: str) -> Path:
        # sanitize: keep the filename a single safe component
        safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in (run_id or "default"))
        return self.log_dir / f"{safe}.usage.jsonl"

    def write(self, run_id: str, row: CallUsage) -> None:
        try:
            with self._lock:
                with self.path_for(run_id).open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row) + "\n")
        except Exception as e:  # noqa: BLE001 — never break the proxied call on a log write
            print(f"  usage_gateway WARN: usage write failed for run {run_id!r}: "
                  f"{type(e).__name__}: {str(e)[:160]}", flush=True)


# ── litellm Vertex bridge (lazy) ──────────────────────────────────────────────
def _vertex_completion(args: dict):
    """Call ``litellm.completion`` (imported LAZILY) for the Vertex Anthropic model.

    litellm is the ONE heavy dep; importing it here (not at module load) keeps
    ``import bench.usage_gateway`` and passthrough mode working on a box without
    litellm. Auth is ADC (Application Default Credentials) — no API key passed;
    ``vertex_project``/``vertex_location`` in ``args`` select the Vertex endpoint.
    """
    import litellm  # lazy — heavy, Vertex-mode only
    return litellm.completion(**args)


# ── the proxy/bridge request handler ──────────────────────────────────────────
def make_handler(upstream_base: str, sink: UsageSink,
                 default_run_id: str = "", default_headers: Optional[dict] = None,
                 timeout_s: float = 600.0, *,
                 mode: str = MODE_VERTEX,
                 vertex_model: str = VERTEX_MODEL,
                 vertex_project: str = VERTEX_PROJECT,
                 vertex_location: str = VERTEX_LOCATION,
                 completion_fn=None):
    """Build a BaseHTTPRequestHandler subclass bound to one upstream/bridge + sink.

    upstream_base   : (passthrough mode) the base URL to forward to verbatim — the
                      arm's compression endpoint or another Anthropic endpoint.
                      Ignored in Vertex mode (litellm is the upstream).
    sink            : where CallUsage rows are written.
    default_run_id  : run-id tag used when a request carries no RUN_ID_HEADER.
    default_headers : (passthrough mode) extra headers MERGED onto every forwarded
                      request. Incoming client headers win on conflict.
    timeout_s       : per-request upstream timeout (passthrough mode).
    mode            : ``MODE_VERTEX`` (default — gateway IS the litellm->Vertex
                      bridge) or ``MODE_PASSTHROUGH`` (forward verbatim upstream).
    vertex_*        : Vertex routing for litellm (model id, project, location);
                      auth is ADC on the box (no API key).
    completion_fn   : injectable litellm.completion (Vertex mode) — defaults to
                      the lazy ``_vertex_completion``. Tests monkeypatch this.
    """
    base = upstream_base.rstrip("/")
    extra_headers = dict(default_headers or {})
    _complete = completion_fn or _vertex_completion

    class _Handler(BaseHTTPRequestHandler):
        # silence the default per-request stderr logging (noisy under a pool);
        # real failures are printed explicitly below.
        def log_message(self, *args) -> None:  # noqa: D401
            return

        # ── shared entrypoint for all methods (route by mode) ────────────────
        def _proxy(self) -> None:
            t0 = time.time()
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""

            run_id = self.headers.get(RUN_ID_HEADER) or default_run_id

            if mode == MODE_VERTEX:
                self._bridge_vertex(body, run_id, t0)
                return
            self._passthrough(body, run_id, t0)

        # ── Vertex mode: bridge Anthropic Messages -> litellm.completion -> Vertex
        def _bridge_vertex(self, body: bytes, run_id: str, t0: float) -> None:
            """Translate the inbound Anthropic request, call litellm (Vertex), and
            relay an Anthropic Messages response (JSON or SSE), capturing usage.

            count_tokens is handled locally (a cheap char-based estimate) so we
            never need a second upstream; the bench prices off the per-CALL usage
            rows from real messages, not count_tokens. Any translation/litellm
            error returns a clean Anthropic-shaped error (never a hang); usage we
            already have is still logged.
            """
            # /v1/messages/count_tokens — answer locally; no Vertex round-trip.
            if self.path.rstrip("/").endswith("count_tokens"):
                self._vertex_count_tokens(body)
                return

            try:
                req_body = json.loads(body.decode("utf-8")) if body else {}
            except (ValueError, TypeError) as e:
                self._gateway_error(400, f"bad request JSON: {type(e).__name__}: {str(e)[:120]}")
                return

            try:
                args = anthropic_request_to_litellm_args(
                    req_body, vertex_model, vertex_project, vertex_location)
            except Exception as e:  # noqa: BLE001 — translation must never hang the run
                self._gateway_error(400, f"request translation failed: "
                                    f"{type(e).__name__}: {str(e)[:160]}")
                return

            stream = bool(args.get("stream"))
            try:
                resp = _complete(args)
            except Exception as e:  # noqa: BLE001 — litellm/upstream/ADC error
                self._gateway_error(502, f"vertex upstream error: "
                                    f"{type(e).__name__}: {str(e)[:200]}")
                return

            try:
                if stream:
                    self._relay_vertex_stream(resp, run_id, t0)
                else:
                    self._relay_vertex_json(resp, run_id, t0)
            except (BrokenPipeError, ConnectionError):
                return  # client hung up — never crash

        def _relay_vertex_json(self, resp, run_id: str, t0: float) -> None:
            anth = litellm_response_to_anthropic(resp, vertex_model)
            data = json.dumps(anth).encode("utf-8")
            # log usage BEFORE relaying (durable by the time the client returns).
            try:
                row = extract_usage(anth.get("usage"), time.time() - t0)
                if row is not None:
                    sink.write(run_id, row)
            except Exception as e:  # noqa: BLE001 — logging must never break the run
                print(f"  usage_gateway WARN: vertex usage capture failed: "
                      f"{type(e).__name__}: {str(e)[:160]}", flush=True)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _relay_vertex_stream(self, resp, run_id: str, t0: float) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            chunks: list[bytes] = []
            for frame in litellm_stream_to_anthropic_sse(resp, vertex_model):
                chunks.append(frame)
                self.wfile.write(frame)
                self.wfile.flush()
            # capture usage off the SSE we just emitted (same parser as passthrough).
            try:
                body_text = b"".join(chunks).decode("utf-8", "replace")
                usage = usage_from_sse(body_text)
                row = extract_usage(usage, time.time() - t0)
                if row is not None:
                    sink.write(run_id, row)
            except Exception as e:  # noqa: BLE001 — logging must never break the run
                print(f"  usage_gateway WARN: vertex SSE usage capture failed: "
                      f"{type(e).__name__}: {str(e)[:160]}", flush=True)

        def _vertex_count_tokens(self, body: bytes) -> None:
            """Local count_tokens estimate (no Vertex round-trip).

            Anthropic's count_tokens returns ``{"input_tokens": N}``. We give a
            coarse char/4 estimate over the serialized messages+system — adequate
            for Claude Code's pre-flight sizing; real billing uses the per-call
            usage rows, not this. Never raises (defaults to 0 on a parse error).
            """
            try:
                obj = json.loads(body.decode("utf-8")) if body else {}
                blob = json.dumps(obj.get("messages") or []) + json.dumps(obj.get("system") or "")
                est = max(1, len(blob) // 4)
            except Exception:  # noqa: BLE001
                est = 1
            payload = json.dumps({"input_tokens": est}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        # ── passthrough mode: forward the request verbatim to upstream_base ──
        def _passthrough(self, body: bytes, run_id: str, t0: float) -> None:
            # Build forwarded headers: drop hop-by-hop + our run-id tag; preserve
            # auth / anthropic-beta / anthropic-version untouched; fill from
            # default_headers only where the client didn't set the key.
            fwd_headers: dict[str, str] = {}
            for k, v in self.headers.items():
                if k.lower() in _HOP_BY_HOP or k.lower() == RUN_ID_HEADER:
                    continue
                fwd_headers[k] = v
            for k, v in extra_headers.items():
                fwd_headers.setdefault(k, v)
            # ask the upstream for an unencoded body so we can tee/parse it.
            fwd_headers["Accept-Encoding"] = "identity"

            url = base + self.path
            req = urllib.request.Request(url, data=body, method=self.command,
                                         headers=fwd_headers)
            try:
                resp = urllib.request.urlopen(req, timeout=timeout_s)
            except urllib.error.HTTPError as e:
                # The upstream returned a non-2xx: forward it FAITHFULLY (status
                # + body) so the SDK sees the real error, then return — no usage
                # to log on an error response.
                self._relay_error(e)
                return
            except Exception as e:  # noqa: BLE001 — connection refused / timeout / etc.
                self._gateway_error(504, f"upstream unreachable: {type(e).__name__}: {str(e)[:160]}")
                return

            # Detect streaming: Anthropic SSE responses are content-type
            # text/event-stream. Stream those through; buffer everything else.
            ctype = (resp.headers.get("Content-Type") or "").lower()
            try:
                if "text/event-stream" in ctype:
                    self._relay_stream(resp, run_id, t0)
                else:
                    self._relay_buffered(resp, run_id, t0)
            except (BrokenPipeError, ConnectionError):
                # client (SDK) hung up mid-response — nothing to do, never crash.
                return

        # ── non-streaming: buffer, log usage, then relay the body ─────────────
        def _relay_buffered(self, resp, run_id: str, t0: float) -> None:
            data = resp.read()
            # log usage BEFORE writing the body back: the whole response is
            # already buffered (no streaming benefit to deferring), and logging
            # first means the usage row is durable by the time the client's
            # request returns — no read-after-write race for callers that inspect
            # the JSONL immediately (e.g. the runner reads it right after the run).
            self._log_json_usage(resp.headers, data, run_id, time.time() - t0)
            self.send_response(resp.status)
            self._send_passthrough_headers(resp.headers, len(data))
            self.end_headers()
            if data:
                self.wfile.write(data)

        # ── streaming: tee bytes to the client AND to an SSE buffer ───────────
        def _relay_stream(self, resp, run_id: str, t0: float) -> None:
            self.send_response(resp.status)
            # streamed: don't set Content-Length; preserve chunked/SSE semantics.
            self._send_passthrough_headers(resp.headers, content_length=None)
            self.end_headers()
            chunks: list[bytes] = []
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                chunks.append(chunk)
                self.wfile.write(chunk)
                self.wfile.flush()
            # reconstruct usage from the buffered SSE body (off the hot path).
            try:
                body_text = b"".join(chunks).decode("utf-8", "replace")
                usage = usage_from_sse(body_text)
                row = extract_usage(usage, time.time() - t0)
                if row is not None:
                    sink.write(run_id, row)
            except Exception as e:  # noqa: BLE001 — logging must never break the run
                print(f"  usage_gateway WARN: SSE usage parse failed: "
                      f"{type(e).__name__}: {str(e)[:160]}", flush=True)

        def _log_json_usage(self, headers, data: bytes, run_id: str, latency_s: float) -> None:
            try:
                obj = json.loads(data.decode("utf-8", "replace")) if data else None
                usage = obj.get("usage") if isinstance(obj, dict) else None
                row = extract_usage(usage, latency_s)
                if row is not None:
                    sink.write(run_id, row)
            except Exception as e:  # noqa: BLE001 — non-JSON body / no usage: passthrough only
                # Not an error — count_tokens responses, error bodies, etc. carry
                # no usage. Stay quiet unless it looked like a message response.
                ct = (headers.get("Content-Type") or "").lower()
                if "application/json" in ct and b"usage" in (data or b""):
                    print(f"  usage_gateway WARN: JSON usage parse failed: "
                          f"{type(e).__name__}: {str(e)[:120]}", flush=True)

        # ── header relay helpers ──────────────────────────────────────────────
        def _send_passthrough_headers(self, headers, content_length: Optional[int]) -> None:
            for k, v in headers.items():
                if k.lower() in _HOP_BY_HOP:
                    continue
                self.send_header(k, v)
            if content_length is not None:
                self.send_header("Content-Length", str(content_length))

        def _relay_error(self, e: urllib.error.HTTPError) -> None:
            data = e.read() or b""
            self.send_response(e.code)
            self._send_passthrough_headers(e.headers, len(data))
            self.end_headers()
            if data:
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionError):
                    pass

        def _gateway_error(self, code: int, msg: str) -> None:
            print(f"  usage_gateway: {code} {msg}", flush=True)
            body = json.dumps({"type": "error",
                               "error": {"type": "gateway_error", "message": msg}}).encode()
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionError):
                pass

        # all proxied verbs route through _proxy
        def do_POST(self) -> None:  # noqa: N802
            self._proxy()

        def do_GET(self) -> None:  # noqa: N802
            self._proxy()

    return _Handler


class UsageGateway:
    """A running gateway: an HTTP server thread + the usage sink it writes to.

    Usage (Vertex mode — the default; the gateway IS the litellm->Vertex bridge):
        gw = UsageGateway(log_dir="runs/usage")          # mode defaults to vertex
        gw.start()
        base_url = gw.base_url          # set ANTHROPIC_BASE_URL to this
        ...                             # run the SDK / a vendor proxy above us
        gw.stop()

    Usage (passthrough mode — forward verbatim to an Anthropic-speaking upstream):
        gw = UsageGateway(log_dir="runs/usage", mode=MODE_PASSTHROUGH,
                          upstream_base="https://api.anthropic.com")

    The server binds to 127.0.0.1 on an ephemeral port (port 0) by default so
    many gateways can run concurrently (one per worker / per (instance, arm))
    without a port-allocation dance. ``base_url`` is the address to hand the SDK
    (A0/woz) or to configure as each vendor proxy's upstream (proxy arms).
    """

    def __init__(self, upstream_base: str = "", log_dir: str = "runs/usage", *,
                 host: str = "127.0.0.1", port: int = 0,
                 default_run_id: str = "", default_headers: Optional[dict] = None,
                 timeout_s: float = 600.0,
                 mode: str = MODE_VERTEX,
                 vertex_model: str = VERTEX_MODEL,
                 vertex_project: str = VERTEX_PROJECT,
                 vertex_location: str = VERTEX_LOCATION,
                 completion_fn=None) -> None:
        self.upstream_base = upstream_base
        self.mode = mode
        self.sink = UsageSink(log_dir)
        self.default_run_id = default_run_id
        handler = make_handler(upstream_base, self.sink,
                               default_run_id=default_run_id,
                               default_headers=default_headers,
                               timeout_s=timeout_s,
                               mode=mode,
                               vertex_model=vertex_model,
                               vertex_project=vertex_project,
                               vertex_location=vertex_location,
                               completion_fn=completion_fn)
        self._server = ThreadingHTTPServer((host, port), handler)
        self._thread: Optional[threading.Thread] = None

    @property
    def address(self) -> tuple[str, int]:
        return self._server.server_address[0], self._server.server_address[1]

    @property
    def base_url(self) -> str:
        host, port = self.address
        return f"http://{host}:{port}"

    def usage_path(self, run_id: Optional[str] = None) -> Path:
        return self.sink.path_for(run_id or self.default_run_id)

    def start(self) -> "UsageGateway":
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="usage-gateway", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        try:
            self._server.shutdown()
        except Exception:
            pass
        try:
            self._server.server_close()
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self) -> "UsageGateway":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


# ── standalone launch (debug / smoke) ─────────────────────────────────────────
def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Anthropic usage-logging gateway "
                                 "(Vertex bridge or passthrough)")
    ap.add_argument("--mode", default=os.environ.get("CCB_GATEWAY_MODE", MODE_VERTEX),
                    choices=[MODE_VERTEX, MODE_PASSTHROUGH],
                    help="vertex: litellm->Vertex bridge (default); "
                         "passthrough: forward verbatim to --upstream")
    ap.add_argument("--upstream", default=os.environ.get("CCB_GATEWAY_UPSTREAM",
                                                          "https://api.anthropic.com"),
                    help="(passthrough mode) upstream base URL to forward to")
    ap.add_argument("--vertex-model", default=VERTEX_MODEL,
                    help="(vertex mode) litellm Vertex model id")
    ap.add_argument("--vertex-project", default=VERTEX_PROJECT)
    ap.add_argument("--vertex-location", default=VERTEX_LOCATION)
    ap.add_argument("--log-dir", default=os.environ.get("CCB_GATEWAY_LOG_DIR", "runs/usage"))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--run-id", default=os.environ.get("CCB_GATEWAY_RUN_ID", "default"))
    a = ap.parse_args()

    gw = UsageGateway(a.upstream, a.log_dir, host=a.host, port=a.port,
                      default_run_id=a.run_id, mode=a.mode,
                      vertex_model=a.vertex_model, vertex_project=a.vertex_project,
                      vertex_location=a.vertex_location).start()
    if a.mode == MODE_VERTEX:
        dest = (f"vertex({a.vertex_model} @ {a.vertex_project}/{a.vertex_location}, "
                f"ADC auth)")
    else:
        dest = a.upstream
    print(f"usage_gateway [{a.mode}]: {gw.base_url} -> {dest}  "
          f"(usage -> {gw.usage_path()})", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        gw.stop()


if __name__ == "__main__":
    main()
