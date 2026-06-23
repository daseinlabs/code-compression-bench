"""A STANDALONE, long-lived usage gateway — ONE shared bottom bridge to Vertex.

WHY THIS EXISTS (vs the per-(instance,arm) gateway in cc_runner)
---------------------------------------------------------------
``bench.cc_runner.run_agent`` starts a FRESH ``UsageGateway`` per solve, bound to
``127.0.0.1:0`` — a RANDOM ephemeral port. That is fine for the baseline/woz arms
(Claude Code is told the gateway URL at run time) but it is unusable as the
upstream for a VENDOR PROXY arm (dasein/edgee/headroom/compresr): a vendor proxy
is provisioned ONCE with a FIXED upstream address, and it cannot be reconfigured
to chase a new random port on every task.

So this module runs ONE ``UsageGateway`` in Vertex mode bound to a FIXED port,
serving until killed. MANY concurrent workers (the runner's 8-wide pool) and many
runs share this single gateway; each run's usage is isolated by the
``x-ccb-run-id`` header (``RUN_ID_HEADER``) into its own ``<run_id>.usage.jsonl``
under ``--usage-dir``. A vendor proxy can be pointed at this stable address once
and forward EVERY run's traffic through it; the gateway still observes the REAL
post-compression usage the model billed, tagged per run.

CONCURRENCY
-----------
The HTTP server MUST be safe under 8 simultaneous workers. ``UsageGateway`` already
uses ``http.server.ThreadingHTTPServer`` (one handler thread per request) and the
usage sink serializes its appends under a lock, so concurrent runs writing to
DIFFERENT ``<run_id>.usage.jsonl`` files never interfere. This entrypoint changes
nothing about that — it only pins the port and keeps the server alive.

WIRING (cc_runner side — see ``CCB_GATEWAY_URL`` / ``CCB_GATEWAY_USAGE_DIR``)
----------------------------------------------------------------------------
Launch this once:

    python -m bench.gateway_server --port 8080 --usage-dir runs/usage [--mode vertex]

then run the bench with the shared gateway:

    CCB_GATEWAY_URL=http://127.0.0.1:8080 \
    CCB_GATEWAY_USAGE_DIR=runs/usage \
    python -m bench.cc_runner --arms baseline,dasein ...

cc_runner then points the gateway-direct arms (baseline/woz) at ``CCB_GATEWAY_URL``
and reads each run's usage from ``CCB_GATEWAY_USAGE_DIR/<run_id>.usage.jsonl``;
proxy arms keep pointing Claude Code at the vendor proxy, whose UPSTREAM is
provisioned to ``CCB_GATEWAY_URL``.

CLEAN-ROOM
----------
Pure stdlib at import time. NO ``adaptive_context`` import. The heavy ``anthropic``
SDK (``AnthropicVertex``) is imported LAZILY by ``UsageGateway`` only on the first
Vertex request, so ``import bench.gateway_server`` works on a box without it.
"""

from __future__ import annotations

import argparse
import os
import time

from bench.usage_gateway import (
    MODE_PASSTHROUGH, MODE_VERTEX,
    UsageGateway,
    VERTEX_LOCATION, VERTEX_MODEL, VERTEX_PROJECT,
)


def build_parser() -> argparse.ArgumentParser:
    """The standalone gateway's CLI.

    ``--port`` / ``--usage-dir`` are the two knobs the spec requires; the rest mirror
    the per-run gateway's Vertex routing (and a passthrough escape hatch) so the
    shared gateway can be retargeted without a code change. Defaults fall back to the
    same env vars the in-process gateway reads, so a single ``.env`` configures both.
    """
    ap = argparse.ArgumentParser(
        prog="bench.gateway_server",
        description="Standalone shared usage gateway (Vertex bridge or passthrough) "
                    "bound to a FIXED port so vendor proxies can forward to a stable "
                    "address; run-id-isolated, concurrency-safe (ThreadingHTTPServer).",
    )
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("CCB_GATEWAY_PORT", "8080")),
                    help="FIXED port to bind (default 8080 / $CCB_GATEWAY_PORT). "
                         "Use a non-zero port so a vendor proxy can be provisioned "
                         "to this stable address.")
    ap.add_argument("--host", default=os.environ.get("CCB_GATEWAY_HOST", "127.0.0.1"),
                    help="bind host (default 127.0.0.1; use 0.0.0.0 to accept "
                         "off-box vendor proxies)")
    ap.add_argument("--usage-dir",
                    default=os.environ.get("CCB_GATEWAY_USAGE_DIR",
                                           os.environ.get("CCB_GATEWAY_LOG_DIR", "runs/usage")),
                    help="dir for per-run usage JSONL (<run_id>.usage.jsonl), keyed "
                         "by the x-ccb-run-id header (default runs/usage / "
                         "$CCB_GATEWAY_USAGE_DIR). cc_runner reads the SAME dir.")
    ap.add_argument("--mode", default=os.environ.get("CCB_GATEWAY_MODE", MODE_VERTEX),
                    choices=[MODE_VERTEX, MODE_PASSTHROUGH],
                    help="vertex: AnthropicVertex native bridge (default); "
                         "passthrough: forward verbatim to --upstream")
    ap.add_argument("--upstream",
                    default=os.environ.get("CCB_GATEWAY_UPSTREAM", "https://api.anthropic.com"),
                    help="(passthrough mode) upstream base URL to forward to")
    ap.add_argument("--vertex-model", default=VERTEX_MODEL,
                    help="(vertex mode) Vertex Claude model id (a leading "
                         "'vertex_ai/' is stripped for AnthropicVertex)")
    ap.add_argument("--vertex-project", default=VERTEX_PROJECT)
    ap.add_argument("--vertex-location", default=VERTEX_LOCATION)
    ap.add_argument("--timeout-s", type=float,
                    default=float(os.environ.get("CCB_GATEWAY_TIMEOUT_S", "600")),
                    help="per-request upstream timeout (passthrough mode)")
    return ap


def main(argv: list | None = None) -> int:
    """Run ONE shared UsageGateway on a FIXED port, serving until killed.

    Builds the gateway (Vertex mode by default — the bottom bridge to claude-sonnet
    on Vertex via ADC), starts the ThreadingHTTPServer (concurrency-safe under the
    runner's 8-wide pool), prints the BOUND URL so provisioning can confirm each
    vendor proxy's upstream == this URL, and blocks until Ctrl-C / SIGTERM. No
    ``default_run_id`` is set: every request MUST carry the x-ccb-run-id header, so
    each run's usage lands in its own JSONL (an untagged request falls back to the
    sink's ``default`` file, which is fine — it just isolates the stray).
    """
    a = build_parser().parse_args(argv)

    gw = UsageGateway(
        a.upstream,
        log_dir=a.usage_dir,
        host=a.host,
        port=a.port,
        # No default_run_id: the shared gateway tags strictly by the per-request
        # x-ccb-run-id header so concurrent runs never share a usage file.
        default_run_id="",
        timeout_s=a.timeout_s,
        mode=a.mode,
        vertex_model=a.vertex_model,
        vertex_project=a.vertex_project,
        vertex_location=a.vertex_location,
    ).start()

    if a.mode == MODE_VERTEX:
        dest = (f"vertex({a.vertex_model} @ {a.vertex_project}/{a.vertex_location}, "
                f"ADC auth)")
    else:
        dest = a.upstream
    # Print the BOUND url (host:actual_port) so provisioning can set each vendor
    # proxy's upstream to exactly this, and confirm it in the worker log.
    print(f"CCB_GATEWAY_URL={gw.base_url}", flush=True)
    print(f"usage_gateway [{a.mode}] SHARED: {gw.base_url} -> {dest}  "
          f"(usage -> {a.usage_dir}/<run_id>.usage.jsonl, "
          f"ThreadingHTTPServer, run-id-isolated)", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        gw.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
