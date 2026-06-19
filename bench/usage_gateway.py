"""A thin logging reverse-proxy for the Anthropic Messages API (pure stdlib).

WHY THIS EXISTS
---------------
The bench drives headless Claude Code (via the Python Claude Agent SDK) as the
ONE fixed agent for every arm. The SDK reports a run-level cost
(``ResultMessage.total_cost_usd``) and a coarse token ``usage`` dict, but it does
NOT expose the PER-REQUEST cache split (cache_creation / cache_read) that our
cache-aware pricing (``bench.pricing``) and the gate2 KPI bundle want. The model
provider DOES report that split on every ``/v1/messages`` response — we just
never see it, because Claude Code talks to the model directly.

So we interpose. We point Claude Code at this gateway
(``ANTHROPIC_BASE_URL=<gateway>``); the gateway forwards each request verbatim to
the arm's UPSTREAM (the arm's compression endpoint, or the real model for A0),
reads the usage off the response it gets back, and appends one
``schema.CallUsage`` row per request to a per-run JSONL. The arm's compression
happens server-side on the far side of the upstream; the gateway is a passive
observer of the wire — it does not transform anything.

WHAT IT CAPTURES (Anthropic usage shape)
----------------------------------------
From each response's ``usage`` object (non-streaming JSON body, OR the final
``message_delta``/``message_start`` of an SSE stream):
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
Every failure mode degrades to passthrough: a parse error, a non-JSON body, an
unexpected content-type, a usage object the upstream didn't send — none of these
break the proxied call. The client always gets the upstream's bytes back
unchanged (status, headers, body); we merely fail to LOG a usage row and move on.
Streaming responses are streamed through chunk-by-chunk so the SDK sees tokens as
they arrive; we tee the SSE bytes to a parser on the side.

CLEAN-ROOM
----------
Pure stdlib (``http.server``, ``urllib``, ``json``, ``threading``). No
``adaptive_context`` import, no vendor SDK, no ``litellm``. ``bench.schema`` is
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
from typing import Optional

# CallUsage is a TypedDict (pure typing) — importing it pulls in no heavy deps.
from bench.schema import CallUsage


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


# ── the proxy request handler ─────────────────────────────────────────────────
def make_handler(upstream_base: str, sink: UsageSink,
                 default_run_id: str = "", default_headers: Optional[dict] = None,
                 timeout_s: float = 600.0):
    """Build a BaseHTTPRequestHandler subclass bound to one upstream + sink.

    upstream_base   : the base URL to forward to (e.g. the arm's compression
                      endpoint, or the real Anthropic/Vertex endpoint for A0).
                      The request path is appended verbatim.
    sink            : where CallUsage rows are written.
    default_run_id  : run-id tag used when a request carries no RUN_ID_HEADER.
    default_headers : extra headers MERGED onto every forwarded request (e.g. an
                      arm's auth header for a hosted upstream). The incoming
                      client headers win on conflict — Claude Code already sends
                      its own auth; default_headers only fills gaps.
    timeout_s       : per-request upstream timeout (a slow upstream surfaces as a
                      504 to the SDK, which the runner treats like any infra fault).
    """
    base = upstream_base.rstrip("/")
    extra_headers = dict(default_headers or {})

    class _Handler(BaseHTTPRequestHandler):
        # silence the default per-request stderr logging (noisy under a pool);
        # real failures are printed explicitly below.
        def log_message(self, *args) -> None:  # noqa: D401
            return

        # ── shared proxy path for all methods ────────────────────────────────
        def _proxy(self) -> None:
            t0 = time.time()
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""

            run_id = self.headers.get(RUN_ID_HEADER) or default_run_id

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

    Usage:
        gw = UsageGateway(upstream_base="https://api.anthropic.com",
                          log_dir="runs/usage")
        gw.start()
        base_url = gw.base_url          # set ANTHROPIC_BASE_URL to this
        ...                             # run the SDK
        gw.stop()

    The server binds to 127.0.0.1 on an ephemeral port (port 0) by default so
    many gateways can run concurrently (one per worker / per (instance, arm))
    without a port-allocation dance. ``base_url`` is the address to hand the SDK.
    """

    def __init__(self, upstream_base: str, log_dir: str, *,
                 host: str = "127.0.0.1", port: int = 0,
                 default_run_id: str = "", default_headers: Optional[dict] = None,
                 timeout_s: float = 600.0) -> None:
        self.upstream_base = upstream_base
        self.sink = UsageSink(log_dir)
        self.default_run_id = default_run_id
        handler = make_handler(upstream_base, self.sink,
                               default_run_id=default_run_id,
                               default_headers=default_headers,
                               timeout_s=timeout_s)
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

    ap = argparse.ArgumentParser(description="Anthropic usage-logging reverse-proxy")
    ap.add_argument("--upstream", default=os.environ.get("CCB_GATEWAY_UPSTREAM",
                                                          "https://api.anthropic.com"),
                    help="upstream base URL to forward to")
    ap.add_argument("--log-dir", default=os.environ.get("CCB_GATEWAY_LOG_DIR", "runs/usage"))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--run-id", default=os.environ.get("CCB_GATEWAY_RUN_ID", "default"))
    a = ap.parse_args()

    gw = UsageGateway(a.upstream, a.log_dir, host=a.host, port=a.port,
                      default_run_id=a.run_id).start()
    print(f"usage_gateway: {gw.base_url} -> {a.upstream}  "
          f"(usage -> {gw.usage_path()})", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        gw.stop()


if __name__ == "__main__":
    main()
