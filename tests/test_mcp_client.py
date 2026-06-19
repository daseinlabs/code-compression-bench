"""Unit tests for bench.mcp_client against a TINY mock stdio MCP server.

The mock (``tests/mock_mcp_server.py``) speaks the same newline-delimited (and,
under a knob, Content-Length) JSON-RPC the real Woz ``code-server.js --stdio``
speaks. These tests assert the three things the bridge must get right:

  1. the MCP handshake (initialize + notifications/initialized) succeeds and the
     server's advertised info/capabilities are captured;
  2. ``tools/list`` returns the REAL tool schemas the server advertises
     (discovery — not a hand-mirrored client list), incl. cursor pagination;
  3. a ``tools/call`` round-trips: arguments go out, text content comes back;
     unknown tools surface as errors; and a hung server TIMES OUT rather than
     deadlocking the run.

Runnable two ways (no node required — the mock is pure Python):
    py -m pytest tests/test_mcp_client.py -q
    py tests/test_mcp_client.py            # standalone, prints PASS/FAIL
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# make the repo root importable when run as a bare script (py tests/...).
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bench.mcp_client import MCPClient, MCPError, MCPTimeout, _concat_text_content  # noqa: E402

_MOCK = str(Path(__file__).resolve().parent / "mock_mcp_server.py")


def _spawn(env_extra: dict | None = None, **spawn_kw) -> MCPClient:
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    c = MCPClient()
    c.spawn([sys.executable, _MOCK], env=env, **spawn_kw)
    return c


# ── 1. handshake ─────────────────────────────────────────────────────────────
def test_handshake_captures_server_info():
    c = _spawn()
    try:
        assert c.server_info.get("name") == "mock-mcp"
        assert c.negotiated_protocol  # server echoed a protocol version
        assert "tools" in c.server_capabilities
    finally:
        c.close()


# ── 2. discovery (real schemas, pagination) ──────────────────────────────────
def test_list_tools_discovers_real_schemas():
    c = _spawn()
    try:
        tools = c.list_tools()
        names = {t["name"] for t in tools}
        assert names == {"echo_search", "echo_edit"}
        search = next(t for t in tools if t["name"] == "echo_search")
        # the REAL inputSchema came across, not a client-side mirror.
        assert search["inputSchema"]["required"] == ["query"]
        assert "k" in search["inputSchema"]["properties"]
    finally:
        c.close()


def test_list_tools_handles_pagination():
    c = _spawn({"MOCK_PAGINATE": "1"})
    try:
        tools = c.list_tools()
        assert {t["name"] for t in tools} == {"echo_search", "echo_edit"}
    finally:
        c.close()


# ── 3. tool-call round-trip ──────────────────────────────────────────────────
def test_call_tool_round_trips():
    c = _spawn()
    try:
        out = c.call_tool("echo_search", {"query": "find the bug", "k": 5})
        assert "echo_search called with" in out
        # the arguments round-tripped to the server and came back in the text.
        assert '"query": "find the bug"' in out
        assert '"k": 5' in out
    finally:
        c.close()


def test_call_unknown_tool_reports_error():
    c = _spawn()
    try:
        out = c.call_tool("does_not_exist", {})
        assert out.startswith("[tool error]")
    finally:
        c.close()


# ── framing tolerance: Content-Length replies ────────────────────────────────
def test_content_length_framing_is_tolerated():
    c = _spawn({"MOCK_FRAMING": "content-length"})
    try:
        # handshake already happened over Content-Length frames in spawn().
        assert c.server_info.get("name") == "mock-mcp"
        tools = c.list_tools()
        assert {t["name"] for t in tools} == {"echo_search", "echo_edit"}
        out = c.call_tool("echo_edit", {"path": "a.py", "anchor": "x", "replacement": "y"})
        assert "echo_edit called with" in out
    finally:
        c.close()


# ── noise tolerance: non-JSON stdout banner ──────────────────────────────────
def test_non_json_stdout_noise_is_ignored():
    c = _spawn({"MOCK_LOG_NOISE": "1"})
    try:
        assert c.server_info.get("name") == "mock-mcp"
        assert {t["name"] for t in c.list_tools()} == {"echo_search", "echo_edit"}
    finally:
        c.close()


# ── robustness: a hung server must TIME OUT, not deadlock ────────────────────
def test_hung_call_times_out():
    c = _spawn({"MOCK_HANG_ON_CALL": "1"})
    try:
        raised = False
        try:
            c.call_tool("echo_search", {"query": "x"}, timeout_s=1.0)
        except MCPTimeout:
            raised = True
        assert raised, "a hung tools/call must raise MCPTimeout, not block forever"
    finally:
        c.close()


# ── robustness: a dead server surfaces as MCPError, not a deadlock ───────────
def test_dead_server_surfaces_error():
    c = _spawn()
    # kill the process out from under the client, then issue a call.
    assert c.proc is not None
    c.proc.kill()
    c.proc.wait(timeout=5)
    raised = False
    try:
        c.call_tool("echo_search", {"query": "x"}, timeout_s=5.0)
    except MCPError:
        raised = True
    finally:
        c.close()
    assert raised, "a call against a dead server must raise MCPError"


# ── write framing is decoupled from read framing ────────────────────────────
def test_write_framing_independent_of_reply_framing():
    # The server REPLIES in Content-Length but READS line-delimited (like our
    # mock). Default write framing (line-delimited) must still round-trip — i.e.
    # we must NOT flip our write framing based on the server's reply framing.
    c = _spawn({"MOCK_FRAMING": "content-length"})
    try:
        assert c._content_length_framing is False  # writes stayed line-delimited
        out = c.call_tool("echo_search", {"query": "q"})
        assert "echo_search called with" in out
    finally:
        c.close()


# ── helper: content concatenation ────────────────────────────────────────────
def test_concat_text_content_variants():
    assert _concat_text_content("plain") == "plain"
    assert _concat_text_content([{"type": "text", "text": "a"},
                                 {"type": "text", "text": "b"}]) == "a\nb"
    assert _concat_text_content({"type": "text", "text": "solo"}) == "solo"
    assert _concat_text_content(None) == ""


# ── standalone runner (py tests/test_mcp_client.py) ──────────────────────────
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
