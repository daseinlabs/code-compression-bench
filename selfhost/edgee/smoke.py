#!/usr/bin/env python3
"""Smoke test the FORKED Edgee local gateway end to end (no Vertex, no spend).

Proves the one thing the fork exists to prove: a REAL `edgee local-gateway` ingests
Claude Code's Anthropic-native `POST /v1/messages`, COMPRESSES the tool-output noise,
and FORWARDS the request to the upstream named by `EDGEE_ANTHROPIC_UPSTREAM` — i.e. our
run gateway — instead of `api.anthropic.com`.

Topology under test (all loopback, no network egress):

    this script (acts as Claude Code) --POST /v1/messages-->
        edgee local-gateway (compresses)  --EDGEE_ANTHROPIC_UPSTREAM-->
            mock gateway in this script (captures what Edgee forwarded)

Steps:
  1. Start a mock upstream HTTP server that records the forwarded /v1/messages body
     and returns a minimal valid Anthropic Messages response.
  2. Launch `edgee local-gateway --port <P>` with EDGEE_ANTHROPIC_UPSTREAM=<mock>.
  3. POST a realistic Claude-Code-shaped request: a user turn + an assistant tool_use +
     a tool_result block carrying a big, compressible `git status`/`ls -R` dump, with
     cache_control on the system block.
  4. Assert: (a) the mock received the request (upstream override works — it did NOT go
     to Anthropic); (b) content blocks / tool_use / tool_result / cache_control survived
     the passthrough; (c) the forwarded tool_result is SMALLER than what we sent (the
     CompressionLayer acted) OR is byte-identical only if Edgee chose not to compress it
     (reported, not failed — compression is content-dependent).

Exit 0 on PASS, 1 on FAIL. Pure stdlib.

Usage:
    EDGEE_BIN=~/.local/bin/edgee python3 selfhost/edgee/smoke.py
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

EDGEE_BIN = os.environ.get("EDGEE_BIN", "edgee")

# A large, repetitive, compressible tool-output blob (the kind edgee targets):
# a flat `find . -type f` listing — many files across a few directories. Edgee's
# FindCompressor groups paths by directory + adds an extension summary, which
# collapses this dramatically (a decisive, unambiguous compression demonstration).
_DIRS = ["./src/core", "./src/api", "./src/db", "./tests/unit", "./tests/integration"]
TOOL_CMD = "find . -type f -name '*.py'"
TOOL_OUTPUT = "\n".join(
    f"{_DIRS[i % len(_DIRS)]}/module_{i:04d}.py" for i in range(600)
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Captured:
    body: dict | None = None
    headers: dict | None = None
    path: str | None = None


def _make_handler(cap: _Captured):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence
            pass

        def _read_body(self) -> bytes:
            # Handle both Content-Length and chunked transfer-encoding: Edgee
            # forwards via reqwest, which streams the body chunked (no
            # Content-Length), so a naive content-length read returns 0 bytes.
            te = (self.headers.get("transfer-encoding") or "").lower()
            if "chunked" in te:
                chunks = []
                while True:
                    size_line = self.rfile.readline().strip()
                    if not size_line:
                        continue
                    try:
                        size = int(size_line.split(b";")[0], 16)
                    except ValueError:
                        break
                    if size == 0:
                        self.rfile.readline()  # trailing CRLF after last chunk
                        break
                    chunks.append(self.rfile.read(size))
                    self.rfile.read(2)  # CRLF after each chunk
                return b"".join(chunks)
            n = int(self.headers.get("content-length", 0))
            return self.rfile.read(n) if n else b""

        def do_POST(self):
            raw = self._read_body()
            cap.path = self.path
            cap.headers = {k.lower(): v for k, v in self.headers.items()}
            try:
                cap.body = json.loads(raw)
            except Exception:
                cap.body = {"_raw_len": len(raw)}
            resp = json.dumps({
                "id": "msg_smoke", "type": "message", "role": "assistant",
                "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 2},
            }).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

    return H


def _wait_port(host: str, port: int, timeout: float = 15.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def main() -> int:
    cap = _Captured()
    mock_port = _free_port()
    edgee_port = _free_port()

    httpd = ThreadingHTTPServer(("127.0.0.1", mock_port), _make_handler(cap))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    mock_url = f"http://127.0.0.1:{mock_port}"

    env = dict(os.environ)
    env["EDGEE_ANTHROPIC_UPSTREAM"] = mock_url
    env["EDGEE_GATEWAY_LOG"] = "info,edgee_gateway_http=info"

    proc = subprocess.Popen(
        [EDGEE_BIN, "local-gateway", "--port", str(edgee_port), "--bind", "127.0.0.1"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    failures: list[str] = []
    try:
        if not _wait_port("127.0.0.1", edgee_port, timeout=15):
            print("FAIL: edgee local-gateway never started listening")
            out = (proc.stdout.read() if proc.stdout else "") or ""
            print(out[:2000])
            return 1
        print(f"edgee local-gateway up on :{edgee_port}, upstream={mock_url}")

        # A realistic Claude-Code-shaped /v1/messages request.
        req_body = {
            "model": "claude-sonnet-4-6",
            "max_tokens": 64,
            "system": [
                {"type": "text", "text": "You are a coding agent.",
                 "cache_control": {"type": "ephemeral"}},
            ],
            "messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": "What changed in the repo?"}]},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "toolu_1", "name": "Bash",
                     "input": {"command": TOOL_CMD}}]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1",
                     "content": TOOL_OUTPUT}]},
            ],
        }
        sent_blob_len = len(TOOL_OUTPUT)

        data = json.dumps(req_body).encode()
        r = urllib.request.Request(
            f"http://127.0.0.1:{edgee_port}/v1/messages", data=data,
            headers={"content-type": "application/json",
                     "x-api-key": "sk-ant-smoke",
                     "anthropic-version": "2023-06-01"},
            method="POST",
        )
        with urllib.request.urlopen(r, timeout=20) as resp:
            client_resp = json.loads(resp.read())

        # (a) the request reached the MOCK gateway (upstream override worked).
        if cap.body is None:
            failures.append("upstream mock received NOTHING — Edgee did not forward "
                            "to EDGEE_ANTHROPIC_UPSTREAM")
        else:
            print(f"PASS: upstream mock received POST {cap.path} "
                  f"(Edgee forwarded to the run gateway, NOT api.anthropic.com)")

        # client got a valid Anthropic-shaped response back through Edgee.
        if client_resp.get("type") != "message":
            failures.append(f"client response not an Anthropic message: {client_resp}")
        else:
            print("PASS: client received a valid Anthropic Messages response via Edgee")

        if cap.body is not None:
            fwd = cap.body
            if os.environ.get("EDGEE_SMOKE_DEBUG"):
                print("--- forwarded body (debug) ---")
                print(json.dumps(fwd, indent=2)[:3000])
            # (b) Anthropic-native structure survived the passthrough.
            try:
                sys_blocks = fwd.get("system")
                has_cache = (isinstance(sys_blocks, list) and sys_blocks
                             and sys_blocks[0].get("cache_control", {}).get("type") == "ephemeral")
                msgs = fwd.get("messages", [])
                has_tool_use = any(
                    blk.get("type") == "tool_use"
                    for m in msgs for blk in (m.get("content") or [])
                    if isinstance(blk, dict))
                tr_blocks = [
                    blk for m in msgs for blk in (m.get("content") or [])
                    if isinstance(blk, dict) and blk.get("type") == "tool_result"]
                has_tool_result = bool(tr_blocks)
            except Exception as e:  # noqa: BLE001
                failures.append(f"forwarded body not parseable as Anthropic shape: {e}")
                has_cache = has_tool_use = has_tool_result = False

            if has_cache:
                print("PASS: cache_control on the system block passed through unchanged")
            else:
                failures.append("cache_control did NOT survive the passthrough")
            if has_tool_use:
                print("PASS: assistant tool_use block passed through")
            else:
                failures.append("tool_use block did NOT survive the passthrough")
            if has_tool_result:
                print("PASS: tool_result block passed through")
            else:
                failures.append("tool_result block did NOT survive the passthrough")

            # (c) did the CompressionLayer act on the tool output?
            def _tr_len(blocks):
                tot = 0
                for blk in blocks:
                    c = blk.get("content")
                    tot += len(c) if isinstance(c, str) else len(json.dumps(c))
                return tot
            fwd_blob_len = _tr_len(tr_blocks) if has_tool_result else -1
            if fwd_blob_len >= 0:
                if fwd_blob_len < sent_blob_len:
                    pct = 100 * (1 - fwd_blob_len / sent_blob_len)
                    print(f"PASS: Edgee COMPRESSED the tool_result "
                          f"({sent_blob_len} -> {fwd_blob_len} chars, -{pct:.0f}%)")
                else:
                    # A `find` listing of 600 files MUST compress (FindCompressor
                    # groups by directory + extension summary). No reduction means the
                    # CompressionLayer never ran on the forwarded body — a real failure.
                    failures.append(
                        f"tool_result NOT compressed ({sent_blob_len} -> {fwd_blob_len} "
                        f"chars): the CompressionLayer did not act on the forwarded body")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        httpd.shutdown()
        # surface the gateway log tail for diagnosis.
        if proc.stdout:
            tail = proc.stdout.read() or ""
            if tail.strip():
                print("--- edgee gateway log (tail) ---")
                print("\n".join(tail.splitlines()[-15:]))

    if failures:
        print("\nSMOKE FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nSMOKE PASS: forked Edgee compresses Claude Code /v1/messages and forwards "
          "to EDGEE_ANTHROPIC_UPSTREAM (the run gateway).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
