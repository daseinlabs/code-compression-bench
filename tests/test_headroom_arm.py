"""Wiring tests for the headroom arm (Anthropic-native self-host proxy).

Headroom (github.com/chopratejas/headroom, PyPI ``headroom-ai``) is an
Anthropic-native compression proxy whose REAL default port is 8787 and whose
upstream is set via ``ANTHROPIC_TARGET_API_URL`` / ``--anthropic-api-url`` — NOT
a LiteLLM knob and NOT port 8803 (an earlier audit gap). These tests LOCK that
wiring so a future edit can't silently regress the port or the ready()-probe
contract that gates the arm:

  * ``DEFAULT_BASE_URL`` / ``model_base_url()`` default to the real port 8787;
  * an explicit ``HEADROOM_BASE_URL`` wins and its trailing slash is stripped;
  * ``headers()`` is empty (upstream lives INSIDE the proxy, not a client header);
  * ``ready()`` does a REAL TCP probe — it PASSES against a listening socket and
    SKIPs (ok=False, actionable reason) when nothing is listening, so a dead /
    un-launched proxy never passes the gate and infra-fails every paid run;
  * the .env.example default and the arm's DEFAULT_BASE_URL AGREE (the exact
    cross-doc mismatch the audit flagged: copying .env.example must not point
    Claude Code/ready() at a port the proxy isn't on).

Pure stdlib (socket + a throwaway listener); no network, no SDK, no vendor code.

Runnable two ways:
    py -m pytest tests/test_headroom_arm.py -q
    py tests/test_headroom_arm.py            # standalone, prints PASS/FAIL
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

from arms.headroom import DEFAULT_BASE_URL, HeadroomArm  # noqa: E402


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


# ── default port is the REAL 8787 (not the stale 8803) ───────────────────────
def test_default_base_url_is_8787():
    assert DEFAULT_BASE_URL == "http://127.0.0.1:8787"
    assert ":8803" not in DEFAULT_BASE_URL


def test_model_base_url_defaults_to_8787_when_env_unset():
    with _env(HEADROOM_BASE_URL=None):
        assert HeadroomArm().model_base_url() == "http://127.0.0.1:8787"


# ── explicit env wins; trailing slash stripped ───────────────────────────────
def test_env_overrides_and_strips_trailing_slash():
    with _env(HEADROOM_BASE_URL="http://127.0.0.1:9999/"):
        assert HeadroomArm().model_base_url() == "http://127.0.0.1:9999"


# ── no client-side auth header (upstream is inside the proxy) ─────────────────
def test_headers_are_empty():
    assert HeadroomArm().headers() == {}


# ── ready() does a REAL TCP probe: pass when listening ────────────────────────
def test_ready_true_when_proxy_listening():
    with _listening_socket() as base, _env(HEADROOM_BASE_URL=base):
        ok, reason = HeadroomArm().ready()
        assert ok is True, reason
        assert reason == "ok"


# ── ready() SKIPs (ok=False) when nothing is listening, w/ actionable reason ──
def test_ready_false_when_proxy_down():
    dead = f"http://127.0.0.1:{_free_port()}"
    with _env(HEADROOM_BASE_URL=dead):
        ok, reason = HeadroomArm().ready()
        assert ok is False
        # reason must tell the operator how to launch the real product
        assert "headroom proxy" in reason
        assert "--anthropic-api-url" in reason


# ── ready() SKIPs (ok=False) when the env var is missing entirely ────────────
def test_ready_false_when_env_missing():
    with _env(HEADROOM_BASE_URL=None):
        ok, reason = HeadroomArm().ready()
        assert ok is False
        assert "HEADROOM_BASE_URL" in reason


# ── cross-doc: .env.example default AGREES with the arm's DEFAULT_BASE_URL ────
def test_env_example_default_matches_arm_default():
    """The audit gap: .env.example must NOT hand operators a port the proxy
    isn't on. The HEADROOM_BASE_URL default in .env.example must equal the arm's
    DEFAULT_BASE_URL (8787), or ready()'s TCP probe SKIPs headroom every run."""
    env_example = (_ROOT / ".env.example").read_text(encoding="utf-8")
    m = re.search(r"^HEADROOM_BASE_URL=(\S+)", env_example, re.MULTILINE)
    assert m, "HEADROOM_BASE_URL not found (uncommented) in .env.example"
    assert m.group(1) == DEFAULT_BASE_URL
    assert "8803" not in m.group(1)


# ── standalone runner (py tests/test_headroom_arm.py) ────────────────────────
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
