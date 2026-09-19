"""parsec — Dasein's parsec proxy (v0.2.x) + Claude Code plugin, spawned per solve
(ProxyArm + plugin).

Today's Dasein product is the ``parsec`` binary + Claude Code plugin + a HOSTED
brain (github.com/daseinlabs/parsec). This arm runs the shipped product through
its public interface; it is a clean-room THIN CLIENT — it imports NO vendor
internals and hard-codes none of the proxy's behaviour.

TOPOLOGY (bench ``parsec`` arm — the single bottom bridge is preserved):

    Claude Code --ANTHROPIC_BASE_URL--> parsec proxy   (spawned per solve on a
                                         free 127.0.0.1 port, isolated HOME)
    parsec proxy --PARSEC_UPSTREAM=<this run's gateway>--> gateway --> Anthropic

  Claude Code ALSO loads the parsec plugin, so the product's own tools/hooks/
  skills are in play (``mcp__plugin_parsec_scout__*`` etc.). ``replace_tools`` is
  False: parsec does NOT remove the native tool surface.

  The proxy uses the HOSTED PRODUCTION brain + production contract: we set
  neither ``PARSEC_BRAIN_URL`` nor ``PARSEC_BRAIN_DEV_RAW`` (that would select a
  dev/raw scorer), so the release-baked hosted brain is used. Its
  ``credentials.json`` is copied into the isolated proxy HOME so the proxy can
  authenticate to the hosted brain.

  The ranking cost still comes from the gateway usage rows (as for every arm).
  The proxy's own ``savings-ledger/v0`` is copied into the run dir and summarised
  as RunRecord diagnostics only.

FAIL-CLOSED: if the binary/credentials are missing, or a live probe cannot
confirm the proxy has a brain configured (a brain-less proxy fail-opens to
passthrough and would mislabel a plain run "parsec"), the arm SKIPS with a
precise reason — never a mislabeled passthrough run.

ENV (see arms/README.md + .env.example):
  PARSEC_BIN               path to the parsec binary (runs ``<bin> proxy``). REQUIRED.
  PARSEC_PLUGIN_DIR        installed parsec Claude Code plugin dir (contains
                           .claude-plugin/plugin.json). REQUIRED. The plugin cache
                           path on the box is discovered at run time and set here.
  PARSEC_CREDENTIALS       path to the hosted-brain credentials.json copied into the
                           proxy HOME (default: ``$HOME/.parsec/credentials.json``). REQUIRED.
  PARSEC_PROXY_STATUS_PATH status route probed on the spawned proxy to read the brain
                           checkpoint_id (default tries a small list — the shipped
                           proxy's status surface is a box-smoke item; set this to the
                           real route once confirmed).
  PARSEC_BENCH_CKPT_SHA256 optional expected brain checkpoint sha256; a mismatch SKIPS
                           (a benchmark against the wrong bundle is contaminated).
  PARSEC_PROXY_TIMEOUT_S   proxy come-up timeout (default 20s).
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Optional

from bench.arm import ProxyArm, register

# Candidate status routes probed to read the brain checkpoint_id, in order. The
# shipped proxy's status surface is a box-smoke item; PARSEC_PROXY_STATUS_PATH
# overrides this list with the confirmed route.
_DEFAULT_STATUS_PATHS = ["/status", "/health", "/internal/status", "/v1/status"]
_DEFAULT_PROXY_TIMEOUT_S = 20.0


def _free_port() -> int:
    """An OS-assigned free TCP port on 127.0.0.1 (bind 0, read, release)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _resolve_bin() -> Optional[str]:
    """Resolve the parsec binary: PARSEC_BIN, else `parsec` on PATH."""
    b = os.environ.get("PARSEC_BIN")
    if b and (os.path.isfile(b) or shutil.which(b)):
        return b
    which = shutil.which("parsec")
    return which


def _credentials_src() -> str:
    return os.environ.get(
        "PARSEC_CREDENTIALS",
        str(Path(os.path.expanduser("~")) / ".parsec" / "credentials.json"),
    )


def _http_get_json(url: str, timeout: float = 5.0) -> Optional[dict]:
    """GET a URL and parse JSON. None on any failure (never raises)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 — localhost
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def summarize_savings_ledger(rows: list[dict]) -> dict:
    """Clean-room roll-up of ``savings-ledger/v0`` rows (no vendor import).

    Per probed row ``tokens_saved = counterfactual_input_tokens - served_input``
    where ``served_input = billed_input + billed_cache_read + billed_cache_write``
    (the same partition the Anthropic usage object reports). Rows whose probe
    failed (``counterfactual_input_tokens is None``) are EXCLUDED from
    ``tokens_saved`` but still counted in the billed totals — never estimated.
    ``tokens_saved`` is signed on purpose (a proxy that adds overhead is negative).
    """
    t = {
        "contract_version": "savings-ledger/v0",
        "requests": 0,
        "probed_requests": 0,
        "null_probe_requests": 0,
        "counterfactual_input_tokens": 0,
        "served_input_tokens": 0,
        "billed_input_tokens": 0,
        "billed_output_tokens": 0,
        "billed_cache_read_tokens": 0,
        "billed_cache_write_tokens": 0,
        "tokens_saved": 0,
        "fail_opens": 0,
    }
    for row in rows:
        t["requests"] += 1
        bi = int(row.get("billed_input_tokens") or 0)
        bo = int(row.get("billed_output_tokens") or 0)
        br = int(row.get("billed_cache_read_tokens") or 0)
        bw = int(row.get("billed_cache_write_tokens") or 0)
        t["billed_input_tokens"] += bi
        t["billed_output_tokens"] += bo
        t["billed_cache_read_tokens"] += br
        t["billed_cache_write_tokens"] += bw
        served = bi + br + bw
        t["served_input_tokens"] += served
        cf = row.get("counterfactual_input_tokens")
        if cf is None:
            t["null_probe_requests"] += 1
        else:
            cf = int(cf)
            t["probed_requests"] += 1
            t["counterfactual_input_tokens"] += cf
            t["tokens_saved"] += cf - served
        if row.get("fail_open"):
            t["fail_opens"] += 1
    cf_tot = t["counterfactual_input_tokens"]
    t["savings_rate"] = (t["tokens_saved"] / cf_tot) if cf_tot else 0.0
    return t


def _read_ledger_rows(path: str, conv_id: str = "") -> list[dict]:
    """Read savings-ledger JSONL rows. If ``conv_id`` is given, keep only rows
    that carry it (the per-solve proxy HOME already isolates rows, so this is a
    defensive extra filter, applied only when the rows actually carry conv_id)."""
    rows: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    if conv_id and any(r.get("conv_id") for r in rows):
        rows = [r for r in rows if str(r.get("conv_id") or "") == conv_id]
    return rows


class _ParsecProxy:
    """A per-solve ``parsec proxy`` process, config entirely via env (argv is just
    ``[bin, "proxy"]``). Isolated HOME holds the ledger + copied credentials."""

    def __init__(self, bin_path: str, upstream: str, home_dir: str) -> None:
        self.bin_path = bin_path
        self.upstream = upstream
        self.home_dir = str(home_dir)
        self.port = 0
        self._proc: Optional[subprocess.Popen] = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def ledger_path(self) -> str:
        return str(Path(self.home_dir) / ".parsec" / "ledger.jsonl")

    def _env(self) -> dict:
        env = dict(os.environ)
        env["PARSEC_PROXY_PORT"] = str(self.port)
        env["PARSEC_UPSTREAM"] = self.upstream
        env["HOME"] = self.home_dir
        # HOSTED production brain: DO NOT set PARSEC_BRAIN_URL / PARSEC_BRAIN_DEV_RAW
        # (those select a dev/raw scorer). The release-baked hosted brain is used.
        env.pop("PARSEC_BRAIN_URL", None)
        env.pop("PARSEC_BRAIN_DEV_RAW", None)
        return env

    def start(self, timeout_s: float = _DEFAULT_PROXY_TIMEOUT_S, attempts: int = 3) -> "_ParsecProxy":
        # Copy the hosted-brain credentials into the isolated HOME.
        cred_src = _credentials_src()
        cred_dst_dir = Path(self.home_dir) / ".parsec"
        cred_dst_dir.mkdir(parents=True, exist_ok=True)
        if os.path.exists(cred_src):
            shutil.copyfile(cred_src, str(cred_dst_dir / "credentials.json"))
        for _ in range(attempts):
            self.port = _free_port()
            self._proc = subprocess.Popen(
                [self.bin_path, "proxy"], env=self._env(),
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                if self._proc.poll() is not None:
                    break  # died (e.g. port raced) — retry on a fresh port
                try:
                    with socket.create_connection(("127.0.0.1", self.port), timeout=1):
                        return self
                except OSError:
                    time.sleep(0.1)
            self.stop()
        raise RuntimeError(
            f"parsec proxy did not come up on 127.0.0.1 after {attempts} attempts "
            f"(bin={self.bin_path}, upstream={self.upstream})")

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        except Exception:
            pass


def _parsec_version(bin_path: str) -> str:
    try:
        out = subprocess.run([bin_path, "--version"], capture_output=True,
                             text=True, timeout=10)
        return (out.stdout or out.stderr or "").strip()[:120]
    except Exception:
        return ""


def _probe_checkpoint(base_url: str) -> tuple[bool, str]:
    """Probe the spawned proxy's status route(s) for the brain checkpoint_id.

    Returns (brain_configured, checkpoint_id). The shipped proxy's status surface
    is a box-smoke item, so the route is configurable (PARSEC_PROXY_STATUS_PATH)
    and we try a small default list. A route that reports a non-empty
    checkpoint_id (or an explicit brain flag) means the brain is engaged."""
    override = os.environ.get("PARSEC_PROXY_STATUS_PATH", "").strip()
    paths = [override] if override else list(_DEFAULT_STATUS_PATHS)
    for p in paths:
        if not p:
            continue
        js = _http_get_json(base_url.rstrip("/") + "/" + p.lstrip("/"))
        if not isinstance(js, dict):
            continue
        ckpt = str(js.get("checkpoint_id") or js.get("checkpointId") or "")
        brain_flag = js.get("brain_configured", js.get("brain"))
        if ckpt:
            return True, ckpt
        if brain_flag:
            return True, ckpt
    return False, ""


@register("parsec")
class ParsecArm(ProxyArm):
    name = "parsec"
    # Binary + plugin dir are hard requirements; credentials + a live brain probe
    # are checked in ready() (see module docstring — fail-closed).
    needs = ["PARSEC_BIN", "PARSEC_PLUGIN_DIR"]

    def __init__(self) -> None:
        # A ProxyArm that ALSO loads the shipped plugin (proxy + plugin). parsec
        # does NOT remove native tools, so replace_tools stays False.
        self.plugin_dir = os.environ.get("PARSEC_PLUGIN_DIR")
        self.plugin_tool_globs = ["mcp__plugin_parsec_*__*", "mcp__parsec*__*"]
        self.replace_tools = False
        self._proxy: Optional[_ParsecProxy] = None
        self._ledger_path: Optional[str] = None
        self.checkpoint_id = ""
        self.parsec_version = ""
        self.conv_id = ""

    # The proxy is spawned per solve, so the base URL is only known then: return
    # "" here and let start_run() supply the real URL.
    def model_base_url(self) -> str:
        return ""

    def headers(self) -> dict[str, str]:
        # Claude Code talks to the local proxy directly; brain auth lives on the
        # proxy (credentials.json in its HOME), not as a client header.
        return {}

    def ready(self) -> tuple[bool, str]:
        ok, reason = super().ready()  # PARSEC_BIN + PARSEC_PLUGIN_DIR non-empty
        if not ok:
            return ok, reason
        bin_path = _resolve_bin()
        if not bin_path:
            return False, ("PARSEC_BIN does not resolve to a parsec binary "
                           "(set PARSEC_BIN or put `parsec` on PATH)")
        plugin_dir = self.plugin_dir or ""
        if not os.path.isfile(os.path.join(plugin_dir, ".claude-plugin", "plugin.json")):
            return False, (f"PARSEC_PLUGIN_DIR={plugin_dir!r} is not an installed parsec "
                           f"plugin (missing .claude-plugin/plugin.json)")
        cred = _credentials_src()
        if not os.path.exists(cred):
            return False, (f"parsec credentials not found at {cred} — the hosted brain "
                           f"cannot authenticate (set PARSEC_CREDENTIALS or run `parsec login`)")
        self.parsec_version = _parsec_version(bin_path)
        # Live probe: spawn once against a throwaway upstream and confirm the brain
        # is configured (reports a checkpoint_id). A brain-less proxy fail-opens to
        # passthrough and would mislabel the run — SKIP fail-closed.
        probe = _ParsecProxy(bin_path, upstream="https://api.anthropic.com",
                             home_dir=os.path.join(
                                 os.environ.get("TMPDIR", "/tmp"),
                                 f"parsec_ready_{os.getpid()}"))
        try:
            probe.start()
        except Exception as e:  # noqa: BLE001
            return False, f"parsec proxy failed to start for readiness probe: {e}"
        try:
            brain_ok, ckpt = _probe_checkpoint(probe.base_url)
        finally:
            probe.stop()
        if not brain_ok:
            return False, (
                "parsec proxy came up but no brain checkpoint could be confirmed via a "
                "status route (tried "
                + (os.environ.get("PARSEC_PROXY_STATUS_PATH") or ", ".join(_DEFAULT_STATUS_PATHS))
                + "). A brain-less proxy fail-opens to passthrough and would mislabel the "
                "run 'parsec' — refusing. Set PARSEC_PROXY_STATUS_PATH to the confirmed "
                "status route (box-smoke item).")
        self.checkpoint_id = ckpt
        want = os.environ.get("PARSEC_BENCH_CKPT_SHA256", "").strip().lower()
        if want and ckpt.lower() != want:
            return False, (f"brain checkpoint_id {ckpt} != expected {want} — refusing: a "
                           f"benchmark against the wrong bundle is contaminated")
        return True, (f"ok (bin={bin_path}, plugin={plugin_dir}, ckpt={ckpt or '?'}, "
                      f"ver={self.parsec_version or '?'})")

    def start_run(self, ctx) -> Optional[str]:
        bin_path = _resolve_bin()
        if not bin_path:
            raise RuntimeError("PARSEC_BIN does not resolve to a parsec binary")
        home = os.path.join(ctx.run_dir, "proxy_home")
        self._proxy = _ParsecProxy(bin_path, upstream=ctx.upstream_base_url, home_dir=home)
        timeout = float(os.environ.get("PARSEC_PROXY_TIMEOUT_S", _DEFAULT_PROXY_TIMEOUT_S))
        self._proxy.start(timeout_s=timeout)
        self._ledger_path = self._proxy.ledger_path
        self.conv_id = ctx.run_id
        # capture the brain checkpoint for the record, best-effort
        try:
            _, ckpt = _probe_checkpoint(self._proxy.base_url)
            if ckpt:
                self.checkpoint_id = ckpt
        except Exception:
            pass
        return self._proxy.base_url

    def end_run(self) -> None:
        proxy, self._proxy = self._proxy, None
        if proxy is not None:
            proxy.stop()

    def ledger_path(self) -> Optional[str]:
        # survives end_run() so the runner can copy it after teardown
        return self._ledger_path

    def ledger_summary(self, run_id: str = "") -> dict:
        if not self._ledger_path or not os.path.exists(self._ledger_path):
            return {}
        rows = _read_ledger_rows(self._ledger_path, conv_id=self.conv_id or run_id)
        return summarize_savings_ledger(rows)
