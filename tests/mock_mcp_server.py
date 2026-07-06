"""A TINY mock stdio MCP server for unit-testing bench.mcp_client.

Speaks the same newline-delimited JSON-RPC 2.0 over stdin/stdout that a real MCP
stdio server (e.g. Woz's ``code-server.js --stdio``) speaks:

  * responds to ``initialize`` with a serverInfo + capabilities + protocolVersion
  * accepts the ``notifications/initialized`` notification (no reply)
  * responds to ``tools/list`` with two REAL-shaped tools (name/description/
    inputSchema), optionally paginated via ``nextCursor``
  * responds to ``tools/call`` by echoing the arguments back as text content,
    or returning ``isError`` for an unknown tool

Env knobs (so one script exercises several client behaviors):
  MOCK_FRAMING=content-length   -> reply using Content-Length headers instead of
                                   newline-delimited JSON (tests framing tolerance)
  MOCK_PAGINATE=1               -> split tools/list across two pages (nextCursor)
  MOCK_HANG_ON_CALL=1           -> never reply to tools/call (tests the timeout)
  MOCK_LOG_NOISE=1              -> print a non-JSON banner to stdout first (tests
                                   that the client ignores non-JSON stdout noise)

Pure stdlib; no third-party deps. Run as a subprocess by the test.
"""

import json
import os
import sys
import time


def _write(obj, framing):
    data = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    out = sys.stdout.buffer
    if framing == "content-length":
        out.write(f"Content-Length: {len(data)}\r\n\r\n".encode("ascii"))
        out.write(data)
    else:
        out.write(data + b"\n")
    out.flush()


TOOLS = [
    {
        "name": "echo_search",
        "description": "Echo a semantic search query back (mock).",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "k": {"type": "integer"}},
            "required": ["query"],
        },
    },
    {
        "name": "echo_edit",
        "description": "Echo a structured edit back (mock).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "anchor": {"type": "string"},
                "replacement": {"type": "string"},
            },
            "required": ["path", "anchor", "replacement"],
        },
    },
]


def main():
    framing = "content-length" if os.environ.get("MOCK_FRAMING") == "content-length" else "line"
    paginate = os.environ.get("MOCK_PAGINATE") == "1"
    hang_on_call = os.environ.get("MOCK_HANG_ON_CALL") == "1"

    if os.environ.get("MOCK_LOG_NOISE") == "1":
        # Some servers log a human banner to stdout before the protocol; the
        # client must ignore non-JSON lines.
        sys.stdout.buffer.write(b"mock-mcp-server starting (this is not JSON)\n")
        sys.stdout.buffer.flush()

    inbuf = sys.stdin.buffer
    while True:
        line = inbuf.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        # The client always sends line-delimited; we read that. (We only switch
        # framing on the REPLY side, to test the client's read tolerance.)
        try:
            msg = json.loads(line.decode("utf-8"))
        except Exception:
            continue

        method = msg.get("method")
        mid = msg.get("id")

        if method == "initialize":
            _write({
                "jsonrpc": "2.0",
                "id": mid,
                "result": {
                    "protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-06-18"),
                    "serverInfo": {"name": "mock-mcp", "version": "0.0.1"},
                    "capabilities": {"tools": {"listChanged": False}},
                },
            }, framing)
        elif method == "notifications/initialized":
            pass  # notification: no reply
        elif method == "tools/list":
            cursor = (msg.get("params") or {}).get("cursor")
            if paginate:
                if not cursor:
                    _write({"jsonrpc": "2.0", "id": mid,
                            "result": {"tools": TOOLS[:1], "nextCursor": "page2"}}, framing)
                else:
                    _write({"jsonrpc": "2.0", "id": mid,
                            "result": {"tools": TOOLS[1:]}}, framing)
            else:
                _write({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}, framing)
        elif method == "tools/call":
            if hang_on_call:
                # Never reply — the client must time out and not deadlock.
                time.sleep(3600)
                continue
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            known = {t["name"] for t in TOOLS}
            if name not in known:
                _write({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": f"unknown tool {name}"}],
                    "isError": True,
                }}, framing)
            else:
                _write({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [
                        {"type": "text", "text": f"{name} called with "
                                                 f"{json.dumps(args, sort_keys=True)}"},
                    ],
                }}, framing)
        else:
            # Unknown method: JSON-RPC error (only for requests, not notifications).
            if mid is not None:
                _write({"jsonrpc": "2.0", "id": mid,
                        "error": {"code": -32601, "message": f"method not found: {method}"}},
                       framing)


if __name__ == "__main__":
    main()
