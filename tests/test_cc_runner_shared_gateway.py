"""cc_runner honors a SHARED standalone gateway (CCB_GATEWAY_URL).

When ``CCB_GATEWAY_URL`` is set, ``run_agent`` must:
  * NOT start a per-(instance,arm) gateway (no ephemeral server bound);
  * point a gateway-direct arm (baseline) at the shared gateway URL via
    ANTHROPIC_BASE_URL (so the env Claude Code inherits carries it);
  * read each run's usage from ``<CCB_GATEWAY_USAGE_DIR>/<run_id>.usage.jsonl``
    (the file the shared gateway writes, keyed by the x-ccb-run-id header).

And the back-compat guarantee: with ``CCB_GATEWAY_URL`` UNSET, ``_resolve_gateway``
returns a freshly-started per-run ``UsageGateway`` (the exact current behaviour).

The Claude Agent SDK is NOT installed here, so ``_run_sdk`` (which imports it
lazily) is monkeypatched with a fake that (a) records the ANTHROPIC_BASE_URL it
would hand Claude Code and (b) writes a usage row into the shared usage dir
exactly where the shared gateway would, then returns a minimal result. We assert
run_agent wires the URL and reads that row back.

Runnable two ways:
    py -m pytest tests/test_cc_runner_shared_gateway.py -q
    py tests/test_cc_runner_shared_gateway.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

# make the repo root importable when run as a bare script (py tests/...).
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import bench.cc_runner as cc_runner  # noqa: E402
from bench.arm import get_arm  # noqa: E402
from bench.usage_gateway import UsageGateway, UsageSink  # noqa: E402


# one normalized CallUsage row the (faked) gateway "wrote" for the run.
_ROW = {
    "prompt_tokens": 5920, "completion_tokens": 45,
    "cache_creation_input_tokens": 800, "cache_read_input_tokens": 5000,
    "latency_s": 0.5,
}


# ── shared-gateway path: ANTHROPIC_BASE_URL wired + usage read from the dir ────
def test_run_agent_uses_shared_gateway_for_baseline(monkeypatch):
    shared_url = "http://127.0.0.1:8088"
    usage_dir = tempfile.mkdtemp(prefix="ccb_shared_usage_")
    out_dir = tempfile.mkdtemp(prefix="ccb_out_")
    run_id = "iid-1__rid__baseline"

    # configure the shared gateway via the module constants run_agent reads.
    monkeypatch.setattr(cc_runner, "SHARED_GATEWAY_URL", shared_url)
    monkeypatch.setattr(cc_runner, "SHARED_GATEWAY_USAGE_DIR", usage_dir)

    captured: dict = {}

    started = {"count": 0}
    _orig_start = UsageGateway.start

    def _spy_start(self):  # if run_agent starts a per-run gateway, this fires
        started["count"] += 1
        return _orig_start(self)

    monkeypatch.setattr(UsageGateway, "start", _spy_start)

    # The SDK isn't installed, so stand in for the whole SDK-run leg
    # (_run_with_wall_cap -> _run_sdk). We get the RESOLVED client_base_url here —
    # i.e. exactly the ANTHROPIC_BASE_URL run_agent would hand Claude Code — and
    # emulate the shared gateway by writing the run's usage row into the shared dir.
    async def _wall(*, wall_cap_s, client_base_url, run_id, **kw):  # noqa: ANN001
        # (a) the base url run_agent resolved for this arm (== ANTHROPIC_BASE_URL).
        captured["client_base_url"] = client_base_url
        # (b) the SHARED gateway writes usage keyed by the run-id header into
        # <usage_dir>/<run_id>.usage.jsonl — emulate that write here.
        path = UsageSink(usage_dir).path_for(run_id)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(_ROW) + "\n")
        return {"reported_cost_usd": 0.0, "sdk_usage": {}, "num_turns": 1,
                "steps": 1, "tool_calls": 0, "is_error": False, "subtype": "success",
                "session_id": "s", "messages": []}

    monkeypatch.setattr(cc_runner, "_run_with_wall_cap", _wall)

    raw = cc_runner.run_agent(
        get_arm("baseline"), "iid-1",
        model="claude-sonnet-4-5", dataset="x", split="test",
        out_dir=out_dir, run_id=run_id, call_cap=3, wall_cap_s=30,
    )

    # NO per-run gateway was started (the shared one is used instead).
    assert started["count"] == 0, "run_agent must NOT start a per-run gateway when " \
                                  "CCB_GATEWAY_URL is set"
    # baseline points Claude Code at the shared gateway URL (ANTHROPIC_BASE_URL).
    assert captured["client_base_url"] == shared_url
    # usage was read from <CCB_GATEWAY_USAGE_DIR>/<run_id>.usage.jsonl.
    assert raw["usage"] == [_ROW]
    assert raw["input_tokens"] == _ROW["prompt_tokens"]
    assert raw["output_tokens"] == _ROW["completion_tokens"]


# ── back-compat: no CCB_GATEWAY_URL -> a per-run gateway is started ────────────
def test_resolve_gateway_defaults_to_per_run_when_unset(monkeypatch):
    out_dir = tempfile.mkdtemp(prefix="ccb_out_default_")
    monkeypatch.setattr(cc_runner, "SHARED_GATEWAY_URL", "")
    monkeypatch.setattr(cc_runner, "SHARED_GATEWAY_USAGE_DIR", "")

    base_url, usage_path, stop_fn = cc_runner._resolve_gateway(out_dir, "rid", 30)
    try:
        # a real ephemeral per-run gateway on 127.0.0.1
        assert base_url.startswith("http://127.0.0.1:")
        # its usage path is under <out_dir>/usage and keyed by the run id
        assert usage_path.name == "rid.usage.jsonl"
        assert str(Path(out_dir) / "usage") in str(usage_path)
    finally:
        stop_fn()


def test_resolve_gateway_shared_returns_noop_stop_and_dir_path(monkeypatch):
    shared_url = "http://10.0.0.5:9000/"
    usage_dir = tempfile.mkdtemp(prefix="ccb_shared_usage2_")
    out_dir = tempfile.mkdtemp(prefix="ccb_out2_")
    monkeypatch.setattr(cc_runner, "SHARED_GATEWAY_URL", shared_url)
    monkeypatch.setattr(cc_runner, "SHARED_GATEWAY_USAGE_DIR", usage_dir)

    base_url, usage_path, stop_fn = cc_runner._resolve_gateway(out_dir, "rid-2", 30)
    # trailing slash stripped; usage path is under the SHARED dir, run-id-keyed.
    assert base_url == "http://10.0.0.5:9000"
    assert usage_path == UsageSink(usage_dir).path_for("rid-2")
    assert str(Path(usage_dir)) in str(usage_path)
    # stop is a no-op (this process does NOT own the shared gateway).
    assert stop_fn() is None


# ── standalone runner ─────────────────────────────────────────────────────────
class _MonkeyPatch:
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
            if "monkeypatch" in inspect.signature(fn).parameters:
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
