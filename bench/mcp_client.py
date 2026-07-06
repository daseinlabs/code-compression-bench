"""A minimal, dependency-free MCP stdio JSON-RPC client (pure stdlib).

This is the bridge a ToolArm (e.g. woz) uses to drive a real MCP server over a
stdio pipe. It deliberately depends on NOTHING but the standard library — no
`mcp` pip package, no `litellm`, no `minisweagent`. That keeps `import
bench.runner` and `--list-arms` working on a box where the heavy deps (and node)
are not installed, and keeps the bench clean-room (no vendor SDK pulled in).

WHAT MCP STDIO IS
-----------------
The Model Context Protocol over stdio is JSON-RPC 2.0 framed on the child
process's stdin/stdout. Two framings exist in the wild:

  * line-delimited JSON — one compact JSON object per line ('\n'-terminated).
    This is what most Node MCP servers (incl. the Woz `code-server.js --stdio`)
    speak by default.
  * Content-Length framing — LSP-style headers ("Content-Length: N\r\n\r\n"
    followed by N bytes of JSON), used by some implementations.

We DEFAULT to line-delimited and AUTO-DETECT Content-Length on read (if the
first bytes look like a "Content-Length:" header we parse that frame instead).
Writes mirror whatever framing the server used on its last reply, defaulting to
line-delimited until we learn otherwise — so we interoperate with either without
configuration.

HANDSHAKE
---------
Per the MCP spec the client must, in order:
  1. send `initialize` (protocolVersion + clientInfo + capabilities), await result
  2. send the `notifications/initialized` notification (no response)
  3. only then issue `tools/list`, `tools/call`, ...

ROBUSTNESS
----------
A paid eval must never be deadlocked by a server that dies or hangs. Every read
is bounded by a timeout (a background reader thread feeds a queue; the main
thread polls with a deadline). A dead process, a closed pipe, or a slow reply
surfaces as an `MCPError`/`MCPTimeout` the caller can handle (fall back to bash,
mark the run, etc.) rather than blocking forever. `close()` terminates the
child and is safe to call repeatedly / from a `finally`.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from queue import Empty, Queue
from typing import Any, Optional


# Protocol version we advertise. MCP servers negotiate down if they prefer an
# older one; we send a recent dated version and accept whatever the server
# returns in the initialize result.
PROTOCOL_VERSION = "2025-06-18"

# Default per-call timeouts (seconds). Generous enough for a cold index build on
# the server side, bounded enough that a hung server can't stall the eval.
DEFAULT_INIT_TIMEOUT_S = 60.0
DEFAULT_LIST_TIMEOUT_S = 30.0
DEFAULT_CALL_TIMEOUT_S = 120.0


class MCPError(RuntimeError):
    """Any MCP-level failure: spawn failed, server returned a JSON-RPC error,
    the process died, or a malformed frame arrived."""


class MCPTimeout(MCPError):
    """A request did not get a response within its timeout."""


class MCPClient:
    """A stdio JSON-RPC client for one MCP server subprocess.

    Lifecycle: ``spawn(argv, env, cwd)`` -> ``initialize()`` (handshake) ->
    ``list_tools()`` / ``call_tool(...)`` -> ``close()``. ``spawn`` performs the
    handshake for you by default; call the pieces directly only if you need to.
    """

    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen] = None
        self._id = 0
        # WRITE framing for our requests. Newline-delimited JSON is the MCP stdio
        # norm and is what essentially every server READS, so we default to it and
        # do NOT auto-flip — a server's REPLY framing (which we auto-detect on the
        # read side) does not tell us what framing it READS. Flip explicitly via
        # ``write_content_length=True`` on spawn() only if a server demands it.
        self._content_length_framing = False
        # READ framing is detected per-message in the reader thread (tolerant of
        # either line-delimited or Content-Length replies) and is independent of
        # the write framing above.
        # incoming parsed JSON-RPC messages, fed by the reader thread.
        self._inbox: "Queue[dict]" = Queue()
        # raw stderr lines (diagnostics only) — drained by a second reader.
        self._stderr_lines: list[str] = []
        self._reader: Optional[threading.Thread] = None
        self._stderr_reader: Optional[threading.Thread] = None
        self._reader_error: Optional[str] = None
        self._closed = False
        # the server's advertised info/capabilities from initialize (diagnostic).
        self.server_info: dict = {}
        self.server_capabilities: dict = {}
        self.negotiated_protocol: str = ""

    # ── lifecycle ────────────────────────────────────────────────────────────
    def spawn(
        self,
        argv: list[str],
        env: Optional[dict[str, str]] = None,
        cwd: Optional[str] = None,
        *,
        handshake: bool = True,
        init_timeout_s: float = DEFAULT_INIT_TIMEOUT_S,
        write_content_length: bool = False,
    ) -> "MCPClient":
        """Launch the MCP server process and (by default) perform the handshake.

        ``env`` fully REPLACES the child environment when given; pass a copy of
        ``os.environ`` merged with the server-specific vars (the secret API key
        belongs here, never in ``argv``). ``cwd`` is the working dir the server
        indexes against (the repo under test).

        ``write_content_length`` opts our OUTBOUND framing into Content-Length
        headers (default: newline-delimited, the MCP stdio norm). Reads always
        auto-detect either framing regardless of this flag.
        """
        if not argv:
            raise MCPError("empty argv: no MCP server command to spawn")
        self._content_length_framing = bool(write_content_length)
        try:
            self.proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=cwd,
                bufsize=0,  # unbuffered: we frame/flush ourselves
            )
        except (OSError, ValueError) as e:
            raise MCPError(f"failed to spawn MCP server {argv!r}: {e}") from e

        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._stderr_reader.start()

        if handshake:
            self.initialize(timeout_s=init_timeout_s)
        return self

    def initialize(self, timeout_s: float = DEFAULT_INIT_TIMEOUT_S) -> dict:
        """MCP handshake: send ``initialize``, read the result, then send the
        ``notifications/initialized`` notification. Returns the server's
        initialize result dict."""
        result = self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {
                    # We are a bench client: we consume tools. Declare an empty
                    # capability set (no sampling/roots) — servers tolerate this.
                    "tools": {},
                },
                "clientInfo": {"name": "code-compression-bench", "version": "0.1.0"},
            },
            timeout_s=timeout_s,
        )
        if isinstance(result, dict):
            self.server_info = result.get("serverInfo", {}) or {}
            self.server_capabilities = result.get("capabilities", {}) or {}
            self.negotiated_protocol = result.get("protocolVersion", "") or ""
        # Per spec, signal readiness before any further requests.
        self._notify("notifications/initialized", {})
        return result if isinstance(result, dict) else {}

    def list_tools(self, timeout_s: float = DEFAULT_LIST_TIMEOUT_S) -> list[dict]:
        """Call ``tools/list`` and return the REAL tool schemas the server
        advertises: a list of ``{name, description, inputSchema}`` dicts.

        Handles cursor pagination (``nextCursor``) by looping until exhausted.
        """
        tools: list[dict] = []
        cursor: Optional[str] = None
        # bound pagination so a misbehaving server can't loop forever.
        for _ in range(64):
            params = {"cursor": cursor} if cursor else {}
            result = self._request("tools/list", params, timeout_s=timeout_s)
            if not isinstance(result, dict):
                break
            page = result.get("tools") or []
            for t in page:
                if isinstance(t, dict) and t.get("name"):
                    tools.append(t)
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(
        self,
        name: str,
        arguments: Optional[dict] = None,
        timeout_s: float = DEFAULT_CALL_TIMEOUT_S,
    ) -> str:
        """Call ``tools/call`` and return the concatenated text content.

        The MCP result is ``{content: [{type, text|...}, ...], isError?: bool}``.
        We concatenate every ``content[i].text`` (the textual blocks) into one
        string — the same shape an observation message expects. A tool-reported
        error (``isError: true``) still returns its text (so the model sees the
        message) but is prefixed so the agent knows it failed.
        """
        result = self._request(
            "tools/call",
            {"name": name, "arguments": arguments or {}},
            timeout_s=timeout_s,
        )
        if not isinstance(result, dict):
            return str(result)
        text = _concat_text_content(result.get("content"))
        if result.get("isError"):
            return f"[tool error] {text}" if text else "[tool error] (no detail)"
        return text

    def close(self, timeout_s: float = 5.0) -> None:
        """Terminate the server process. Idempotent; safe from a ``finally``.

        Tries a graceful close of stdin, then ``terminate()``, then ``kill()`` —
        never blocks longer than ``timeout_s``.
        """
        if self._closed:
            return
        self._closed = True
        proc = self.proc
        if proc is None:
            return
        # close stdin so a well-behaved server can exit on EOF.
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=timeout_s)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=timeout_s)
            except Exception:
                pass

    # ── context manager sugar ────────────────────────────────────────────────
    def __enter__(self) -> "MCPClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── JSON-RPC plumbing ────────────────────────────────────────────────────
    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _request(self, method: str, params: dict, *, timeout_s: float) -> Any:
        """Send a JSON-RPC request and wait for its matching response.

        Matches by ``id`` (notifications and unrelated server->client messages
        in the inbox are skipped/queued past). Raises ``MCPTimeout`` if no
        matching response arrives in time, ``MCPError`` on a JSON-RPC error or a
        dead server.
        """
        self._ensure_alive()
        req_id = self._next_id()
        self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})

        deadline = time.time() + timeout_s
        # messages that arrived but aren't our response (e.g. server requests,
        # notifications, or out-of-order replies) — we re-queue after matching.
        deferred: list[dict] = []
        try:
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise MCPTimeout(
                        f"timeout after {timeout_s:.0f}s waiting for '{method}' "
                        f"response (id={req_id}){self._stderr_tail()}"
                    )
                try:
                    msg = self._inbox.get(timeout=min(remaining, 0.5))
                except Empty:
                    # poll: did the reader die or the process exit?
                    if self._reader_error:
                        raise MCPError(
                            f"MCP reader failed during '{method}': "
                            f"{self._reader_error}{self._stderr_tail()}"
                        )
                    if self.proc is not None and self.proc.poll() is not None:
                        raise MCPError(
                            f"MCP server exited (code={self.proc.returncode}) "
                            f"during '{method}'{self._stderr_tail()}"
                        )
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("id") == req_id:
                    if "error" in msg and msg["error"] is not None:
                        err = msg["error"]
                        raise MCPError(
                            f"JSON-RPC error for '{method}': "
                            f"{err.get('message', err) if isinstance(err, dict) else err}"
                        )
                    return msg.get("result")
                # not ours: a notification or another request — hold and re-queue.
                deferred.append(msg)
        finally:
            for m in deferred:
                self._inbox.put(m)

    def _notify(self, method: str, params: dict) -> None:
        """Fire-and-forget JSON-RPC notification (no id, no response expected)."""
        self._ensure_alive()
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _send(self, obj: dict) -> None:
        """Frame and write one JSON-RPC message to the server's stdin."""
        proc = self.proc
        if proc is None or proc.stdin is None:
            raise MCPError("MCP server stdin is not open")
        payload = json.dumps(obj, separators=(",", ":"))
        data = payload.encode("utf-8")
        if self._content_length_framing:
            frame = f"Content-Length: {len(data)}\r\n\r\n".encode("ascii") + data
        else:
            frame = data + b"\n"
        try:
            proc.stdin.write(frame)
            proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as e:
            raise MCPError(f"failed to write to MCP server stdin: {e}") from e

    def _ensure_alive(self) -> None:
        if self.proc is None:
            raise MCPError("MCP server not spawned")
        if self.proc.poll() is not None:
            raise MCPError(
                f"MCP server is not running (exit code={self.proc.returncode})"
                f"{self._stderr_tail()}"
            )

    # ── reader threads ───────────────────────────────────────────────────────
    def _read_loop(self) -> None:
        """Background thread: parse framed JSON-RPC messages off stdout into the
        inbox. Auto-detects Content-Length vs line-delimited framing PER MESSAGE
        on the READ side only — it does NOT change our write framing (a server's
        reply framing tells us nothing about what framing it reads)."""
        proc = self.proc
        assert proc is not None and proc.stdout is not None
        stream = proc.stdout
        try:
            while True:
                # peek the first byte(s) of a frame to decide framing.
                line = stream.readline()
                if line == b"":
                    break  # EOF: server closed stdout
                # Content-Length framing? (LSP-style header line)
                stripped = line.strip()
                if stripped[:15].lower() == b"content-length:":
                    n = int(stripped.split(b":", 1)[1].strip())
                    # consume header block until the blank separator line.
                    while True:
                        hdr = stream.readline()
                        if hdr in (b"\r\n", b"\n", b""):
                            break
                    body = self._read_exact(stream, n)
                    if body is None:
                        break
                    self._dispatch_raw(body)
                    continue
                if not stripped:
                    continue  # blank line between line-delimited messages — skip
                # line-delimited JSON.
                self._dispatch_raw(stripped)
        except Exception as e:  # noqa: BLE001 — record, let _request surface it
            self._reader_error = f"{type(e).__name__}: {e}"
        finally:
            # sentinel so a blocked _request wakes promptly on EOF.
            self._inbox.put({"_eof": True})

    @staticmethod
    def _read_exact(stream, n: int) -> Optional[bytes]:
        """Read exactly n bytes (Content-Length body); None on premature EOF."""
        chunks: list[bytes] = []
        got = 0
        while got < n:
            chunk = stream.read(n - got)
            if not chunk:
                return None
            chunks.append(chunk)
            got += len(chunk)
        return b"".join(chunks)

    def _dispatch_raw(self, raw: bytes) -> None:
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            # Non-JSON noise on stdout (some servers log there). Ignore — the
            # JSON-RPC frames we care about parse fine.
            return
        if isinstance(obj, dict):
            self._inbox.put(obj)

    def _read_stderr(self) -> None:
        """Drain stderr into a bounded buffer for diagnostics (never blocks the
        protocol; node servers log here)."""
        proc = self.proc
        if proc is None or proc.stderr is None:
            return
        try:
            for line in iter(proc.stderr.readline, b""):
                try:
                    s = line.decode("utf-8", "replace").rstrip()
                except Exception:
                    continue
                if s:
                    self._stderr_lines.append(s)
                    if len(self._stderr_lines) > 200:
                        del self._stderr_lines[:100]
        except Exception:
            pass

    def _stderr_tail(self, n: int = 5) -> str:
        if not self._stderr_lines:
            return ""
        tail = "\n  ".join(self._stderr_lines[-n:])
        return f"\n  server stderr (last {min(n, len(self._stderr_lines))} lines):\n  {tail}"


# ── helpers ──────────────────────────────────────────────────────────────────
def _concat_text_content(content: Any) -> str:
    """Concatenate the textual blocks of an MCP tool result's ``content`` array.

    MCP content blocks are ``{"type": "text", "text": "..."}`` (also image/
    resource blocks we summarize rather than inline). Robust to a bare string or
    a single dict instead of a list.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        content = [content]
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text" or "text" in block:
            parts.append(str(block.get("text", "")))
        elif btype == "resource":
            res = block.get("resource", {})
            if isinstance(res, dict) and res.get("text"):
                parts.append(str(res["text"]))
            else:
                parts.append(f"[resource: {res.get('uri', '?') if isinstance(res, dict) else res}]")
        elif btype == "image":
            parts.append(f"[image: {block.get('mimeType', 'image')}]")
        else:
            # unknown block: include its JSON so nothing is silently dropped.
            parts.append(json.dumps(block, separators=(",", ":")))
    return "\n".join(p for p in parts if p)


def merged_env(extra: Optional[dict[str, str]] = None) -> dict[str, str]:
    """A child env = a copy of the current process env merged with ``extra``.

    Convenience for callers spawning a server: secrets (API keys) and server
    config go in ``extra`` and reach the child via the ENVIRONMENT, never argv.
    """
    env = dict(os.environ)
    if extra:
        env.update({k: str(v) for k, v in extra.items() if v is not None})
    return env
