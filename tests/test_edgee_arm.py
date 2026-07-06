"""Wiring tests for the edgee arm (edgee-ai/edgee — a forked, Anthropic-native local gateway).

Edgee is an open-source Rust CLI AI gateway with a Claude-Code token-compression
engine. Its `edgee local-gateway` routes `POST /v1/messages` through the real
Anthropic passthrough + Claude CompressionLayer. Real Edgee hardcoded the Anthropic
upstream to api.anthropic.com (the `with_base_url` override existed + was unit-tested,
but `local_gateway::start()` never called it), so it couldn't forward to the bench's
run gateway. The fork in selfhost/edgee/ wires that override from EDGEE_ANTHROPIC_UPSTREAM.

An earlier audit caught the arm with a fictitious default port (8801, not Edgee's real
8787), a non-existent `edgee/edgee:latest` Docker image, and NO ready() probe — so a
dead/un-launched gateway passed the env gate and infra-failed every paid run. These
tests LOCK the corrected contract so a future edit can't regress it:

  * EdgeeArm is a ProxyArm registered under "edgee", needs=['EDGEE_BASE_URL'];
  * its default base URL is Edgee's REAL local-gateway port 8787 (NOT 8801);
  * it injects NO client headers (the upstream lives INSIDE Edgee via
    EDGEE_ANTHROPIC_UPSTREAM, not as a client bearer);
  * ready() does a REAL TCP probe: PASS when something is listening on the port, and
    SKIP (ok=False, actionable launch hint) when nothing is — instead of passing the
    gate on a dead port;
  * the .env.example carries no fictitious 8801 and the selfhost fork assets exist.

Pure stdlib (a throwaway loopback listener); no network egress, no SDK, no vendor code.

Runnable two ways:
    py -m pytest tests/test_edgee_arm.py -q
    py tests/test_edgee_arm.py            # standalone, prints PASS/FAIL
"""

from __future__ import annotations

import socket
import sys
from contextlib import closing, contextmanager
from pathlib import Path

# make the repo root importable when run as a bare script (py tests/...).
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bench.arm import ArmKind, ProxyArm, get_arm  # noqa: E402
from arms.edgee import DEFAULT_BASE_URL, EdgeeArm  # noqa: E402


@contextmanager
def _listening_port():
    """Bind a loopback TCP listener and yield its port; closed on exit."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        yield s.getsockname()[1]


def _free_port() -> int:
    """Return a port number that is NOT currently bound (best-effort)."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ─────────────────────────────── tests ──────────────────────────────────────
def test_edgee_is_a_proxy_arm_registered():
    """EdgeeArm registers under 'edgee' and is a ProxyArm (not a hook/tool arm)."""
    arm = get_arm("edgee")
    assert isinstance(arm, EdgeeArm)
    assert isinstance(arm, ProxyArm)
    assert arm.kind == ArmKind.PROXY
    assert arm.name == "edgee"
    assert arm.needs == ["EDGEE_BASE_URL"]


def test_default_port_is_edgee_real_8787_not_8801(monkeypatch):
    """Default base URL is Edgee's REAL local-gateway port 8787 (was fictitious 8801)."""
    assert DEFAULT_BASE_URL == "http://127.0.0.1:8787"
    monkeypatch.delenv("EDGEE_BASE_URL", raising=False)
    assert EdgeeArm().model_base_url() == "http://127.0.0.1:8787"
    assert ":8801" not in DEFAULT_BASE_URL


def test_model_base_url_env_override_and_strip(monkeypatch):
    """EDGEE_BASE_URL overrides the default; trailing slash is stripped."""
    monkeypatch.setenv("EDGEE_BASE_URL", "http://127.0.0.1:9099/")
    assert EdgeeArm().model_base_url() == "http://127.0.0.1:9099"


def test_no_client_headers(monkeypatch):
    """No client bearer: the upstream is configured INSIDE Edgee, not sent by us."""
    monkeypatch.setenv("EDGEE_BASE_URL", "http://127.0.0.1:8787")
    assert EdgeeArm().headers() == {}


def test_ready_passes_when_gateway_is_listening(monkeypatch):
    """ready() PASSES when something is actually listening on the port (real probe)."""
    with _listening_port() as port:
        monkeypatch.setenv("EDGEE_BASE_URL", f"http://127.0.0.1:{port}")
        ok, reason = EdgeeArm().ready()
        assert ok, reason
        assert reason == "ok"


def test_ready_skips_when_gateway_dead_with_launch_hint(monkeypatch):
    """ready() SKIPs (ok=False) on a dead port — NOT pass the gate and infra-fail."""
    dead = _free_port()
    monkeypatch.setenv("EDGEE_BASE_URL", f"http://127.0.0.1:{dead}")
    ok, reason = EdgeeArm().ready()
    assert not ok
    # actionable: names the unreachable endpoint AND the build+launch path.
    assert f"127.0.0.1:{dead}" in reason
    assert "selfhost/edgee/build.sh" in reason
    assert "launch.sh" in reason


def test_ready_skips_when_env_missing(monkeypatch):
    """Without EDGEE_BASE_URL the base ready() check SKIPs before any probe."""
    monkeypatch.delenv("EDGEE_BASE_URL", raising=False)
    ok, reason = EdgeeArm().ready()
    assert not ok
    assert "EDGEE_BASE_URL" in reason


def test_env_example_has_no_fictitious_8801():
    """.env.example must not advertise the fictitious 8801 default any more."""
    env = (_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "EDGEE_BASE_URL=http://127.0.0.1:8787" in env
    assert ":8801" not in env


def test_fork_assets_present():
    """The selfhost fork assets (patch + build/launch scripts) ship with the repo."""
    base = _ROOT / "selfhost" / "edgee"
    assert (base / "anthropic_upstream.patch").is_file()
    assert (base / "build.sh").is_file()
    assert (base / "launch.sh").is_file()
    patch = (base / "anthropic_upstream.patch").read_text(encoding="utf-8")
    # the patch must wire the env var into the local gateway via with_base_url.
    assert "EDGEE_ANTHROPIC_UPSTREAM" in patch
    assert "with_base_url" in patch
    assert "local_gateway.rs" in patch


# ── standalone runner (py tests/test_edgee_arm.py) ───────────────────────────
class _MonkeyPatch:
    """Minimal monkeypatch shim so the tests run without pytest installed."""

    def __init__(self):
        import os
        self._os = os
        self._saved: list[tuple[str, str | None]] = []

    def setenv(self, k, v):
        self._saved.append((k, self._os.environ.get(k)))
        self._os.environ[k] = v

    def delenv(self, k, raising=False):
        self._saved.append((k, self._os.environ.get(k)))
        self._os.environ.pop(k, None)

    def undo(self):
        for k, v in reversed(self._saved):
            if v is None:
                self._os.environ.pop(k, None)
            else:
                self._os.environ[k] = v
        self._saved.clear()


def _run_standalone() -> int:
    import inspect
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failures = 0
    for t in tests:
        mp = _MonkeyPatch()
        try:
            if "monkeypatch" in inspect.signature(t).parameters:
                t(mp)
            else:
                t()
            print(f"PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL  {t.__name__}: {type(e).__name__}: {e}")
        finally:
            mp.undo()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
