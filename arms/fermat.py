"""fermat — Quotient Labs' "Fermat's Last Token", run as its shipped `claude` shim.

Fermat (quotientlabs.com; install: ``curl -fsSL
https://downloads.quotientlabs.com/fermat/install.sh | bash && fermat login``)
is a Node runtime that ships a ``claude`` SHIM (``bin/claude`` -> ``node
cli.js``). The shim starts a local proxy DAEMON (``server.js``, port in
``~/.fermat/runtime.json``) and spawns the REAL Claude Code with the proxy as
its upstream, forces ``--agent fermat-code``, attaches its Search/Edit MCP
facades + a native-file tool-gate, bash-output and idle compression, and a
hosted "Stage 3" sidecar. Every feature is entitlement-gated: without an
entitled login it silently falls back to vanilla passthrough.

We run the product AS SHIPPED: point the SDK's executable at the Fermat shim
(``cli_path``) and let Fermat do everything. The shim's real-Claude target is
``FERMAT_CLAUDE_BIN`` and its proxy upstream is our gateway (``FERMAT_UPSTREAM``).

FAIL-CLOSED everywhere: the arm SKIPS/FAILS rather than let a mislabeled vanilla
row through, and it REFUSES concurrency (fermat has ONE session seat).

CRITICAL WIRING (each fixed a real bypass observed on the box):

  1. DAEMON UPSTREAM ENV.  The two MCP facades (searchFacade.js / editFacade.js)
     spawn a DETACHED fermat daemon via ``ensureDaemon``. If that daemon is not
     handed ``FERMAT_UPSTREAM`` (+ the sibling upstream vars) it defaults to
     ``api.anthropic.com`` and BYPASSES the bench gateway — untracked spend and
     invalid rows. We therefore pass the full FERMAT_*/FILETOFISH_* env (incl.
     the upstream vars, FERMAT_CLAUDE_BIN, FERMAT_DISPLAY_DISABLED,
     FERMAT_API_TOKEN, FERMAT_CREDENTIALS) into EACH facade's ``env`` — never
     ``{}`` — and, before every solve, verify the RUNNING daemon's upstream via
     its ``/health`` and kill+respawn a daemon pointed at the wrong upstream.

  2. SINGLE-SEAT CONCURRENCY.  fermat's one session seat hits "claude session
     binding" contention at workers>1 -> silent vanilla fallback / bypass. The
     arm REFUSES to run more than one solve at a time (a cross-process advisory
     lock) unless ``FERMAT_ALLOW_CONCURRENT=1``, and also refuses up-front when
     the launcher advertises ``FERMAT_WORKERS>1``.

  3. SESSION LAPSE.  ``fermat login`` is browser OAuth; the ACCESS token lives
     ~2 h (``expires_at`` in ~/.fermat/credentials). The runtime CAN refresh it
     headlessly (Supabase rotating refresh token: POST
     ``$FERMAT_AUTH_URL/auth/v1/token?grant_type=refresh_token`` with the app's
     anon ``apikey``), but the on-disk refresh token is SINGLE-USE: once the
     rotation chain breaks the only cure is a fresh ``fermat login``. So before
     EVERY task the arm (a) proactively refreshes + persists the rotated creds
     while the chain is alive, and (b) on a true lapse PAUSES the run (blocks,
     polling ``fermat whoami`` every 60 s, logging) so a re-login by the operator
     mid-run resumes it cleanly — it NEVER skips or drops to vanilla.

ENV:
  FERMAT_SHIM              path to the Fermat `claude` shim (~/fermat/bin/claude). REQUIRED.
  FERMAT_BIN               path to the `fermat` CLI (whoami/--version); default: sibling
                           of FERMAT_SHIM, else `fermat` on PATH.
  FERMAT_RUNTIME           the runtime dir (cli.js/server.js/fermat-mcp-dist/agents);
                           default: ``<dirname(dirname(FERMAT_SHIM))>/runtime``.
  FERMAT_CLAUDE_BIN        the REAL claude executable Fermat wraps.
  FERMAT_UPSTREAM          the proxy upstream = THIS run's gateway URL. Also mirrored to
                           FERMAT_UPSTREAM_BASE_URL / FILETOFISH_UPSTREAM.
  FERMAT_API_TOKEN         Stage-2/3 bearer (from ~/secrets/fermat.env). Forwarded, never logged.
  FERMAT_CREDENTIALS       path to the login credentials json; default ~/.fermat/credentials.
  FERMAT_ALLOW_CONCURRENT  set to "1" to disable the single-seat refusal (NOT recommended).
  FERMAT_WORKERS           launcher-advertised worker count (fail-closed if >1).
  FERMAT_AUTH_URL          override the auth base (default decoded from runtime; else
                           https://auth.quotientlabs.com).
  FERMAT_AUTH_APIKEY       override the Supabase anon apikey for the refresh call.
  (any other FERMAT_* / FILETOFISH_* var in the operator env is forwarded to the facades.)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.request
import urllib.error
from typing import Optional

from bench.arm import Arm, ArmKind, register

# Substrings in `fermat whoami` output that indicate an ENTITLED/logged-in account.
_ENTITLED_HINTS = ("entitled", "logged in", "logged-in", "active", "subscription",
                   "plan:", "tier:", "@")
# Substrings that indicate NOT logged in / not entitled (checked first).
_NOT_ENTITLED_HINTS = ("not signed in", "not logged in", "not logged-in", "no account",
                       "unauthenticated", "please log in", "please login",
                       "run `fermat login`", "run: fermat login", "not entitled",
                       "logged out", "no active", "signed out")

# ClaudeAgentOptions field names (across SDK versions) that point the SDK at a
# specific `claude` executable. _run_sdk tries the same candidate names to place cli_path.
CLI_PATH_FIELD_CANDIDATES = ("cli_path", "path_to_claude_code_executable",
                            "claude_code_executable", "claude_executable", "executable")

# --- token refresh (Supabase GoTrue rotating refresh token) -------------------
# Endpoint + path decoded from the shipped runtime (cli.impl.js) on 2026-09-20;
# the apikey is Fermat's PUBLIC Supabase anon key (embedded in their client
# bundle, not a user secret). We prefer to decode both LIVE from the runtime so
# they self-heal across vendor updates; these are the fallback defaults.
_FERMAT_AUTH_URL_DEFAULT = "https://auth.quotientlabs.com"
_FERMAT_AUTH_REFRESH_PATH = "/auth/v1/token?grant_type=refresh_token"
# Refresh proactively when the access token has less than this many seconds left.
_REFRESH_SKEW_S = 300
# Poll cadence while PAUSED on a session lapse.
_PAUSE_POLL_S = 60


def _log(msg: str) -> None:
    print("[fermat] " + msg, flush=True)


def _sdk_cli_path_field() -> Optional[str]:
    """The installed SDK's ClaudeAgentOptions field for a custom executable, or
    None. Lazy import (the SDK is absent on dev boxes)."""
    try:
        import dataclasses as _dc
        from claude_agent_sdk import ClaudeAgentOptions  # lazy
        fields = {f.name for f in _dc.fields(ClaudeAgentOptions)}
    except Exception:
        return None
    for cand in CLI_PATH_FIELD_CANDIDATES:
        if cand in fields:
            return cand
    return None


def _resolve_fermat_cli() -> Optional[str]:
    """Resolve the `fermat` CLI (whoami/--version): FERMAT_BIN, else a sibling
    `fermat` next to FERMAT_SHIM, else `fermat` on PATH."""
    b = os.environ.get("FERMAT_BIN")
    if b and (os.path.isfile(b) or shutil.which(b)):
        return b
    shim = os.environ.get("FERMAT_SHIM")
    if shim:
        sib = os.path.join(os.path.dirname(shim), "fermat")
        if os.path.isfile(sib):
            return sib
    return shutil.which("fermat")


def _runtime_dir() -> str:
    shim = os.environ.get("FERMAT_SHIM") or ""
    return os.environ.get("FERMAT_RUNTIME") or os.path.join(
        os.path.dirname(os.path.dirname(shim)), "runtime")


def _creds_path() -> str:
    return os.environ.get("FERMAT_CREDENTIALS") or os.path.expanduser("~/.fermat/credentials")


def _fermat_version(cli: str) -> str:
    try:
        out = subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=15)
        return (out.stdout or out.stderr or "").strip()[:120]
    except Exception:
        return ""


def _parse_agent_md(md_path):
    """Return (description, prompt_body) parsed from an agent markdown file with
    YAML-ish frontmatter (``--- ... ---`` then the system-prompt body)."""
    txt = open(md_path, encoding="utf-8").read()
    desc = ""
    body = txt
    if txt.startswith("---"):
        parts = txt.split("---", 2)
        if len(parts) == 3:
            front, body = parts[1], parts[2]
            for line in front.splitlines():
                if line.strip().lower().startswith("description:"):
                    desc = line.split(":", 1)[1].strip()
    return desc, body.strip()


def _facade_env() -> dict:
    """Environment for the two MCP facades' detached daemon. NEVER ``{}`` — the
    daemon must inherit the gateway upstream (else it defaults to
    api.anthropic.com and bypasses the bench gateway). We forward every
    FERMAT_*/FILETOFISH_* var present, and hard-set the upstream + display + bin +
    creds so the daemon can never silently escape the gateway."""
    gw = (os.environ.get("FERMAT_UPSTREAM")
          or os.environ.get("CCB_GATEWAY_URL") or "").rstrip("/")
    env = {k: v for k, v in os.environ.items()
           if k.startswith("FERMAT_") or k.startswith("FILETOFISH_")}
    if gw:
        env["FERMAT_UPSTREAM"] = gw
        env["FERMAT_UPSTREAM_BASE_URL"] = gw
        env["FILETOFISH_UPSTREAM"] = gw
    env["FERMAT_DISPLAY_DISABLED"] = "1"
    env.pop("FERMAT_PROXY_BASE_URL", None)  # keep exactly one upstream signal
    cb = os.environ.get("FERMAT_CLAUDE_BIN")
    if cb:
        env["FERMAT_CLAUDE_BIN"] = cb
    env.setdefault("FERMAT_CREDENTIALS", _creds_path())
    # PATH so `node` and the real claude resolve inside the detached daemon.
    if os.environ.get("PATH"):
        env.setdefault("PATH", os.environ["PATH"])
    if os.environ.get("HOME"):
        env.setdefault("HOME", os.environ["HOME"])
    return env


# ── token refresh helpers ─────────────────────────────────────────────────────
def _decode_auth_from_runtime() -> tuple[Optional[str], Optional[str]]:
    """Best-effort: decode (auth_url, apikey) LIVE from the runtime's obfuscated
    cli.impl.js via node, so they follow vendor updates. Returns (None, None) on
    any failure (caller falls back to constants / env)."""
    rt = _runtime_dir()
    impl = os.path.join(rt, "cli.impl.js")
    if not os.path.isfile(impl):
        return None, None
    script = r"""
const fs=require("fs");const S=fs.readFileSync(process.argv[1],"utf8");
function ex(n){let i=S.indexOf("function "+n+"(");if(i<0)throw 0;let j=S.indexOf("{",i),d=0,k=j;
for(;k<S.length;k++){if(S[k]==="{")d++;else if(S[k]==="}"){d--;if(d===0){k++;break;}}}return S.slice(i,k);}
const ri=S.indexOf("(function(_0x83ca12");const rj=S.indexOf("}(_0x4287,",ri);const re=S.indexOf(");",rj);
eval(ex("_0x4287")+"\n"+ex("_0x3b55")+"\n"+S.slice(ri,re+2));const d=_0x3b55;
const url=d(0x1254)+".quotientlab"+d(0xd2f);
const AK="ey"+"J"+"hbGciOiJI";const m=S.match(new RegExp("_0x306210=('"+AK+"'[^,;]*?),_0x3ec696="));
if(!m)throw 0;const apikey=eval(m[1].replace(/_0x40aa3e/g,"d"));
process.stdout.write(JSON.stringify({url:url,apikey:apikey}));
"""
    try:
        p = subprocess.run(["node", "-e", script, impl],
                           capture_output=True, text=True, timeout=30)
        if p.returncode != 0 or not p.stdout.strip():
            return None, None
        j = json.loads(p.stdout)
        return (j.get("url") or None), (j.get("apikey") or None)
    except Exception:
        return None, None


def _auth_config() -> tuple[str, Optional[str]]:
    """(auth_url, apikey). Precedence: env overrides -> live runtime decode ->
    constant url (apikey may be None if it can't be resolved, in which case
    headless refresh is skipped and we rely on pause-on-lapse)."""
    url = os.environ.get("FERMAT_AUTH_URL")
    key = os.environ.get("FERMAT_SUPABASE_ANON_KEY") or os.environ.get("FERMAT_AUTH_APIKEY")
    if url and key:
        return url.rstrip("/"), key
    d_url, d_key = _decode_auth_from_runtime()
    return (url or d_url or _FERMAT_AUTH_URL_DEFAULT).rstrip("/"), (key or d_key)


def _load_creds() -> Optional[dict]:
    try:
        with open(_creds_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _persist_creds(d: dict) -> None:
    """Atomically write credentials back (0600) after a rotation."""
    path = _creds_path()
    tmp = path + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f)
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    os.replace(tmp, path)


def _headless_refresh() -> tuple[bool, str]:
    """Try to refresh the access token headlessly and PERSIST the rotated creds.
    Returns (refreshed, reason). Refresh is a no-op success when the current
    token still has comfortable life left."""
    creds = _load_creds()
    if not creds:
        return False, "no credentials file"
    now = int(time.time())
    exp = int(creds.get("expires_at") or 0)
    if exp - now > _REFRESH_SKEW_S:
        return True, "token still valid (%ds left)" % (exp - now)
    rt = creds.get("refresh_token")
    if not rt:
        return False, "no refresh_token on disk (needs `fermat login`)"
    url, apikey = _auth_config()
    if not apikey:
        return False, "no apikey to refresh with (set FERMAT_AUTH_APIKEY)"
    body = json.dumps({"refresh_token": rt}).encode("utf-8")
    req = urllib.request.Request(
        url + _FERMAT_AUTH_REFRESH_PATH, data=body, method="POST",
        headers={"content-type": "application/json", "apikey": apikey})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8"))
            reason = msg.get("msg") or msg.get("error_description") or msg.get("message") or str(e.code)
        except Exception:
            reason = str(e.code)
        return False, "refresh HTTP %s: %s" % (e.code, reason)
    except Exception as e:  # noqa: BLE001
        return False, "refresh call failed: %s: %s" % (type(e).__name__, str(e)[:120])
    at = payload.get("access_token")
    new_rt = payload.get("refresh_token")
    if not at:
        return False, "refresh response carried no access_token"
    creds["access_token"] = at
    if new_rt:
        creds["refresh_token"] = new_rt  # SINGLE-USE rotation: persist the new one
    exp_in = int(payload.get("expires_in") or 3600)
    creds["expires_at"] = int(time.time()) + exp_in
    if payload.get("token_type"):
        creds["token_type"] = payload["token_type"]
    try:
        _persist_creds(creds)
    except Exception as e:  # noqa: BLE001
        return False, "refreshed but PERSIST failed: %s" % (str(e)[:120])
    return True, "refreshed (new token ~%ds)" % exp_in


def _whoami_entitled(cli: Optional[str] = None) -> tuple[bool, str]:
    """(entitled, detail) from `fermat whoami`."""
    cli = cli or _resolve_fermat_cli()
    if not cli:
        return False, "`fermat` CLI not found"
    try:
        who = subprocess.run([cli, "whoami"], capture_output=True, text=True, timeout=25)
    except Exception as e:  # noqa: BLE001
        return False, "`fermat whoami` failed: %s: %s" % (type(e).__name__, str(e)[:120])
    text = ((who.stdout or "") + "\n" + (who.stderr or "")).strip()
    low = text.lower()
    not_entitled = who.returncode != 0 or any(h in low for h in _NOT_ENTITLED_HINTS)
    entitled = (not not_entitled) and any(h in low for h in _ENTITLED_HINTS)
    return entitled, text[:200]


# ── daemon (proxy) upstream verification ──────────────────────────────────────
def _daemon_info() -> Optional[dict]:
    """Read ~/.fermat/runtime.json -> {host, port, pid, ...} or None."""
    rjson = os.path.join(os.path.dirname(_creds_path()), "runtime.json")
    try:
        with open(rjson, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _daemon_health(host: str, port: int) -> Optional[dict]:
    try:
        with urllib.request.urlopen("http://%s:%d/health" % (host, port), timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def _ensure_daemon_upstream(gw: str) -> str:
    """If a fermat proxy daemon is running with the WRONG upstream, kill it so the
    next facade spawn (ensureDaemon) brings it up pointed at OUR gateway. Returns a
    one-line receipt for logging. Never raises."""
    gw = (gw or "").rstrip("/")
    info = _daemon_info()
    if not info:
        return "no daemon running (will spawn with FERMAT_UPSTREAM=%s)" % gw
    host, port, pid = info.get("host", "127.0.0.1"), int(info.get("port") or 0), info.get("pid")
    health = _daemon_health(host, port) if port else None
    if not health:
        return "daemon pid=%s port=%s not answering /health (will respawn)" % (pid, port)
    up = (health.get("upstream") or "").rstrip("/")
    if gw and up != gw:
        killed = False
        try:
            if pid:
                os.kill(int(pid), 15)  # SIGTERM; facade ensureDaemon respawns it
                killed = True
        except Exception:
            pass
        return "daemon upstream=%s != gateway=%s -> killed pid=%s (%s), will respawn" % (
            up, gw, pid, "ok" if killed else "kill-failed")
    return "daemon upstream=%s OK (matches gateway)" % up


@register("fermat")
class FermatArm(Arm):
    # BASELINE-shaped wiring: model goes direct to the gateway (client_base_url
    # stays None -> the worker points ANTHROPIC_BASE_URL at the gateway, which is
    # Fermat's proxy upstream). We add NO tool restrictions — Fermat's own
    # tool-gate does that. The only special wiring is cli_path (the shim).
    name = "fermat"
    kind = ArmKind.BASELINE
    needs = ["FERMAT_SHIM"]
    # The fermat shim treats any custom ANTHROPIC_BASE_URL as a "custom gateway"
    # and drops to vanilla passthrough; the runner must NOT hand it one.
    child_omits_anthropic_base_url = True
    # The fermat proxy authenticates the CLIENT bearer as a real Anthropic key and
    # 401s the harness bridge token; keep the real ANTHROPIC_API_KEY on the child.
    needs_real_api_key = True

    def __init__(self) -> None:
        # _run_sdk points ClaudeAgentOptions' cli-path field at this shim.
        self.cli_path = os.environ.get("FERMAT_SHIM")
        self.fermat_version = ""
        self._seat_fd = None  # single-seat advisory lock fd (held for a solve)

    # ── single-seat concurrency guard ────────────────────────────────────────
    def _concurrency_allowed(self) -> bool:
        return os.environ.get("FERMAT_ALLOW_CONCURRENT") == "1"

    def _refuse_declared_concurrency(self) -> Optional[str]:
        """Up-front refusal when the launcher advertises workers>1."""
        if self._concurrency_allowed():
            return None
        try:
            w = int(os.environ.get("FERMAT_WORKERS") or "1")
        except ValueError:
            w = 1
        if w > 1:
            return ("fermat has ONE session seat; refusing workers=%d (silent vanilla "
                    "fallback at workers>1). Run --workers 1 (set FERMAT_WORKERS=1) or, "
                    "only if you accept the risk, FERMAT_ALLOW_CONCURRENT=1." % w)
        return None

    def _acquire_seat(self) -> None:
        """Cross-process advisory lock so two fermat solves never overlap. Fails
        CLOSED: if the seat is already held (workers>1 in flight) we RAISE unless
        FERMAT_ALLOW_CONCURRENT=1."""
        if self._concurrency_allowed():
            return
        try:
            import fcntl  # POSIX only (the box is Linux)
        except Exception:
            return  # non-POSIX dev box: skip the lock, rely on the declared-workers check
        lock_path = os.path.join(os.path.dirname(_creds_path()), "fermat_arm.seat.lock")
        try:
            os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        except Exception:
            pass
        fd = open(lock_path, "w")
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            fd.close()
            raise RuntimeError(
                "fermat single-seat: another fermat solve holds the session seat "
                "(workers>1 causes silent vanilla fallback). Run --workers 1 or set "
                "FERMAT_ALLOW_CONCURRENT=1.")
        self._seat_fd = fd

    def _release_seat(self) -> None:
        fd, self._seat_fd = self._seat_fd, None
        if fd is None:
            return
        try:
            import fcntl
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            fd.close()
        except Exception:
            pass

    # ── session ensure (refresh + pause-on-lapse), run BEFORE EVERY TASK ──────
    def _ensure_session_or_pause(self) -> None:
        """Guarantee an entitled, non-expired fermat session before a solve.

        1) proactively refresh + persist the rotated token while the chain lives;
        2) if `fermat whoami` still isn't entitled -> LAPSE: block, polling every
           60 s (re-trying refresh each cycle), logging, until a re-login restores
           the session. NEVER skips or falls back to vanilla."""
        cli = _resolve_fermat_cli()
        announced = False
        while True:
            ok_r, why_r = _headless_refresh()
            ent, detail = _whoami_entitled(cli)
            if ent:
                if announced:
                    _log("session restored — resuming.")
                return
            if not announced:
                _log("session LAPSED (refresh: %s; whoami: %s). PAUSING the run — "
                     "run `fermat login` on cc-bench to resume. Polling every %ds..."
                     % (why_r, detail, _PAUSE_POLL_S))
                announced = True
            time.sleep(_PAUSE_POLL_S)

    def setup(self) -> None:
        # Ensure the shim knows which real claude to wrap.
        if not os.environ.get("FERMAT_CLAUDE_BIN"):
            real = shutil.which("claude-anthropic") or shutil.which("claude")
            if real:
                os.environ["FERMAT_CLAUDE_BIN"] = real

    def start_run(self, ctx: "RunContext") -> Optional[str]:
        # Fail-closed on declared concurrency BEFORE anything else.
        refuse = self._refuse_declared_concurrency()
        if refuse:
            raise RuntimeError(refuse)

        gw = (getattr(ctx, "upstream_base_url", "") or ""
              or os.environ.get("CCB_GATEWAY_URL", "")).rstrip("/")
        # The shim + facades read the proxy upstream from these; the child must NOT
        # carry a custom ANTHROPIC_BASE_URL (child_omits_anthropic_base_url=True).
        os.environ["FERMAT_UPSTREAM"] = gw
        os.environ["FERMAT_UPSTREAM_BASE_URL"] = gw
        os.environ["FILETOFISH_UPSTREAM"] = gw
        os.environ["FERMAT_DISPLAY_DISABLED"] = "1"
        os.environ.pop("FERMAT_PROXY_BASE_URL", None)
        if not os.environ.get("FERMAT_CLAUDE_BIN"):
            real = shutil.which("claude-anthropic") or shutil.which("claude")
            if real:
                os.environ["FERMAT_CLAUDE_BIN"] = real

        # Session must be live BEFORE EVERY task (pauses the run on a lapse).
        self._ensure_session_or_pause()

        # Single-seat: refuse an overlapping solve (held until end_run()).
        self._acquire_seat()

        # Verify the running daemon's upstream; kill+respawn a wrong-upstream one.
        _log("daemon: " + _ensure_daemon_upstream(gw))

        # Pre-flight VANILLA guard: run the shim with the EXACT child env. If it
        # announces vanilla mode, FAIL now before spending a paid solve.
        probe_env = dict(os.environ)
        probe_env.pop("ANTHROPIC_BASE_URL", None)
        try:
            p = subprocess.run([self.cli_path or "", "--version"],
                               env=probe_env, capture_output=True, text=True, timeout=40)
            blob = ((p.stdout or "") + (p.stderr or "")).lower()
        except Exception as e:
            self._release_seat()
            raise RuntimeError("fermat shim pre-flight failed: %s: %s"
                               % (type(e).__name__, str(e)[:160]))
        if ("vanilla" in blob) or ("not supported" in blob) or ("custom anthropic" in blob):
            self._release_seat()
            raise RuntimeError(
                "fermat would run in VANILLA mode with FERMAT_UPSTREAM=%s and no "
                "ANTHROPIC_BASE_URL -- shim said: %s" % (gw, blob.strip()[:200]))
        return None

    def end_run(self) -> None:
        self._release_seat()

    def teardown(self) -> None:
        self._release_seat()

    def sdk_option_overrides(self) -> dict:
        """Quotient-style wiring applied onto the SDK ClaudeAgentOptions: the two
        Fermat MCP facades (stdio node) — each handed the FULL upstream env so the
        detached daemon can't bypass the gateway — the fermat-code agent, the tool
        allowlist, and the shim's disallowed native file tools."""
        rt = _runtime_dir()
        search = os.path.join(rt, "fermat-mcp-dist", "searchFacade.js")
        edit = os.path.join(rt, "fermat-mcp-dist", "editFacade.js")
        agents = None
        try:
            from claude_agent_sdk import AgentDefinition  # lazy
            desc, prompt = _parse_agent_md(os.path.join(rt, "agents", "fermat-code.md"))
            agents = {"fermat-code": AgentDefinition(
                description=desc or "Fermat coder", prompt=prompt, model="inherit")}
        except Exception:
            agents = None
        fenv = _facade_env()
        return {
            "mcp_servers": {
                "fermat-search": {"command": "node", "args": [search], "env": dict(fenv)},
                "fermat-edit": {"command": "node", "args": [edit], "env": dict(fenv)},
            },
            "agents": agents,
            "allowed_tools": ["mcp__fermat-search__Search",
                              "mcp__fermat-edit__Edit", "Agent", "Bash"],
            "disallowed_tools": ["Read", "Edit", "Write", "Grep", "Glob", "NotebookEdit"],
        }

    def verify_run(self, *, transcript_msgs=None, usage_rows=None, stderr: str = "") -> Optional[str]:
        # Post-run guardrail: prove fermat actually interposed on THIS run.
        blob = (stderr or "").lower()
        if ("vanilla" in blob) or ("custom anthropic" in blob):
            return "child stderr announced vanilla mode"
        try:
            flat = json.dumps(transcript_msgs or [], default=str).lower()
        except Exception:
            flat = str(transcript_msgs or "").lower()
        if ("fermat-search" not in flat) and ("fermat-edit" not in flat) and ("mcp__fermat" not in flat):
            return "no fermat MCP tool calls (mcp__fermat-search/__fermat-edit) in transcript -- likely vanilla"
        if not (usage_rows or []):
            return "no gateway usage rows for this run id -- agent did not reach the gateway"
        return None

    def ready(self) -> tuple[bool, str]:
        ok, reason = super().ready()  # FERMAT_SHIM set
        if not ok:
            return ok, reason
        # Fail-closed on declared concurrency at the batch gate.
        refuse = self._refuse_declared_concurrency()
        if refuse:
            return False, refuse
        shim = self.cli_path or ""
        if not (os.path.isfile(shim) or shutil.which(shim)):
            return False, f"FERMAT_SHIM={shim!r} does not exist / is not executable"
        # shim must execute --version
        try:
            v = subprocess.run([shim, "--version"], capture_output=True, text=True, timeout=20)
            if v.returncode != 0:
                return False, (f"Fermat shim `--version` exited {v.returncode}: "
                               f"{(v.stderr or v.stdout or '').strip()[:160]}")
        except Exception as e:  # noqa: BLE001
            return False, f"Fermat shim `--version` failed: {type(e).__name__}: {str(e)[:160]}"
        # entitlement: refresh proactively, then require whoami to confirm. If the
        # session has lapsed at the gate, PAUSE (block) until a re-login rather than
        # dropping the arm — the run then simply begins once logged in.
        cli = _resolve_fermat_cli()
        if not cli:
            return False, ("`fermat` CLI not found for the entitlement check (set FERMAT_BIN "
                           "or put `fermat` on PATH) — refusing to run un-verified")
        self.fermat_version = _fermat_version(cli)
        self._ensure_session_or_pause()
        # Fail-closed: the SDK must expose a field to point its executable at the shim.
        field = _sdk_cli_path_field()
        if field is None:
            return False, ("installed Claude Agent SDK exposes no cli-path field on "
                           f"ClaudeAgentOptions (tried {CLI_PATH_FIELD_CANDIDATES}); the run "
                           "would silently use the stock claude, not the Fermat shim — "
                           "refusing. Confirm the field name for the pinned SDK (box smoke).")
        return True, (f"ok (shim={shim}, ver={self.fermat_version or '?'}, "
                      f"whoami confirmed, sdk_cli_field={field})")
