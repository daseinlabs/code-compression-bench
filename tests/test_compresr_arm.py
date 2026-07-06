"""Wiring tests for the compresr arm (Compresr Context Gateway, Anthropic-native).

Compresr's Context Gateway (github.com/Compresr-ai/Context-Gateway, Go,
Apache-2.0) is an Anthropic-native compression proxy whose REAL default port is
18081 (cmd/configs/fast_setup.yaml) — NOT the stale 8804 the old .env.example
shipped (an audit gap). Critically, the var the arm reads is
``COMPRESR_GATEWAY_URL`` (the LOCAL gateway Claude Code points at), NOT
``COMPRESR_BASE_URL`` — in the real product COMPRESR_BASE_URL/_API_KEY are the
compression-SERVICE creds (the gateway's call OUT to api.compresr.ai), which live
ON THE GATEWAY and are never a client bearer. These tests LOCK that wiring so a
future edit can't silently regress the port, the env-var name, or the
ready()-probe contract that gates the arm:

  * ``DEFAULT_BASE_URL`` / ``model_base_url()`` default to the real port 18081;
  * ``COMPRESR_GATEWAY_URL`` is the var the arm needs (``needs``) and wins;
  * an explicit ``COMPRESR_GATEWAY_URL`` wins and its trailing slash is stripped;
  * ``headers()`` is empty (no client bearer — the gateway uses Claude Code's own
    auth via skip_api_key_setup; the cmp_ key lives ON THE GATEWAY);
  * ``ready()`` does a REAL TCP probe — it PASSES against a listening socket and
    SKIPs (ok=False, actionable reason) when nothing is listening, so a dead /
    un-launched gateway never passes the gate and burns a paid run;
  * the .env.example default and the arm's DEFAULT_BASE_URL AGREE (the exact
    cross-doc mismatch the audit flagged: copying .env.example must not point
    Claude Code/ready() at a port the gateway isn't on, and must surface
    COMPRESR_GATEWAY_URL — not the stale COMPRESR_BASE_URL=8804).

Pure stdlib (socket + a throwaway listener); no network, no SDK, no vendor code.

Runnable two ways:
    py -m pytest tests/test_compresr_arm.py -q
    py tests/test_compresr_arm.py            # standalone, prints PASS/FAIL
"""

from __future__ import annotations

import os
import re
import socket
import sys
from contextlib import contextmanager
from pathlib import Path

# make the repo root importable when run as a bare script (py tests/...).
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from arms.compresr import DEFAULT_BASE_URL, CompresrArm  # noqa: E402


@contextmanager
def _env(**kv):
    """Temporarily set/clear env vars, restoring the prior state after."""
    saved = {k: os.environ.get(k) for k in kv}
    try:
        for k, v in kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextmanager
def _listening_socket():
    """A real TCP listener on 127.0.0.1:<ephemeral>; yields its base URL."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.close()


def _free_port() -> int:
    """Grab then immediately release a port so nothing is listening on it."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ── default port is the REAL 18081 (not the stale 8804) ──────────────────────
def test_default_base_url_is_18081():
    assert DEFAULT_BASE_URL == "http://127.0.0.1:18081"
    assert ":8804" not in DEFAULT_BASE_URL


def test_model_base_url_defaults_to_18081_when_env_unset():
    with _env(COMPRESR_GATEWAY_URL=None, COMPRESR_BASE_URL=None):
        assert CompresrArm().model_base_url() == "http://127.0.0.1:18081"


# ── the arm needs COMPRESR_GATEWAY_URL (not the compression-service creds) ────
def test_needs_is_the_gateway_url_not_base_url():
    needs = CompresrArm().needs
    assert "COMPRESR_GATEWAY_URL" in needs
    # COMPRESR_BASE_URL / _API_KEY are the gateway's OWN upstream creds, not the
    # var the client/arm reads — they must NOT be required of the client.
    assert "COMPRESR_BASE_URL" not in needs
    assert "COMPRESR_API_KEY" not in needs


# ── explicit COMPRESR_GATEWAY_URL wins; trailing slash stripped ──────────────
def test_gateway_url_overrides_and_strips_trailing_slash():
    with _env(COMPRESR_GATEWAY_URL="http://127.0.0.1:9999/", COMPRESR_BASE_URL=None):
        assert CompresrArm().model_base_url() == "http://127.0.0.1:9999"


def test_gateway_url_wins_over_legacy_base_url():
    """If both are set, COMPRESR_GATEWAY_URL (the local gateway) wins over the
    legacy COMPRESR_BASE_URL fallback — copying an old .env can't mis-wire it."""
    with _env(
        COMPRESR_GATEWAY_URL="http://127.0.0.1:18081",
        COMPRESR_BASE_URL="https://api.compresr.ai",
    ):
        assert CompresrArm().model_base_url() == "http://127.0.0.1:18081"


# ── no client-side auth header (cmp_ key lives ON THE GATEWAY) ────────────────
def test_headers_are_empty():
    assert CompresrArm().headers() == {}


# ── ready() does a REAL TCP probe: pass when listening ────────────────────────
def test_ready_true_when_gateway_listening():
    with _listening_socket() as base, _env(COMPRESR_GATEWAY_URL=base):
        ok, reason = CompresrArm().ready()
        assert ok is True, reason
        assert reason == "ok"


# ── ready() SKIPs (ok=False) when nothing is listening, w/ actionable reason ──
def test_ready_false_when_gateway_down():
    dead = f"http://127.0.0.1:{_free_port()}"
    with _env(COMPRESR_GATEWAY_URL=dead):
        ok, reason = CompresrArm().ready()
        assert ok is False
        # reason must tell the operator how to launch the real product
        assert "Context Gateway" in reason
        assert "ANTHROPIC_PROVIDER_URL" in reason


# ── ready() SKIPs (ok=False) when the env var is missing entirely ────────────
def test_ready_false_when_env_missing():
    with _env(COMPRESR_GATEWAY_URL=None, COMPRESR_BASE_URL=None):
        ok, reason = CompresrArm().ready()
        assert ok is False
        assert "COMPRESR_GATEWAY_URL" in reason


# ── cross-doc: .env.example default AGREES with the arm's DEFAULT_BASE_URL ────
def test_env_example_surfaces_gateway_url_matching_arm_default():
    """The audit gap: .env.example shipped a stale COMPRESR_BASE_URL=8804 and
    NEVER surfaced the var the arm reads (COMPRESR_GATEWAY_URL). It must now
    surface COMPRESR_GATEWAY_URL with a default equal to the arm's
    DEFAULT_BASE_URL (18081), or a copy-from-template run mis-wires / SKIPs."""
    env_example = (_ROOT / ".env.example").read_text(encoding="utf-8")
    m = re.search(r"^COMPRESR_GATEWAY_URL=(\S+)", env_example, re.MULTILINE)
    assert m, "COMPRESR_GATEWAY_URL not found (uncommented) in .env.example"
    assert m.group(1) == DEFAULT_BASE_URL
    assert "8804" not in m.group(1)


def test_env_example_does_not_ship_stale_active_compresr_base_url():
    """COMPRESR_BASE_URL must NOT be shipped as an ACTIVE (uncommented) client
    var pointing at the dead 8804 port — that was the mis-wiring the audit
    flagged. It may appear only in comments (explaining it's a gateway-side cred)."""
    env_example = (_ROOT / ".env.example").read_text(encoding="utf-8")
    active = re.search(r"^COMPRESR_BASE_URL=", env_example, re.MULTILINE)
    assert active is None, "COMPRESR_BASE_URL must not be an active client var"
    assert "8804" not in env_example


# ── standalone runner (py tests/test_compresr_arm.py) ────────────────────────
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
