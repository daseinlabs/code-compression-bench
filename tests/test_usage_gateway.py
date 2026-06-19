"""Unit tests for bench.usage_gateway — the usage-capturing reverse-proxy.

These assert the ONE thing the gateway must get right: it extracts the Anthropic
cache split (prompt/completion/cache_creation/cache_read) into a CallUsage row,
from BOTH response shapes the Messages API uses:

  1. a non-streaming JSON body (usage object on the response);
  2. a streaming SSE body (usage split across message_start + message_delta).

Two layers of coverage, both pure-Python (no claude_agent_sdk, no node):

  * unit: feed extract_usage / usage_from_sse the sample payloads directly and
    assert the parsed CallUsage row;
  * integration: stand up a MOCK upstream HTTP server that returns those exact
    payloads, point a real UsageGateway at it, fire a request through the gateway
    (urllib), and assert the gateway (a) relayed the body faithfully AND (b) wrote
    the right CallUsage row to the per-run JSONL.

The Vertex-mode tests inject a fake completion_fn (standing in for the native
AnthropicVertex client called with ``stream=True``) returning an iterable of plain
event DICTS that mimic the SDK's native Anthropic stream events, so the suite runs
on a box WITHOUT the ``anthropic`` SDK installed — including the regression that a
conversation carrying a tool_result message + tools round-trips natively (the
relayed body is native SSE for a streaming client; a reconstructed JSON message for
a non-streaming client) with the tool_use ``input`` a parsed OBJECT and the cache
split captured.

Runnable two ways:
    py -m pytest tests/test_usage_gateway.py -q
    py tests/test_usage_gateway.py            # standalone, prints PASS/FAIL
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

# make the repo root importable when run as a bare script (py tests/...).
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bench.usage_gateway import (  # noqa: E402
    MODE_PASSTHROUGH, RUN_ID_HEADER, UsageGateway, extract_usage, usage_from_sse,
    _anthropic_create_kwargs, _strip_vertex_prefix,
    _accumulate_anthropic_message, _event_type, _event_data,
)


# ── sample payloads (real Anthropic Messages API shapes) ─────────────────────
# A non-streaming /v1/messages response. usage.input_tokens is the UNCACHED new
# input; the cache split is reported separately.
SAMPLE_JSON_RESPONSE = {
    "id": "msg_01ABC",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-4-5",
    "content": [{"type": "text", "text": "done"}],
    "stop_reason": "end_turn",
    "usage": {
        "input_tokens": 120,                 # uncached new input
        "output_tokens": 45,
        "cache_creation_input_tokens": 800,  # cache WRITE
        "cache_read_input_tokens": 5000,     # cache READ
    },
}

# A streaming /v1/messages SSE body: message_start carries the input side + the
# cache split; message_delta carries the running output_tokens.
SAMPLE_SSE_BODY = (
    "event: message_start\n"
    'data: {"type":"message_start","message":{"id":"msg_01X","model":"claude-sonnet-4-5",'
    '"usage":{"input_tokens":120,"output_tokens":1,'
    '"cache_creation_input_tokens":800,"cache_read_input_tokens":5000}}}\n\n'
    "event: content_block_start\n"
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
    "event: content_block_delta\n"
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n'
    "event: message_delta\n"
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
    '"usage":{"output_tokens":45}}\n\n'
    "event: message_stop\n"
    'data: {"type":"message_stop"}\n\n'
    "data: [DONE]\n\n"
)

# Expected CallUsage after normalization: prompt_tokens == FULL billable input
# (uncached 120 + write 800 + read 5000 = 5920); completion == 45.
EXPECTED = {
    "prompt_tokens": 5920,
    "completion_tokens": 45,
    "cache_creation_input_tokens": 800,
    "cache_read_input_tokens": 5000,
}


def _assert_row(row: dict) -> None:
    assert row is not None, "expected a CallUsage row, got None"
    for k, v in EXPECTED.items():
        assert row.get(k) == v, f"{k}: expected {v}, got {row.get(k)}"


# ── 1. unit: extract_usage off a JSON response usage object ──────────────────
def test_extract_usage_json_shape():
    row = extract_usage(SAMPLE_JSON_RESPONSE["usage"], latency_s=1.23)
    _assert_row(row)
    assert row["latency_s"] == 1.23


def test_extract_usage_no_cache_fields():
    # an all-cold call (no cache split reported): prompt == input, no cache keys.
    row = extract_usage({"input_tokens": 300, "output_tokens": 10})
    assert row["prompt_tokens"] == 300
    assert row["completion_tokens"] == 10
    assert "cache_creation_input_tokens" not in row
    assert "cache_read_input_tokens" not in row


def test_extract_usage_empty_returns_none():
    assert extract_usage(None) is None
    assert extract_usage({}) is None
    assert extract_usage({"input_tokens": 0, "output_tokens": 0}) is None


# ── 2. unit: usage_from_sse merges message_start + message_delta ──────────────
def test_usage_from_sse_merges_events():
    merged = usage_from_sse(SAMPLE_SSE_BODY)
    assert merged is not None
    # output_tokens comes from the message_delta (running total), not message_start
    assert merged["output_tokens"] == 45
    assert merged["input_tokens"] == 120
    assert merged["cache_creation_input_tokens"] == 800
    assert merged["cache_read_input_tokens"] == 5000
    # and the full extraction yields the same normalized row as the JSON path
    _assert_row(extract_usage(merged))


def test_usage_from_sse_no_usage_returns_none():
    body = ("event: ping\ndata: {\"type\":\"ping\"}\n\n"
            "data: [DONE]\n\n")
    assert usage_from_sse(body) is None


# ── mock upstream (returns the sample payloads) ──────────────────────────────
def _make_mock_upstream(stream: bool):
    """A tiny HTTP server that echoes a fixed Anthropic-shaped response.

    Returns (server, base_url). stream=True replies with the SSE body +
    text/event-stream; stream=False with the JSON body. It also echoes back a
    header so the integration test can confirm faithful header relay.
    """
    body = (SAMPLE_SSE_BODY.encode() if stream
            else json.dumps(SAMPLE_JSON_RESPONSE).encode())
    ctype = "text/event-stream" if stream else "application/json"

    class _Mock(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence
            return

        def do_POST(self):  # noqa: N802
            # drain the request body so the connection closes cleanly
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                self.rfile.read(n)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("X-Mock-Echo", "ok")
            if not stream:
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Mock)
    Thread(target=srv.serve_forever, daemon=True).start()
    host, port = srv.server_address
    return srv, f"http://{host}:{port}"


# ── 3. integration: a request through the real gateway logs the right row ─────
def _through_gateway(stream: bool):
    mock_srv, upstream = _make_mock_upstream(stream)
    tmp = tempfile.mkdtemp(prefix="ccb_gw_")
    # passthrough mode: the gateway forwards verbatim to the mock Anthropic upstream
    # (the original behaviour). Vertex mode is covered separately below.
    gw = UsageGateway(upstream_base=upstream, log_dir=tmp,
                      default_run_id="rid-1", mode=MODE_PASSTHROUGH).start()
    try:
        req = urllib.request.Request(
            gw.base_url + "/v1/messages",
            data=json.dumps({"model": "claude-sonnet-4-5",
                             "messages": [{"role": "user", "content": "hi"}]}).encode(),
            method="POST",
            headers={"Content-Type": "application/json",
                     "anthropic-version": "2023-06-01",
                     "anthropic-beta": "prompt-caching-2024-07-31",
                     "Authorization": "Bearer sk-test",
                     RUN_ID_HEADER: "rid-1"},
        )
        resp = urllib.request.urlopen(req, timeout=10)
        relayed = resp.read()
        # (a) the gateway relayed the upstream body + headers faithfully
        assert resp.headers.get("X-Mock-Echo") == "ok"
        assert b"usage" in relayed or b"message_delta" in relayed
        # (b) the gateway wrote the right CallUsage row to the per-run JSONL. The
        # streaming path logs usage on the handler thread just after the client
        # sees stream-end, so poll briefly for the row to appear (a real reader —
        # the runner — reads after gw.stop() drains the server, so this race is
        # test-only). The non-streaming path logs BEFORE relaying, so it's instant.
        path = gw.usage_path("rid-1")
        lines: list[str] = []
        for _ in range(50):  # up to ~1s
            if path.exists():
                lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
                if lines:
                    break
            time.sleep(0.02)
        assert len(lines) == 1, f"expected exactly one usage row, got {len(lines)}"
        _assert_row(json.loads(lines[0]))
    finally:
        gw.stop()
        mock_srv.shutdown()
        mock_srv.server_close()


def test_gateway_logs_usage_json_path():
    _through_gateway(stream=False)


def test_gateway_logs_usage_sse_path():
    _through_gateway(stream=True)


# ── 4. Vertex mode: mocked AnthropicVertex STREAM (native passthrough) ────────
# The default completion_fn now calls AnthropicVertex with stream=True and returns
# the native event iterator; tests inject a fake returning an iterable of plain
# event DICTS that mimic the SDK's events. message_start.message.usage carries the
# cache split EXACTLY as Anthropic reports it: input_tokens is the UNCACHED count
# (120), the cache split is separate (write 800, read 5000); message_delta carries
# the running output_tokens (45). No OpenAI-shape, no translation — pure passthrough.
_START_USAGE = {
    "input_tokens": 120, "output_tokens": 1,
    "cache_creation_input_tokens": 800, "cache_read_input_tokens": 5000,
}
_DELTA_USAGE = {"output_tokens": 45}


def _event_dict(etype, **rest):
    """A plain event dict mimicking an SDK stream event's ``.model_dump()`` — it
    carries a top-level ``"type"`` key (the SDK's dump does too)."""
    d = {"type": etype}
    d.update(rest)
    return d


def _fake_event_stream(model="claude-sonnet-4-6", *, tool=False):
    """Yield plain event DICTS that mimic the AnthropicVertex stream=True events.

    A text reply (``done``) by default, or a tool_use block emitted via a
    content_block_start{type:tool_use,id,name} + input_json_delta + content_block_stop
    when ``tool=True`` (the regression shape: the model's tool call must survive as
    a well-formed tool_use whose ``input`` parses back to the OBJECT). The cache
    split rides on message_start.message.usage; output_tokens on message_delta.
    """
    yield _event_dict("message_start", message={
        "id": "msg_vtx_x", "type": "message", "role": "assistant", "model": model,
        "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": dict(_START_USAGE),
    })
    if tool:
        yield _event_dict("content_block_start", index=0, content_block={
            "type": "tool_use", "id": "toolu_abc", "name": "Bash", "input": {}})
        yield _event_dict("content_block_delta", index=0, delta={
            "type": "input_json_delta",
            "partial_json": json.dumps({"command": "ls -la", "timeout": 5})})
        yield _event_dict("content_block_stop", index=0)
        stop_reason = "tool_use"
    else:
        yield _event_dict("content_block_start", index=0, content_block={
            "type": "text", "text": ""})
        yield _event_dict("content_block_delta", index=0, delta={
            "type": "text_delta", "text": "done"})
        yield _event_dict("content_block_stop", index=0)
        stop_reason = "end_turn"
    yield _event_dict("message_delta",
                      delta={"stop_reason": stop_reason, "stop_sequence": None},
                      usage=dict(_DELTA_USAGE))
    yield _event_dict("message_stop")


def _parse_sse_blocks(sse_text: str):
    """Pull (event, data-dict) pairs out of an SSE body for assertions."""
    out = []
    for chunk in sse_text.split("\n\n"):
        ev = dat = None
        for line in chunk.splitlines():
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                dat = json.loads(line[5:].strip())
        if ev and dat is not None:
            out.append((ev, dat))
    return out


def test_strip_vertex_prefix():
    """AnthropicVertex wants the BARE model id — the 'vertex_ai/' prefix is stripped."""
    assert _strip_vertex_prefix("vertex_ai/claude-sonnet-4-6") == "claude-sonnet-4-6"
    # already-bare ids and unrelated strings pass through untouched
    assert _strip_vertex_prefix("claude-sonnet-4-6") == "claude-sonnet-4-6"
    assert _strip_vertex_prefix("") == ""


def test_anthropic_create_kwargs_passthrough():
    """An inbound Anthropic body becomes native AnthropicVertex create kwargs.

    Native fields present in the body pass through verbatim; absent ones are NOT
    fabricated; ``stream`` is dropped (handled separately); ``model`` is forced to
    the configured Vertex (bare) id, NOT the client's; ``max_tokens`` defaults.
    """
    body = {
        "model": "claude-sonnet-4-5",                # client's model — IGNORED
        "messages": [{"role": "user", "content": "hi"}],
        "system": [{"type": "text", "text": "be terse",
                    "cache_control": {"type": "ephemeral"}}],
        "max_tokens": 1024,
        "temperature": 0.2,
        "tools": [{"name": "Bash", "input_schema": {"type": "object"}}],
        "tool_choice": {"type": "auto"},
        "thinking": {"type": "enabled", "budget_tokens": 1000},
        "stream": True,                              # handled separately — NOT in kwargs
    }
    kw = _anthropic_create_kwargs(body, "claude-sonnet-4-6")
    assert kw["model"] == "claude-sonnet-4-6"        # the configured Vertex model, bare
    assert kw["messages"] == body["messages"]
    assert kw["system"] == body["system"]            # cache_control rides through
    assert kw["max_tokens"] == 1024
    assert kw["temperature"] == 0.2
    assert kw["tools"] == body["tools"]
    assert kw["tool_choice"] == {"type": "auto"}
    assert kw["thinking"] == {"type": "enabled", "budget_tokens": 1000}
    # stream is NOT forwarded (the gateway re-emits SSE itself)
    assert "stream" not in kw
    # ADC auth: no api_key smuggled in
    assert "api_key" not in kw
    # absent native fields are not fabricated
    assert "top_p" not in kw and "stop_sequences" not in kw and "metadata" not in kw


def test_anthropic_create_kwargs_defaults_max_tokens():
    """max_tokens defaults to 4096 when the inbound body omits it."""
    kw = _anthropic_create_kwargs(
        {"messages": [{"role": "user", "content": "hi"}]}, "claude-sonnet-4-6")
    assert kw["max_tokens"] == 4096
    assert kw["messages"] == [{"role": "user", "content": "hi"}]


def test_event_helpers_accept_dicts_and_objects():
    """_event_type / _event_data work on plain dicts AND SDK-like objects, and the
    data dict always carries a ``"type"`` key (set from the event type if missing)."""
    # plain dict (what the fake stream yields)
    d = _event_dict("message_stop")
    assert _event_type(d) == "message_stop"
    assert _event_data(d) == {"type": "message_stop"}
    # an SDK-like object: .type + .model_dump() (dump lacks "type" -> we add it)
    obj = SimpleNamespace(type="message_stop", model_dump=lambda: {"foo": "bar"})
    assert _event_type(obj) == "message_stop"
    data = _event_data(obj)
    assert data["foo"] == "bar"
    assert data["type"] == "message_stop"      # injected from _event_type


def test_accumulate_anthropic_message_text_cache_split():
    """Accumulating a native event stream reconstructs the Anthropic message dict +
    usage cache split (for the NON-streaming client path)."""
    ev_dicts = [(_event_type(e), _event_data(e))
                for e in _fake_event_stream("claude-sonnet-4-6")]
    anth = _accumulate_anthropic_message(ev_dicts, "claude-sonnet-4-6")
    assert anth["type"] == "message"
    assert anth["role"] == "assistant"
    assert anth["model"] == "claude-sonnet-4-6"
    assert anth["stop_reason"] == "end_turn"
    assert anth["content"] == [{"type": "text", "text": "done"}]
    # usage is a PLAIN dict; native input_tokens is the UNCACHED count (120), kept
    # as-is (NOT re-derived); the cache split is separate; output_tokens from delta.
    u = anth["usage"]
    assert isinstance(u, dict)
    assert u["input_tokens"] == 120
    assert u["output_tokens"] == 45
    assert u["cache_creation_input_tokens"] == 800
    assert u["cache_read_input_tokens"] == 5000
    # and extract_usage normalizes it to the same CallUsage row as the wire paths
    _assert_row(extract_usage(u))


def test_accumulate_anthropic_message_tool_use_input_is_object():
    """A tool_use stream accumulates into a block whose ``input`` is a PARSED OBJECT
    (the partial_json fragments are buffered then json.loads'd at content_block_stop),
    and a missing cache split coerces to 0."""
    # build a tool stream whose message_start reports no cache split, to also check
    # the None -> 0 coercion.
    def _stream():
        yield _event_dict("message_start", message={
            "id": "msg_d", "type": "message", "role": "assistant",
            "usage": {"input_tokens": 50, "output_tokens": 1}})  # no cache split
        yield _event_dict("content_block_start", index=0, content_block={
            "type": "tool_use", "id": "toolu_x", "name": "Edit", "input": {}})
        # input arrives as TWO partial_json fragments — must concatenate then parse
        yield _event_dict("content_block_delta", index=0, delta={
            "type": "input_json_delta", "partial_json": '{"path":'})
        yield _event_dict("content_block_delta", index=0, delta={
            "type": "input_json_delta", "partial_json": ' "a.py"}'})
        yield _event_dict("content_block_stop", index=0)
        yield _event_dict("message_delta",
                          delta={"stop_reason": "tool_use"}, usage={"output_tokens": 3})
        yield _event_dict("message_stop")

    ev_dicts = [(_event_type(e), _event_data(e)) for e in _stream()]
    anth = _accumulate_anthropic_message(ev_dicts, "claude-sonnet-4-6")
    tu = [b for b in anth["content"] if b["type"] == "tool_use"]
    assert tu[0]["input"] == {"path": "a.py"}        # parsed object, not a string
    assert anth["stop_reason"] == "tool_use"
    assert anth["usage"]["cache_creation_input_tokens"] == 0   # None -> 0
    assert anth["usage"]["cache_read_input_tokens"] == 0


def _through_vertex_gateway(stream: bool):
    """A request through a Vertex-mode gateway with a MOCKED completion_fn.

    The injected completion_fn stands in for the native AnthropicVertex client
    called with ``stream=True`` and returns an iterable of native event dicts.
    Asserts the gateway (a) called it with native create kwargs (bare model id, no
    foreign routing args, no ``stream``) and (b) relayed the native events VERBATIM
    as SSE (streaming client) or a reconstructed JSON message (non-streaming
    client), and wrote the right CallUsage row (cache split) to the per-run JSONL.
    """
    tmp = tempfile.mkdtemp(prefix="ccb_gw_vtx_")
    seen = {}

    def fake_completion(create_kwargs):
        seen["kwargs"] = create_kwargs       # capture the native create kwargs
        return _fake_event_stream()          # native Anthropic stream events

    gw = UsageGateway(log_dir=tmp, default_run_id="rid-v",
                      completion_fn=fake_completion).start()  # mode defaults to vertex
    try:
        req = urllib.request.Request(
            gw.base_url + "/v1/messages",
            data=json.dumps({"model": "claude-sonnet-4-5",
                             "max_tokens": 512,
                             "messages": [{"role": "user", "content": "hi"}],
                             "stream": stream}).encode(),
            method="POST",
            headers={"Content-Type": "application/json", RUN_ID_HEADER: "rid-v"},
        )
        resp = urllib.request.urlopen(req, timeout=10)
        relayed = resp.read()
        # (a) the mocked completion got native create kwargs: the BARE configured
        # Vertex model (the leading 'vertex_ai/' stripped), no foreign routing args,
        # and NO ``stream`` in the kwargs (the completion_fn adds stream=True itself).
        kw = seen["kwargs"]
        assert kw["model"] == "claude-sonnet-4-6"   # bare id, not the client's model
        assert "vertex_ai/" not in kw["model"]
        assert "stream" not in kw
        assert "vertex_project" not in kw and "vertex_location" not in kw
        # (a') the gateway emitted Anthropic format: native SSE events relayed
        # verbatim (streaming client) OR a reconstructed JSON message.
        if stream:
            assert resp.headers.get("Content-Type", "").startswith("text/event-stream")
            blocks = _parse_sse_blocks(relayed.decode())
            evs = [e for e, _ in blocks]
            assert "message_start" in evs and "message_delta" in evs and "message_stop" in evs
            deltas = [d for e, d in blocks if e == "content_block_delta"]
            assert any(d.get("delta", {}).get("text") == "done" for d in deltas)
        else:
            assert resp.headers.get("Content-Type", "").startswith("application/json")
            obj = json.loads(relayed)
            assert obj["type"] == "message"
            assert obj["content"] == [{"type": "text", "text": "done"}]
        # (b) the CallUsage row captures the cache split
        path = gw.usage_path("rid-v")
        lines: list[str] = []
        for _ in range(50):
            if path.exists():
                lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
                if lines:
                    break
            time.sleep(0.02)
        assert len(lines) == 1, f"expected exactly one usage row, got {len(lines)}"
        _assert_row(json.loads(lines[0]))
    finally:
        gw.stop()


def test_vertex_gateway_json_round_trip():
    _through_vertex_gateway(stream=False)


def test_vertex_gateway_sse_round_trip():
    _through_vertex_gateway(stream=True)


def test_vertex_tool_call_survives_native_events():
    """A model TOOL CALL must survive the native passthrough as a well-formed
    Anthropic tool_use with a PARSED input object. Regression: the old litellm
    streaming path dropped tool calls -> Claude Code reported 'the model's tool
    call could not be parsed' -> the whole run failed (the A0 smoke caught exactly
    this). The native streaming passthrough relays the tool_use events verbatim;
    the JSON path reconstructs the block off those same events."""
    events = list(_fake_event_stream("claude-sonnet-4-6", tool=True))
    ev_dicts = [(_event_type(e), _event_data(e)) for e in events]

    # (a) the native event stream relayed VERBATIM as SSE keeps the tool_use shape:
    # content_block_start carries id+name, and the input rides as an input_json_delta
    # partial_json that parses back to the OBJECT.
    from bench.usage_gateway import _sse_event
    frames = b"".join(_sse_event(t, d) for (t, d) in ev_dicts)
    blocks = _parse_sse_blocks(frames.decode())
    tu_start = [d for e, d in blocks
                if e == "content_block_start" and d["content_block"]["type"] == "tool_use"]
    assert len(tu_start) == 1
    assert tu_start[0]["content_block"]["id"] == "toolu_abc"
    assert tu_start[0]["content_block"]["name"] == "Bash"
    ijd = [d for e, d in blocks
           if e == "content_block_delta" and d.get("delta", {}).get("type") == "input_json_delta"]
    assert len(ijd) == 1
    assert json.loads(ijd[0]["delta"]["partial_json"]) == {"command": "ls -la", "timeout": 5}

    # (b) the NON-stream client path reconstructs a tool_use block whose input is a
    # PARSED OBJECT (NOT a string) off those same events.
    anth = _accumulate_anthropic_message(ev_dicts, "claude-sonnet-4-6")
    tu = [b for b in anth["content"] if b["type"] == "tool_use"]
    assert len(tu) == 1
    assert tu[0]["name"] == "Bash" and tu[0]["id"] == "toolu_abc"
    assert tu[0]["input"] == {"command": "ls -la", "timeout": 5}   # parsed, NOT a string
    assert anth["stop_reason"] == "tool_use"


def _tool_result_conversation_through_gateway(stream: bool):
    """A REAL conversation that carries a tool_result message + tools must
    round-trip through the gateway (JSON and SSE), the tool_use response coming
    back with its ``input`` a PARSED object and the cache split captured.

    This is the end-to-end regression for the native passthrough: the inbound body
    is Anthropic-native (a user turn, an assistant ``tool_use`` turn, a user
    ``tool_result`` turn, plus a ``tools`` array with cache_control), it reaches
    the completion_fn UNTRANSLATED, and the gateway re-emits the model's tool_use
    reply faithfully.
    """
    tmp = tempfile.mkdtemp(prefix="ccb_gw_tr_")
    seen = {}

    def fake_completion(create_kwargs):
        seen["kwargs"] = create_kwargs
        return _fake_event_stream(tool=True)   # model answers with a tool_use

    gw = UsageGateway(log_dir=tmp, default_run_id="rid-tr",
                      completion_fn=fake_completion).start()
    try:
        conversation = {
            "model": "claude-sonnet-4-5",
            "max_tokens": 512,
            "stream": stream,
            "system": [{"type": "text", "text": "be terse",
                        "cache_control": {"type": "ephemeral"}}],
            "tools": [{"name": "Bash", "description": "run a shell command",
                       "input_schema": {"type": "object"},
                       "cache_control": {"type": "ephemeral"}}],
            "messages": [
                {"role": "user", "content": "list the files"},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "toolu_prev", "name": "Bash",
                     "input": {"command": "pwd"}}]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_prev",
                     "content": "/repo"}]},
            ],
        }
        req = urllib.request.Request(
            gw.base_url + "/v1/messages",
            data=json.dumps(conversation).encode(),
            method="POST",
            headers={"Content-Type": "application/json", RUN_ID_HEADER: "rid-tr"},
        )
        resp = urllib.request.urlopen(req, timeout=10)
        relayed = resp.read()
        # (a) the native fields reached the completion_fn UNTRANSLATED — the
        # tool_result message + tools + cache_control all rode through verbatim.
        kw = seen["kwargs"]
        assert kw["messages"] == conversation["messages"]    # tool_result intact
        assert kw["tools"] == conversation["tools"]          # tools + cache_control intact
        assert kw["system"] == conversation["system"]
        # (b) the model's tool_use reply round-trips with a PARSED input object
        if stream:
            assert resp.headers.get("Content-Type", "").startswith("text/event-stream")
            blocks = _parse_sse_blocks(relayed.decode())
            tu_start = [d for e, d in blocks
                        if e == "content_block_start"
                        and d["content_block"]["type"] == "tool_use"]
            assert len(tu_start) == 1 and tu_start[0]["content_block"]["name"] == "Bash"
            ijd = [d for e, d in blocks
                   if e == "content_block_delta"
                   and d.get("delta", {}).get("type") == "input_json_delta"]
            assert json.loads(ijd[0]["delta"]["partial_json"]) == {"command": "ls -la", "timeout": 5}
        else:
            obj = json.loads(relayed)
            tu = [b for b in obj["content"] if b["type"] == "tool_use"]
            assert len(tu) == 1 and tu[0]["name"] == "Bash"
            assert tu[0]["input"] == {"command": "ls -la", "timeout": 5}   # OBJECT, not str
        # (c) the cache split was captured in the usage row
        path = gw.usage_path("rid-tr")
        lines: list[str] = []
        for _ in range(50):
            if path.exists():
                lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
                if lines:
                    break
            time.sleep(0.02)
        assert len(lines) == 1, f"expected exactly one usage row, got {len(lines)}"
        _assert_row(json.loads(lines[0]))
    finally:
        gw.stop()


def test_vertex_tool_result_conversation_json():
    _tool_result_conversation_through_gateway(stream=False)


def test_vertex_tool_result_conversation_sse():
    _tool_result_conversation_through_gateway(stream=True)


def test_vertex_gateway_upstream_error_is_clean():
    """An AnthropicVertex/upstream error returns a clean Anthropic-shaped error —
    never a hang."""
    tmp = tempfile.mkdtemp(prefix="ccb_gw_err_")

    def boom(args):
        raise RuntimeError("vertex exploded")

    gw = UsageGateway(log_dir=tmp, default_run_id="rid-e",
                      completion_fn=boom).start()
    try:
        req = urllib.request.Request(
            gw.base_url + "/v1/messages",
            data=json.dumps({"max_tokens": 16,
                             "messages": [{"role": "user", "content": "hi"}]}).encode(),
            method="POST",
            headers={"Content-Type": "application/json", RUN_ID_HEADER: "rid-e"},
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            got_code = 200
            body = b""
        except urllib.error.HTTPError as e:
            got_code = e.code
            body = e.read()
        assert got_code == 502, f"expected a clean 502, got {got_code}"
        err = json.loads(body)
        assert err.get("type") == "error"
        assert "vertex exploded" in json.dumps(err)
    finally:
        gw.stop()


# ── standalone runner (py tests/test_usage_gateway.py) ───────────────────────
def _run_standalone() -> int:
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL  {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
