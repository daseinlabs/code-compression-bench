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
  * ``vertex``      — the gateway IS the Vertex bridge, a NATIVE STREAMING
                      passthrough (relays Anthropic events verbatim). It receives
                      Anthropic ``/v1/messages`` (+ ``/v1/messages/count_tokens``)
                      and forwards the request NATIVELY via the ``anthropic`` SDK's
                      ``AnthropicVertex`` client, calling
                      ``client.messages.create(model="claude-sonnet-4-6", ...,
                      stream=True)`` with ADC auth (no API key). The inbound request
                      IS already Anthropic Messages format and the target IS
                      Anthropic-on-Vertex, so this is a pure passthrough — the
                      recognized native fields (messages/system/tools/tool_choice/
                      thinking/…) ride through untouched, and the stream events the
                      SDK yields ARE native Anthropic SSE events (message_start,
                      content_block_*, message_delta, message_stop). We relay those
                      events VERBATIM — zero translation, which kills the whole class
                      of tool-call / shape bugs we hit. This is the seam that lets
                      the WHOLE chain speak Anthropic (Claude Code and the vendor
                      proxies only speak the Anthropic API) while the real model runs
                      claude-sonnet on Vertex via ADC.
                      (Why ``stream=True``: Claude Code sends a large ``max_tokens``,
                      and the anthropic SDK then REFUSES a non-streaming call with
                      ``ValueError: Streaming is required for operations that may
                      take longer than 10 minutes``. Iterating a streaming response
                      avoids that guard entirely.)
                      (The old litellm <-> OpenAI translation layer is gone: it
                      dropped tool_calls -> "the model's tool call could not be
                      parsed", and rejected Anthropic-native messages -> "Invalid
                      user message ... not valid OpenAI chat completion messages".)
  * ``passthrough`` — the original behaviour: forward each request verbatim to a
                      configured upstream base URL (urllib), a passive observer of
                      the wire. Used for an Anthropic-direct upstream or to chain
                      to another Anthropic-speaking endpoint.

WHAT IT CAPTURES (Anthropic usage shape)
----------------------------------------
From each response's ``usage`` object (non-streaming JSON body, OR the final
``message_delta``/``message_start`` of an SSE stream — or, in Vertex mode, the
``AnthropicVertex`` Message's ``usage``):
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
unchanged and we merely fail to LOG a usage row. In Vertex mode an upstream
(AnthropicVertex / ADC) error returns a clean Anthropic-shaped error response
(never a hang), and we still log whatever usage we managed to read. Streaming
responses are streamed through so the SDK sees tokens as they arrive; we tee the
bytes to a usage parser on the side.

CLEAN-ROOM
----------
The module itself is pure stdlib (``http.server``, ``urllib``, ``json``,
``threading``). It has NO ``adaptive_context`` import. The ONE heavy dependency —
the ``anthropic`` SDK (``AnthropicVertex``) — is imported LAZILY, only when the
default Vertex completion_fn first builds its client, so ``import bench.usage_gateway`` /
``import bench.cc_runner``, the tests, and the passthrough mode all work on a box
without ``anthropic`` installed. ``bench.schema`` is imported only for the
``CallUsage`` TypedDict shape (stdlib dataclasses/typing).
"""

from __future__ import annotations

import json
import os
import re
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
MODE_VERTEX = "vertex"            # gateway IS the AnthropicVertex native bridge (default)
MODE_PASSTHROUGH = "passthrough"  # gateway forwards verbatim to an upstream URL

# ── Vertex defaults (claude-sonnet on Vertex via AnthropicVertex + ADC) ──────
# Auth is Application Default Credentials on the box (gcloud auth application-default
# login) — NO API key. project/location are overridable via env to retarget without
# a code change. cc_runner passes VERTEX_MODEL="vertex_ai/claude-sonnet-4-6"; the
# native AnthropicVertex client wants the BARE id, so the leading "vertex_ai/" is
# stripped (``_strip_vertex_prefix``) before use.
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


# ── Anthropic Messages -> AnthropicVertex create kwargs (native passthrough) ──
def _strip_vertex_prefix(model: str) -> str:
    """Strip a leading ``"vertex_ai/"`` so AnthropicVertex gets the BARE model id.

    cc_runner passes ``VERTEX_MODEL="vertex_ai/claude-sonnet-4-6"`` (the litellm
    routing id). The native ``AnthropicVertex`` client wants ``"claude-sonnet-4-6"``.
    """
    if model and model.startswith("vertex_ai/"):
        return model[len("vertex_ai/"):]
    return model


# ── anthropic-beta forwarding (filter the betas Vertex doesn't accept) ────────
# Claude Code forwards an ``anthropic-beta`` header carrying its negotiated beta
# capabilities (claude-code-*, interleaved-thinking-*, context-management-*,
# structured-outputs-*, prompt-caching-scope-*, …). AnthropicVertex ACCEPTS most
# of these, but REJECTS some with ``400 invalid_request_error: Unexpected
# value(s) `<beta>` `` — forwarding the FULL set 400s the whole call, so the
# Vertex-unsupported betas MUST be stripped before we forward. The first known
# offender is the ``prompt-caching-scope`` family; we drop it by PREFIX so the
# filter is version-robust (``prompt-caching-scope-2026-01-05`` and any future
# dated variant). _make_vertex_completion adds a self-healing retry on top of
# this for betas that become unsupported later.
_VERTEX_UNSUPPORTED_BETA_PREFIXES = (
    "prompt-caching-scope",
)


def _filter_vertex_betas(header_value: Optional[str]) -> str:
    """Drop Vertex-unsupported betas from a comma-separated ``anthropic-beta`` value.

    Splits the header on commas, strips whitespace, and removes any beta whose
    name starts with one of ``_VERTEX_UNSUPPORTED_BETA_PREFIXES`` (prefix match —
    version-robust). Returns the surviving betas rejoined with ``","`` (no spaces),
    or ``""`` if none remain / the input is None/empty.
    """
    if not header_value:
        return ""
    kept = []
    for raw in header_value.split(","):
        beta = raw.strip()
        if not beta:
            continue
        if any(beta.startswith(p) for p in _VERTEX_UNSUPPORTED_BETA_PREFIXES):
            continue
        kept.append(beta)
    return ",".join(kept)


# Vertex reports unsupported betas as ``Unexpected value(s) `<beta>` `` (one or
# more backtick-quoted tokens). This pulls the token(s) out so the self-healing
# retry in _make_vertex_completion can strip exactly those and try again.
_UNEXPECTED_VALUE_BETA_RE = re.compile(r"`([^`]+)`")


def _betas_from_unexpected_value_error(message: str) -> list:
    """Extract the backtick-quoted beta token(s) from a Vertex 'Unexpected value(s)'
    error message. Returns the list of offending beta names (possibly empty)."""
    if "Unexpected value(s)" not in message:
        return []
    return [m.strip() for m in _UNEXPECTED_VALUE_BETA_RE.findall(message) if m.strip()]


def _strip_betas_from_kwargs(create_kwargs: dict, offenders) -> bool:
    """Remove ``offenders`` from ``create_kwargs["extra_headers"]["anthropic-beta"]``.

    Returns True if anything was actually removed (so the caller knows to retry).
    If the ``anthropic-beta`` value empties out, the key is removed; if
    ``extra_headers`` empties out, it's dropped entirely.
    """
    headers = create_kwargs.get("extra_headers")
    if not isinstance(headers, dict):
        return False
    current = headers.get("anthropic-beta")
    if not current:
        return False
    offender_set = {o.strip() for o in offenders}
    kept = [b.strip() for b in current.split(",")
            if b.strip() and b.strip() not in offender_set]
    if len(kept) == len([b for b in current.split(",") if b.strip()]):
        return False  # nothing matched — don't loop forever
    if kept:
        headers["anthropic-beta"] = ",".join(kept)
    else:
        headers.pop("anthropic-beta", None)
        if not headers:
            create_kwargs.pop("extra_headers", None)
    return True


# Native Anthropic Messages fields we pass through verbatim when present. The
# inbound request IS Anthropic Messages format and the target IS Anthropic — so
# this is a pure passthrough; we never translate to a foreign (OpenAI) shape.
# ``stream`` is handled separately (the gateway buffers + re-emits SSE itself),
# and ``model`` is forced to the configured Vertex model, so neither appears here.
_NATIVE_PASSTHROUGH_FIELDS = (
    "messages", "system", "tools", "tool_choice", "temperature", "top_p",
    "top_k", "stop_sequences", "metadata", "thinking",
)


def _anthropic_create_kwargs(body: dict, model: str) -> dict:
    """Build ``client.messages.create(**kwargs)`` from an inbound Anthropic body.

    Pass through ONLY the recognized native Anthropic fields that are present in
    the inbound body (don't fabricate absent ones), plus ``max_tokens`` (default
    64000 if absent — Anthropic requires it) and the configured Vertex ``model``.
    We do NOT include ``stream`` (handled separately) and do NOT forward the
    client's ``model`` (we always route to the configured Vertex model id).

    The client's ``max_tokens`` passes through unchanged — Claude Code ALWAYS
    sends one (the API requires it; it sends 32000), so the absent-case default
    is never hit in practice. It exists only as a safe floor; 64000 is Sonnet
    4.x's max output, so the floor can't truncate a reply on its own.
    """
    kwargs: dict = {
        "model": model,
        "max_tokens": int(body.get("max_tokens") or 64000),
        "messages": body.get("messages") or [],
    }
    for k in _NATIVE_PASSTHROUGH_FIELDS:
        if k == "messages":
            continue  # already set above (always present)
        if body.get(k) is not None:
            kwargs[k] = body[k]
    return kwargs


def _sse_event(event: str, data: dict) -> bytes:
    """One Anthropic-style SSE event frame: ``event: <e>\\ndata: <json>\\n\\n``."""
    return (f"event: {event}\n"
            f"data: {json.dumps(data, separators=(',', ':'))}\n\n").encode("utf-8")


# ── native stream-event helpers (SDK objects OR plain dicts, for tests) ───────
def _event_type(e: Any) -> Optional[str]:
    """The SSE event name. Accepts an SDK event object (``e.type``) or a plain dict
    (``e["type"]``) so tests can pass dicts that mimic the SDK without ``anthropic``.
    """
    t = getattr(e, "type", None)
    if t is not None:
        return t
    if isinstance(e, dict):
        return e.get("type")
    return None


def _event_data(e: Any) -> dict:
    """The SSE ``data`` dict for an event — the JSON payload relayed verbatim.

    An SDK event has ``model_dump()`` whose output already carries a ``"type"`` key
    (e.g. message_start dumps to ``{"type":"message_start","message":{...}}``).
    A plain dict (a test fake) is used as-is. Either way we ensure the dict carries
    a ``"type"`` key (set from ``_event_type`` if missing) so ``usage_from_sse``
    and any downstream consumer see the native event shape.
    """
    if hasattr(e, "model_dump"):
        d = e.model_dump()
    elif isinstance(e, dict):
        d = dict(e)
    else:
        d = {}
    if "type" not in d:
        t = _event_type(e)
        if t is not None:
            d["type"] = t
    return d


def _accumulate_anthropic_message(ev_dicts: list, model: str) -> dict:
    """Reconstruct a COMPLETE Anthropic message dict from a native event list.

    ``ev_dicts`` is the buffered ``[(type, data), ...]`` from one streamed response.
    Used ONLY for a NON-streaming client (the streaming client relays the native
    event frames verbatim — no reconstruction). We walk the events the way any SSE
    client would:

      * ``message_start.message`` -> id / role / model / usage (the input side,
        incl. the cache split; native ``input_tokens`` is the UNCACHED count, kept
        as-is — NOT re-derived);
      * ``content_block_start`` opens a block (text accumulates ``text_delta``;
        tool_use buffers ``input_json_delta`` ``partial_json`` then ``json.loads``
        it into the tool_use ``input`` OBJECT at ``content_block_stop``);
      * ``message_delta`` -> ``stop_reason`` (+ merges its ``output_tokens`` /
        cache echo into usage).

    Returns the Anthropic Messages response dict the JSON relay sends:
    {id, type:"message", role:"assistant", model, content, stop_reason,
     stop_sequence, usage(plain dict, cache split, None->0)}.
    """
    msg_id = "msg_vertex"
    role = "assistant"
    stop_reason: Optional[str] = None
    stop_sequence = None
    raw_usage: dict = {}
    # block accumulators, keyed by content-block index
    blocks: dict = {}            # index -> partial block dict
    order: list = []             # index order, as opened
    json_buf: dict = {}          # index -> accumulated partial_json string (tool_use)

    for (etype, data) in ev_dicts:
        if etype == "message_start":
            m = (data.get("message") or {}) if isinstance(data, dict) else {}
            msg_id = m.get("id") or msg_id
            role = m.get("role") or role
            u = m.get("usage")
            if isinstance(u, dict):
                raw_usage.update(u)
        elif etype == "content_block_start":
            idx = data.get("index", len(order))
            cb = (data.get("content_block") or {}) if isinstance(data, dict) else {}
            btype = cb.get("type")
            if btype == "text":
                blk = {"type": "text", "text": cb.get("text") or ""}
            elif btype == "tool_use":
                blk = {"type": "tool_use", "id": cb.get("id") or "toolu_unknown",
                       "name": cb.get("name") or "tool", "input": {}}
                json_buf[idx] = ""
            else:
                blk = {"type": btype or "text", "text": ""}
            blocks[idx] = blk
            order.append(idx)
        elif etype == "content_block_delta":
            idx = data.get("index", 0)
            delta = (data.get("delta") or {}) if isinstance(data, dict) else {}
            dtype = delta.get("type")
            blk = blocks.get(idx)
            if blk is None:
                continue
            if dtype == "text_delta":
                blk["text"] = (blk.get("text") or "") + (delta.get("text") or "")
            elif dtype == "input_json_delta":
                json_buf[idx] = json_buf.get(idx, "") + (delta.get("partial_json") or "")
        elif etype == "content_block_stop":
            idx = data.get("index", 0)
            blk = blocks.get(idx)
            if blk is not None and blk.get("type") == "tool_use":
                raw = json_buf.get(idx, "")
                try:
                    parsed = json.loads(raw) if raw else {}
                except (ValueError, TypeError):
                    parsed = {}
                blk["input"] = parsed if isinstance(parsed, dict) else {}
        elif etype == "message_delta":
            delta = (data.get("delta") or {}) if isinstance(data, dict) else {}
            if delta.get("stop_reason") is not None:
                stop_reason = delta.get("stop_reason")
            if delta.get("stop_sequence") is not None:
                stop_sequence = delta.get("stop_sequence")
            u = data.get("usage") if isinstance(data, dict) else None
            if isinstance(u, dict):
                for k in ("output_tokens", "input_tokens",
                          "cache_creation_input_tokens", "cache_read_input_tokens"):
                    if u.get(k) is not None:
                        raw_usage[k] = u[k]

    content = [blocks[i] for i in order]

    def _ug(key: str) -> int:
        return int(raw_usage.get(key) or 0)

    usage = {
        "input_tokens": _ug("input_tokens"),
        "output_tokens": _ug("output_tokens"),
        "cache_creation_input_tokens": _ug("cache_creation_input_tokens"),
        "cache_read_input_tokens": _ug("cache_read_input_tokens"),
    }

    return {
        "id": msg_id,
        "type": "message",
        "role": role,
        "model": model,
        "content": content,
        "stop_reason": stop_reason or "end_turn",
        "stop_sequence": stop_sequence,
        "usage": usage,
    }


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


# ── AnthropicVertex native bridge (lazy client, built once) ───────────────────
def _make_vertex_completion(project: str, location: str):
    """Build the DEFAULT Vertex completion_fn — a NATIVE STREAMING ``AnthropicVertex``
    call that returns the native event iterator (relays Anthropic events verbatim).

    The ``anthropic`` SDK is the ONE heavy dep; ``AnthropicVertex`` is imported and
    constructed LAZILY (only on the first request) so ``import bench.usage_gateway``,
    the tests, and passthrough mode all work on a box without ``anthropic``. The
    client is thread-safe, so we build it ONCE and reuse it across requests. Auth
    is ADC (Application Default Credentials) — no API key passed; project/location
    select the Vertex endpoint.

    Returns a ``completion_fn(create_kwargs) -> iterable_of_events`` closure.
    ``create_kwargs`` already carries ``model`` (bare id) + the native Anthropic
    fields (and, when the inbound request forwarded any, ``extra_headers`` with a
    pre-filtered ``anthropic-beta`` — see ``_filter_vertex_betas``); we call
    ``stream=True`` and RETURN the SDK's event iterator. The events are native
    Anthropic SSE events (message_start, content_block_*, message_delta,
    message_stop) — the gateway relays them verbatim. ``stream=True`` is REQUIRED:
    Claude Code sends a large ``max_tokens`` and the SDK refuses the equivalent
    non-streaming call with ``ValueError: Streaming is required for operations that
    may take longer than 10 minutes`` — iterating a stream avoids that guard.

    SELF-HEALING against Vertex beta drift: ``_bridge_vertex`` pre-filters the
    KNOWN-unsupported betas, but if a NEWLY-unsupported beta slips through, Vertex
    400s with ``Unexpected value(s) `<beta>` ``. Rather than fail the run, we parse
    the offending beta token(s) out of that message, strip them from
    ``extra_headers["anthropic-beta"]``, and RETRY (up to 2 strips); if the header
    empties out we drop ``extra_headers`` entirely. So a beta that becomes
    unsupported tomorrow auto-recovers instead of breaking the panel.
    """
    cache: dict = {}

    def _complete(create_kwargs: dict):
        client = cache.get("client")
        if client is None:
            from anthropic import AnthropicVertex  # lazy — heavy, Vertex-mode only
            client = AnthropicVertex(project_id=project, region=location)
            cache["client"] = client

        # Up to 2 self-healing strips of a newly-unsupported beta, then give up
        # (re-raise) — the bridge's clean Anthropic-shaped error path handles it.
        for _ in range(3):
            try:
                return client.messages.create(stream=True, **create_kwargs)
            except Exception as e:  # noqa: BLE001 — match the message, no anthropic import
                msg = str(e)
                if "Unexpected value(s)" not in msg:
                    raise
                offenders = _betas_from_unexpected_value_error(msg)
                if not offenders or not _strip_betas_from_kwargs(create_kwargs, offenders):
                    raise  # nothing left to strip / nothing matched — propagate
        # exhausted retries — one last attempt so the real error surfaces
        return client.messages.create(stream=True, **create_kwargs)

    return _complete


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
                      Ignored in Vertex mode (AnthropicVertex is the upstream).
    sink            : where CallUsage rows are written.
    default_run_id  : run-id tag used when a request carries no RUN_ID_HEADER.
    default_headers : (passthrough mode) extra headers MERGED onto every forwarded
                      request. Incoming client headers win on conflict.
    timeout_s       : per-request upstream timeout (passthrough mode).
    mode            : ``MODE_VERTEX`` (default — gateway IS the AnthropicVertex
                      native bridge) or ``MODE_PASSTHROUGH`` (forward verbatim
                      upstream).
    vertex_*        : Vertex routing (model id, project, location); auth is ADC on
                      the box (no API key).
    completion_fn   : injectable Vertex completion (Vertex mode) — a callable
                      ``fn(create_kwargs) -> iterable_of_events`` returning the
                      native Anthropic stream events. Defaults to a lazily-built
                      native ``AnthropicVertex`` client called with ``stream=True``
                      (``_make_vertex_completion``). Tests pass a fake yielding plain
                      event dicts that mimic the SDK's events.
    """
    base = upstream_base.rstrip("/")
    extra_headers = dict(default_headers or {})
    _complete = completion_fn or _make_vertex_completion(vertex_project, vertex_location)

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

        # ── Vertex mode: NATIVE STREAMING passthrough (relays Anthropic events) ──
        def _bridge_vertex(self, body: bytes, run_id: str, t0: float) -> None:
            """Forward the inbound Anthropic request NATIVELY via AnthropicVertex
            with ``stream=True``, and RELAY THE NATIVE ANTHROPIC EVENTS VERBATIM —
            as an event-stream to a streaming client, or reconstructed into one JSON
            message for a non-streaming client. Captures usage off the events.

            count_tokens is handled locally (a cheap char-based estimate) so we
            never need a second upstream; the bench prices off the per-CALL usage
            rows from real messages, not count_tokens. The request IS already
            Anthropic Messages format and the target IS Anthropic-on-Vertex, and the
            stream events the SDK yields ARE native Anthropic SSE events — so this is
            a pure passthrough with ZERO translation (which kills the tool-call /
            shape bug class). ``stream=True`` also sidesteps the SDK's
            ``ValueError: Streaming is required ...`` guard that the large
            ``max_tokens`` Claude Code sends would otherwise trip on a non-streaming
            call. Any upstream (AnthropicVertex / ADC) error returns a clean
            Anthropic-shaped error (never a hang); usage we already have is logged.
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

            # Build native create kwargs (bare model id; native fields only). The
            # completion_fn calls stream=True and returns the native event iterator.
            model = _strip_vertex_prefix(vertex_model)
            kwargs = _anthropic_create_kwargs(req_body, model)
            # Forward Claude Code's negotiated betas (interleaved-thinking,
            # context-management, structured-outputs, …) to Vertex, pre-dropping the
            # ones Vertex rejects (prompt-caching-scope*); the completion_fn
            # self-heals on any newly-unsupported beta. Without this the agent
            # silently loses real capabilities — notably context-management, which
            # is how it sustains long/large-context runs.
            betas = _filter_vertex_betas(self.headers.get("anthropic-beta"))
            if betas:
                kwargs["extra_headers"] = {"anthropic-beta": betas}
            client_wants_stream = bool(req_body.get("stream"))
            try:
                event_stream = _complete(kwargs)
            except Exception as e:  # noqa: BLE001 — AnthropicVertex/upstream/ADC error
                self._gateway_error(502, f"vertex upstream error: "
                                    f"{type(e).__name__}: {str(e)[:200]}")
                return

            # Consume the native events. ITERATING the stream is what avoids the
            # SDK's "streaming required" guard; buffering the (type, data) pairs is
            # fine — they're small and we need the full set to capture final usage
            # and (for a non-stream client) reconstruct the message.
            try:
                ev_dicts = [(_event_type(e), _event_data(e)) for e in event_stream]
            except Exception as e:  # noqa: BLE001 — mid-stream upstream/ADC error
                self._gateway_error(502, f"vertex stream error: "
                                    f"{type(e).__name__}: {str(e)[:200]}")
                return

            # Pre-build the verbatim SSE frames once; usage is parsed from the same
            # frames (the cache split rides on message_start + message_delta).
            frames = [_sse_event(t, d) for (t, d) in ev_dicts]
            try:
                usage = usage_from_sse(b"".join(frames).decode("utf-8", "replace"))
                row = extract_usage(usage, time.time() - t0)
                if row is not None:
                    sink.write(run_id, row)
            except Exception as e:  # noqa: BLE001 — logging must never break the run
                print(f"  usage_gateway WARN: vertex usage capture failed: "
                      f"{type(e).__name__}: {str(e)[:160]}", flush=True)

            try:
                if client_wants_stream:
                    self._relay_frames_sse(frames)
                else:
                    self._relay_anth_json(_accumulate_anthropic_message(ev_dicts, model))
            except (BrokenPipeError, ConnectionError):
                return  # client hung up — never crash

        def _relay_anth_json(self, anth: dict) -> None:
            data = json.dumps(anth).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _relay_frames_sse(self, frames: list) -> None:
            """Relay pre-built native Anthropic SSE event frames verbatim."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            for frame in frames:
                self.wfile.write(frame)
                self.wfile.flush()

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

    Usage (Vertex mode — the default; the gateway IS the AnthropicVertex bridge):
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
                    help="vertex: AnthropicVertex native bridge (default); "
                         "passthrough: forward verbatim to --upstream")
    ap.add_argument("--upstream", default=os.environ.get("CCB_GATEWAY_UPSTREAM",
                                                          "https://api.anthropic.com"),
                    help="(passthrough mode) upstream base URL to forward to")
    ap.add_argument("--vertex-model", default=VERTEX_MODEL,
                    help="(vertex mode) Vertex Claude model id "
                         "(a leading 'vertex_ai/' is stripped for AnthropicVertex)")
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
