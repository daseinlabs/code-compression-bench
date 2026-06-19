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
    anthropic_request_to_litellm_args, litellm_response_to_anthropic,
    anthropic_message_to_sse,
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


# ── 4. Vertex mode: mocked litellm.completion -> Anthropic translation ────────
# A fake litellm Usage object: OpenAI-shaped prompt_tokens is the FULL input
# (uncached 120 + write 800 + read 5000 = 5920); the Anthropic cache split lives
# on the private attrs litellm stuffs it into. Our translator must recover the
# split and re-emit Anthropic's input_tokens (= uncached = 120).
def _fake_usage():
    u = SimpleNamespace(
        prompt_tokens=5920, completion_tokens=45,
        _cache_creation_input_tokens=800, _cache_read_input_tokens=5000,
        prompt_tokens_details=None,
    )
    return u


def _fake_litellm_response():
    """An OpenAI-shaped litellm ModelResponse with a text reply + the cache split."""
    msg = SimpleNamespace(content="done", tool_calls=None)
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(id="chatcmpl-x", choices=[choice], usage=_fake_usage())


def _fake_litellm_toolcall_response():
    """An OpenAI-shaped litellm response whose message is a TOOL CALL — args as a
    JSON STRING, the way OpenAI/litellm return them. The gateway must parse the
    string into an Anthropic tool_use ``input`` OBJECT; getting this wrong is what
    made Claude Code report "the model's tool call could not be parsed" and fail
    the whole run."""
    fn = SimpleNamespace(name="Bash", arguments='{"command": "ls -la", "timeout": 5}')
    tc = SimpleNamespace(id="toolu_abc", type="function", function=fn)
    msg = SimpleNamespace(content=None, tool_calls=[tc])
    choice = SimpleNamespace(message=msg, finish_reason="tool_calls")
    return SimpleNamespace(id="chatcmpl-tool", choices=[choice], usage=_fake_usage())


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


def test_anthropic_request_to_litellm_args():
    """An inbound Anthropic request becomes the right Vertex litellm args."""
    body = {
        "model": "claude-sonnet-4-5",
        "messages": [{"role": "user", "content": "hi"}],
        "system": "be terse",
        "max_tokens": 1024,
        "temperature": 0.2,
        "tools": [{"name": "bash", "input_schema": {"type": "object"}}],
        "stream": False,
    }
    args = anthropic_request_to_litellm_args(
        body, "vertex_ai/claude-sonnet-4-6", "dasein-473321", "us-east5")
    assert args["model"] == "vertex_ai/claude-sonnet-4-6"
    assert args["vertex_project"] == "dasein-473321"
    assert args["vertex_location"] == "us-east5"
    assert args["messages"] == body["messages"]
    assert args["system"] == "be terse"
    assert args["max_tokens"] == 1024
    assert args["temperature"] == 0.2
    assert args["tools"] == body["tools"]
    assert args["stream"] is False
    # ADC auth: no api_key smuggled in
    assert "api_key" not in args


def test_litellm_response_to_anthropic_cache_split():
    """A litellm response with a cache split relays as Anthropic format + usage."""
    anth = litellm_response_to_anthropic(_fake_litellm_response(), "vertex_ai/claude-sonnet-4-6")
    assert anth["type"] == "message"
    assert anth["role"] == "assistant"
    assert anth["stop_reason"] == "end_turn"
    assert anth["content"] == [{"type": "text", "text": "done"}]
    # Anthropic usage: input_tokens is the UNCACHED portion (120), split separate.
    u = anth["usage"]
    assert u["input_tokens"] == 120
    assert u["output_tokens"] == 45
    assert u["cache_creation_input_tokens"] == 800
    assert u["cache_read_input_tokens"] == 5000
    # and extract_usage normalizes it to the same CallUsage row as the wire paths
    _assert_row(extract_usage(u))


def _through_vertex_gateway(stream: bool):
    """A request through a Vertex-mode gateway with a MOCKED litellm.completion.

    Asserts the gateway (a) relayed an Anthropic-format response and (b) wrote the
    right CallUsage row (cache split) to the per-run JSONL — for BOTH the JSON and
    SSE paths.
    """
    tmp = tempfile.mkdtemp(prefix="ccb_gw_vtx_")
    seen = {}

    def fake_completion(args):
        seen["args"] = args                # capture the translated litellm args
        return _fake_litellm_response()    # gateway always calls litellm NON-streaming

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
        # (a) the mocked completion got Vertex-routed args, ALWAYS non-streaming
        # (the gateway buffers upstream and re-emits SSE itself — so tool calls
        # translate correctly off the complete message).
        assert seen["args"]["model"].startswith("vertex_ai/")
        assert seen["args"]["vertex_project"]
        assert seen["args"]["stream"] is False
        # (a') the gateway emitted Anthropic format (JSON message OR SSE events)
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


def test_vertex_tool_call_survives_json_and_sse():
    """A model TOOL CALL must round-trip as a well-formed Anthropic tool_use with a
    PARSED input object — both as JSON and over SSE. Regression: the streaming path
    dropped tool calls -> Claude Code reported 'the model's tool call could not be
    parsed' -> the whole run failed (the A0 smoke caught exactly this)."""
    # (a) non-streaming translation parses the arguments-STRING into an input OBJECT
    anth = litellm_response_to_anthropic(
        _fake_litellm_toolcall_response(), "vertex_ai/claude-sonnet-4-6")
    tu = [b for b in anth["content"] if b["type"] == "tool_use"]
    assert len(tu) == 1
    assert tu[0]["name"] == "Bash" and tu[0]["id"] == "toolu_abc"
    assert tu[0]["input"] == {"command": "ls -la", "timeout": 5}   # parsed, NOT a string
    assert anth["stop_reason"] == "tool_use"
    # (b) SSE re-emission: tool_use content_block_start carries id+name, and the
    # input rides as an input_json_delta partial_json that parses back to the object
    blocks = _parse_sse_blocks(b"".join(anthropic_message_to_sse(anth)).decode())
    tu_start = [d for e, d in blocks
                if e == "content_block_start" and d["content_block"]["type"] == "tool_use"]
    assert len(tu_start) == 1
    assert tu_start[0]["content_block"]["id"] == "toolu_abc"
    assert tu_start[0]["content_block"]["name"] == "Bash"
    ijd = [d for e, d in blocks
           if e == "content_block_delta" and d.get("delta", {}).get("type") == "input_json_delta"]
    assert len(ijd) == 1
    assert json.loads(ijd[0]["delta"]["partial_json"]) == {"command": "ls -la", "timeout": 5}


def test_vertex_gateway_upstream_error_is_clean():
    """A litellm error returns a clean Anthropic-shaped error — never a hang."""
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
