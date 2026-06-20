"""Unit tests for bench.gateway_server — the STANDALONE shared usage gateway.

The standalone gateway is ONE long-lived ``UsageGateway`` bound to a FIXED port so
a vendor proxy (provisioned once) can forward EVERY run's traffic to a stable
address. MANY concurrent workers share it; each run's usage is isolated by the
``x-ccb-run-id`` header into its own ``<run_id>.usage.jsonl``.

These assert the two properties that make that safe:

  1. CONCURRENCY + RUN-ID ISOLATION — many simultaneous requests tagged with
     DIFFERENT run-ids land in SEPARATE JSONL files, each with exactly its own
     rows. The Vertex completion_fn is MOCKED (an iterable of native event dicts),
     so the suite runs on a box WITHOUT the ``anthropic`` SDK.
  2. THREADING SERVER — the gateway the entrypoint runs is a
     ``http.server.ThreadingHTTPServer`` (one handler thread per request), the
     prerequisite for serving 8 simultaneous workers without serializing them.

Plus CLI wiring: ``--port`` / ``--usage-dir`` map onto the gateway, and the bound
URL is printed on startup.

Runnable two ways:
    py -m pytest tests/test_gateway_server.py -q
    py tests/test_gateway_server.py            # standalone, prints PASS/FAIL
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

# make the repo root importable when run as a bare script (py tests/...).
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import bench.gateway_server as gateway_server  # noqa: E402
from bench.usage_gateway import RUN_ID_HEADER, UsageGateway  # noqa: E402


# ── a mocked AnthropicVertex stream (native event dicts; no anthropic SDK) ────
_START_USAGE = {
    "input_tokens": 120, "output_tokens": 1,
    "cache_creation_input_tokens": 800, "cache_read_input_tokens": 5000,
}
_DELTA_USAGE = {"output_tokens": 45}
# the normalized CallUsage every run logs: prompt == 120+800+5000 == 5920.
_EXPECTED = {
    "prompt_tokens": 5920, "completion_tokens": 45,
    "cache_creation_input_tokens": 800, "cache_read_input_tokens": 5000,
}


def _fake_event_stream(model="claude-sonnet-4-6"):
    """Native Anthropic stream events (plain dicts) — a one-line text reply."""
    return [
        {"type": "message_start", "message": {
            "id": "msg_x", "type": "message", "role": "assistant", "model": model,
            "usage": dict(_START_USAGE)}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": "done"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta",
         "delta": {"stop_reason": "end_turn", "stop_sequence": None},
         "usage": dict(_DELTA_USAGE)},
        {"type": "message_stop"},
    ]


def _start_shared_gateway(usage_dir: str) -> UsageGateway:
    """Stand up a Vertex-mode UsageGateway with a MOCKED completion_fn — the exact
    server bench.gateway_server.main runs (no default_run_id; tags by header only).
    Bound to an ephemeral port for the test; the concurrency/isolation guarantees
    are identical to a fixed port."""
    def fake_completion(create_kwargs):
        return _fake_event_stream()

    return UsageGateway(
        log_dir=usage_dir,
        default_run_id="",            # tag strictly by the per-request run-id header
        completion_fn=fake_completion,  # mode defaults to vertex
    ).start()


def _post(base_url: str, run_id: str) -> bytes:
    req = urllib.request.Request(
        base_url + "/v1/messages",
        data=json.dumps({"model": "claude-sonnet-4-5", "max_tokens": 64,
                         "messages": [{"role": "user", "content": "hi"}]}).encode(),
        method="POST",
        headers={"Content-Type": "application/json", RUN_ID_HEADER: run_id},
    )
    return urllib.request.urlopen(req, timeout=15).read()


# ── 1. concurrency + run-id isolation ─────────────────────────────────────────
def test_concurrent_runs_isolated_by_run_id():
    """8 concurrent requests across 4 distinct run-ids (2 each) must produce ONE
    JSONL file per run-id, each holding exactly its own 2 rows — no cross-talk."""
    tmp = tempfile.mkdtemp(prefix="ccb_shared_gw_")
    gw = _start_shared_gateway(tmp)
    try:
        run_ids = [f"iid-{i}__rid__baseline" for i in range(4)]
        # 8 workers: each run-id fired twice, all in flight at once.
        jobs = run_ids + run_ids
        errors: list[Exception] = []

        def _fire(rid: str):
            try:
                body = _post(gw.base_url, rid)
                # the relayed body is a reconstructed Anthropic JSON message
                assert b'"done"' in body or b"message" in body
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=_fire, args=(rid,)) for rid in jobs]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert not errors, f"request errors: {errors}"

        usage_dir = Path(tmp)
        # exactly one JSONL per distinct run-id, no extras (e.g. no 'default').
        files = sorted(p.name for p in usage_dir.glob("*.usage.jsonl"))
        expected_files = sorted(f"{rid}.usage.jsonl" for rid in run_ids)
        assert files == expected_files, f"files: {files} != {expected_files}"

        # each run-id's file holds exactly its OWN 2 rows (2 requests each), and the
        # rows are the right normalized CallUsage — proof the run-id tagging isolates.
        for rid in run_ids:
            path = gw.usage_path(rid)
            rows = [json.loads(ln) for ln in
                    path.read_text(encoding="utf-8").splitlines() if ln.strip()]
            assert len(rows) == 2, f"{rid}: expected 2 rows, got {len(rows)}"
            for row in rows:
                for k, v in _EXPECTED.items():
                    assert row.get(k) == v, f"{rid}: {k}={row.get(k)} != {v}"
    finally:
        gw.stop()


# ── 2. the entrypoint's server is a ThreadingHTTPServer ───────────────────────
def test_shared_gateway_uses_threading_http_server():
    """The server backing the standalone gateway MUST be a ThreadingHTTPServer so 8
    workers are served concurrently (one thread per request), not serialized."""
    tmp = tempfile.mkdtemp(prefix="ccb_shared_gw_t_")
    gw = _start_shared_gateway(tmp)
    try:
        assert isinstance(gw._server, ThreadingHTTPServer)
    finally:
        gw.stop()


# ── 3. CLI: --port / --usage-dir wire onto the gateway ────────────────────────
def test_build_parser_port_and_usage_dir():
    """The standalone CLI exposes --port and --usage-dir (the spec's two knobs),
    defaulting to vertex mode."""
    a = gateway_server.build_parser().parse_args(
        ["--port", "8123", "--usage-dir", "/tmp/ccb_usage"])
    assert a.port == 8123
    assert a.usage_dir == "/tmp/ccb_usage"
    assert a.mode == "vertex"


def test_main_binds_port_prints_url_and_serves(monkeypatch):
    """main() binds the requested port, prints the bound CCB_GATEWAY_URL on startup,
    and stops cleanly. The blocking serve loop is short-circuited by patching
    time.sleep to raise KeyboardInterrupt (the same signal a Ctrl-C delivers), and
    the Vertex completion builder is patched to the mock so no anthropic SDK / ADC /
    Vertex round-trip is needed."""
    tmp = tempfile.mkdtemp(prefix="ccb_shared_gw_main_")
    # Patch the gateway's default Vertex completion builder so main()'s gateway uses
    # the mocked native stream (no anthropic import, no ADC).
    import bench.usage_gateway as ug

    def _fake_make_vertex_completion(project, location):
        def _complete(create_kwargs):
            return _fake_event_stream()
        return _complete

    monkeypatch.setattr(ug, "_make_vertex_completion", _fake_make_vertex_completion)

    # main() blocks in a sleep loop; make the first sleep raise KeyboardInterrupt so
    # main builds + starts the gateway, prints, then exits cleanly via its finally.
    def _sleep(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(gateway_server.time, "sleep", _sleep)

    import io
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        rc = gateway_server.main(["--port", "0", "--usage-dir", tmp])
    finally:
        sys.stdout = old
    out = buf.getvalue()
    assert rc == 0
    assert "CCB_GATEWAY_URL=http://127.0.0.1:" in out
    assert "ThreadingHTTPServer" in out
    assert "run-id-isolated" in out


# ── standalone runner (py tests/test_gateway_server.py) ───────────────────────
class _MonkeyPatch:
    """A tiny monkeypatch shim so the standalone runner can call the monkeypatch
    tests without pytest. Restores all setattr changes on undo()."""

    def __init__(self):
        self._undo = []

    def setattr(self, target, name, value):
        old = getattr(target, name)
        self._undo.append((target, name, old))
        setattr(target, name, value)

    def undo(self):
        for target, name, old in reversed(self._undo):
            setattr(target, name, old)


def _run_standalone() -> int:
    import inspect
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failures = 0
    for name, fn in tests:
        mp = _MonkeyPatch()
        try:
            params = inspect.signature(fn).parameters
            if "monkeypatch" in params:
                fn(mp)
            else:
                fn()
            print(f"PASS  {name}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL  {name}: {type(e).__name__}: {e}")
        finally:
            mp.undo()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
