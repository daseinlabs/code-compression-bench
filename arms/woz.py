"""Woz arm — a ToolArm wrapping Woz's REAL Claude Code MCP server.

WHAT WOZ IS (and why it's a ToolArm, not a proxy or transform)
--------------------------------------------------------------
Woz (WOZCODE) is a *paid* Claude Code plugin that ships an MCP (Model Context
Protocol) server (``servers/code-server.js`` in github.com/WithWoz/wozcode-plugin,
reported as MCP server ``code`` v0.3.82). It does NOT compress the model's prompt
stream and it does NOT sit on the model endpoint. Instead it changes the agent's
TOOLS: it replaces the generic shell-and-grep surface with a smaller, sharper set.

The real tool set (capitalized) is ``Search``, ``Edit``, ``Recall``, ``Sql`` —
captured empirically from the live server's own ``tools/list`` (see
meta_learning/WOZ_SEARCH_ANALYSIS.md). What each one actually is:

  * ``Search`` — a COMBINED regex-grep + glob + file-read tool. It runs a
    TypeScript regex (``content_regex``) over file CONTENTS, selects files by
    glob (``file_glob_patterns``), and returns matching lines with explicit
    OUTPUT-SIZE knobs:
      - ``output_mode``: ``file_paths_only`` | ``file_paths_with_match_count`` |
        ``file_paths_with_content`` (default) — escalate from cheap to detailed.
      - ``lines_before`` / ``lines_after``: context lines around each match.
      - ``lines_per_file``: max matching lines per file (default 500; 0 =
        unlimited).
      - ``max_line_length``: per-line char cap (default 1000; 0 = unlimited).
      - ``file_limit``: process only the first N matching files.
      - ``type``: file-type filter (e.g. ``ts``, ``py``, ``sql``).
      - ``ignore_case`` / ``multiline``: case-insensitive; ``.`` spans newlines.
      - ``if_modified_since``: an ISO timestamp (the "Results as of" header from a
        prior Search) → INCREMENTAL re-search: only files modified since.
      - ``summary``: TS/JS only — return signatures/structure (code SKELETONS)
        for many files cheaply.
    It is NOT semantic, NOT embedding-based, and NOT relevance-ranked: results
    come back in filesystem/discovery order (it's grep, not a learned score).
    There is NO upfront repo vector index of any kind.

  * ``Recall`` — semantic search over PAST CLAUDE CODE SESSIONS (cross-session
    memory: "commands, solutions, explanations, and context from previous
    conversations", TurboQuant-compressed). This is the ONLY embedding search
    Woz ships, and it searches prior conversations, NOT the codebase.

  * ``Edit`` — a single ``edits`` JSON array applied in ONE call: many fuzzy
    search/replace edits, across many files, batched together.

  * ``Sql`` — a schema/query tool (tables/functions/enums/query/... against a
    live DB).

WHERE WOZ'S TOKEN SAVINGS ACTUALLY COME FROM
--------------------------------------------
NOT prompt compression, NOT a vector index, NOT a model proxy. The lever is the
TOOL SURFACE itself:
  - CONSOLIDATION — one ``Search`` call discovers + greps + reads in a single
    round-trip; one ``Edit`` call applies many edits. Fewer tool calls → fewer
    assistant/observation turns accreting in the transcript.
  - OUTPUT SHAPING — ``output_mode`` escalation, ``lines_before/after``,
    ``lines_per_file`` / ``max_line_length`` caps, ``file_limit`` → narrow,
    bounded observations instead of whole-file dumps.
  - INCREMENTAL re-search — ``if_modified_since`` returns only files changed
    since the last Search, so re-greps after edits don't re-dump unchanged code.
  - SKELETONS — ``summary`` returns signatures/structure cheaply (TS/JS).
The agent is *steered* to small, targeted reads, so the persistent window stays
small — the indirect "sharper tools → smaller transcript" effect, confirmed at
the schema level. That IS the ToolArm contract: we return the MCP server command
to spawn (+ ``replace_tools``) and let the runner spawn it, discover its REAL
tools via ``tools/list``, and wire them into the one fixed scaffold; the model
endpoint is untouched.

CLEAN-ROOM
----------
This module contains NO proprietary logic and imports NOTHING from
``adaptive_context``. It only *names* the MCP server command to launch, the env
it needs, and the one-time login that authenticates it. The actual
Search/Edit/Recall/Sql implementations live inside the Woz plugin on the other
side of the MCP stdio pipe — we neither import nor reimplement them, and we do
NOT hand-mirror their schemas: the runner discovers the live tool surface from
the server's own ``tools/list`` at run time.

AUTH (a CLI login, NOT a server env var)
----------------------------------------
Forwarding ``WOZ_API_KEY`` to the server as an env var does NOT authenticate it —
that path yields ``auth.login_required``. The REAL flow is a one-time CLI login
that stores a session under ``~/.claude/wozcode/``; the MCP server then serves
``Search`` against that STORED session:

    <node> <WOZ_PLUGIN_DIR>/scripts/wozcode-cli.js login --token "$WOZ_API_KEY"
        (with CLAUDE_PLUGIN_ROOT=<WOZ_PLUGIN_DIR> and node on PATH)

``WozArm.setup()`` runs this ONCE before the server is used (idempotent: a fresh
login simply refreshes the stored creds). The plugin's ``.mcp.json`` server env
(cwd hook + telemetry) is still passed to the spawned server — but the API key
authenticates via the login above, not via the server's environment.

NODE >= 20.12 REQUIRED
----------------------
The bundled server imports ``util.styleText`` (added in Node 20.12). Node 20.11
crashes on import with::

    SyntaxError: The requested module 'node:util' does not provide an export
    named 'styleText'

``ready()`` checks ``node --version`` (when node resolves) and SKIPs with a
precise reason if older. Override the node binary with ``WOZ_NODE`` (woz_probe
ships a portable node22 for exactly this).

*** MAKE-OR-BREAK — UNRESOLVED, MUST BE SETTLED AT SMOKE *********************
Woz's MCP server runs on the HOST runner, but the swebench task repo lives inside
the per-task DOCKER CONTAINER. So ``Search`` (which greps the filesystem at its
``cwd``) will NOT see the task repo unless one of these is arranged at smoke on
the Linux box:
  (a) Woz runs INSIDE the container (node + the plugin baked into the task
      image), so the server's cwd IS the repo; or
  (b) the task repo is copied/mounted to a HOST path that is passed to Search as
      ``cwd`` (see WOZ_SEARCH_CWD in the runner's MCP dispatch).
Until that is resolved, a host-spawned Woz server will grep the runner's working
dir, not the task repo — i.e. it will find nothing relevant. DO NOT assume this
works; it is an OPEN ITEM for smoke. (This same warning is repeated in
bench/runner.py at the cwd-injection seam.)
****************************************************************************

Env:
  WOZ_API_KEY      — Woz account/license key (the website ``{refreshToken,
                     organizationId}`` token). Used by ``setup()`` to perform the
                     one-time CLI login (NOT forwarded as the server's auth — that
                     does not work). Passed to the login subprocess via env, never
                     argv, so it can't leak into process listings.
  WOZ_PLUGIN_DIR   — path to a clone of github.com/WithWoz/wozcode-plugin on the
                     runner box (the ``${CLAUDE_PLUGIN_ROOT}``). The server file is
                     ``<WOZ_PLUGIN_DIR>/servers/code-server.js`` and the login CLI
                     is ``<WOZ_PLUGIN_DIR>/scripts/wozcode-cli.js``.
  WOZ_NODE         — (optional) a pinned node binary (>= 20.12) used for both the
                     login and the server, e.g. woz_probe's portable node22. If
                     unset, ``node`` from PATH is used (and version-checked).
  WOZ_MCP_CMD      — (optional) operator override: a full shell-style command that
                     launches the MCP stdio server, in case the plugin layout
                     differs. If set it wins over the WOZ_PLUGIN_DIR default.
  WOZ_SEARCH_CWD   — (read by the runner, not here) the path Woz's Search should
                     grep. See bench/runner.py's MCP dispatch. Documented here so
                     the whole Woz wiring is described in one place.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

from bench.arm import ToolArm, ToolAttach, register


# ── env the plugin's .mcp.json sets on the server process ────────────────────
# Reproduced verbatim from github.com/WithWoz/wozcode-plugin/.mcp.json so the
# server behaves identically to a real Claude Code launch (the cwd hook + the
# vendor's PostHog telemetry config). NOTE: the API key does NOT go here — the
# server authenticates from the stored login session (see WozArm.setup()), not
# from its environment. Forwarding the key here yields auth.login_required.
_WOZ_SERVER_ENV: dict[str, str] = {
    "WOZCODE_MCP_CWD_HOOK_INJECTED": "1",
    "WOZCODE_POSTHOG_ENABLED": "true",
    "WOZCODE_POSTHOG_PROJECT_TOKEN": "phc_F3mo2emdspgzD4QmFMQxHQfab1TyXgCAU7eYBakKq9k",
    "WOZCODE_POSTHOG_PROJECT_REGION": "us",
}

# node flag the plugin uses (suppress the ExperimentalWarning noise on stdout).
_NODE_FLAGS = ["--no-warnings=ExperimentalWarning"]

# Minimum Node the bundled server needs (it imports util.styleText, added in
# Node 20.12; 20.11 crashes on import). ready() enforces this when node resolves.
_MIN_NODE = (20, 12)


def _node_exe() -> str:
    """The node binary to launch. Honor WOZ_NODE for a pinned node, else 'node'."""
    return os.environ.get("WOZ_NODE", "node")


def _server_js() -> str | None:
    """Path to ``servers/code-server.js`` under WOZ_PLUGIN_DIR, or None if unset."""
    plugin_dir = os.environ.get("WOZ_PLUGIN_DIR")
    if not plugin_dir:
        return None
    return os.path.join(plugin_dir, "servers", "code-server.js")


def _login_cli() -> str | None:
    """Path to ``scripts/wozcode-cli.js`` under WOZ_PLUGIN_DIR, or None if unset."""
    plugin_dir = os.environ.get("WOZ_PLUGIN_DIR")
    if not plugin_dir:
        return None
    return os.path.join(plugin_dir, "scripts", "wozcode-cli.js")


def _node_version(exe: str) -> tuple[int, int, int] | None:
    """Return the (major, minor, patch) of ``exe`` via ``node --version``, or None
    if node can't be run / its output can't be parsed (then we don't gate on it)."""
    try:
        out = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, timeout=10
        )
    except Exception:
        return None
    m = re.search(r"v?(\d+)\.(\d+)\.(\d+)", (out.stdout or "") + (out.stderr or ""))
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


class WozLoginError(RuntimeError):
    """The one-time Woz CLI login failed (bad key, node error, missing CLI)."""


@register("woz")
class WozArm(ToolArm):
    """Attach Woz's REAL Claude Code MCP server as the agent's tool layer.

    Woz changes the agent's TOOLS (Search/Edit/Recall/Sql) rather than
    compressing the prompt stream or proxying the model. The benefit is indirect:
    a consolidated grep+read tool with output-size knobs steers the agent to
    short, bounded tool calls → a smaller transcript. ToolArm is the right
    pattern.

    Requires a Woz account key (WOZ_API_KEY), a resolvable node >= 20.12, and a
    clone of the plugin (WOZ_PLUGIN_DIR) whose ``servers/code-server.js`` and
    ``scripts/wozcode-cli.js`` exist. ``setup()`` performs the one-time CLI login
    that the MCP server then authenticates against.
    """

    name = "woz"
    needs = ["WOZ_API_KEY"]

    def ready(self) -> tuple[bool, str]:
        """Ready iff WOZ_API_KEY is set AND the MCP server is actually runnable:
        a resolvable node (>= 20.12) + a real server entrypoint on disk. So the
        runner skips this arm cleanly (with a precise reason) when Woz isn't
        installed / the node is too old, rather than crashing mid-run."""
        ok, reason = super().ready()  # checks WOZ_API_KEY is set & non-empty
        if not ok:
            return ok, reason
        cmd = self._mcp_server_cmd()
        if cmd is None:
            return False, (
                "Woz MCP server not configured. Set WOZ_PLUGIN_DIR to a clone of "
                "github.com/WithWoz/wozcode-plugin (so "
                "<WOZ_PLUGIN_DIR>/servers/code-server.js exists), or set WOZ_MCP_CMD "
                "to the launch argv directly."
            )
        exe = cmd[0]
        if shutil.which(exe) is None and not os.path.exists(exe):
            return False, (
                f"node launcher not found on PATH or disk: {exe!r} "
                f"(install Node.js >= 20.12 on the runner box, or set WOZ_NODE)."
            )
        # Node >= 20.12 gate: the bundled server imports util.styleText (Node
        # 20.12+); 20.11 crashes on import. Only gate when we can actually read a
        # version (a resolvable node); if --version can't be parsed we don't block.
        ver = _node_version(exe)
        if ver is not None and ver[:2] < _MIN_NODE:
            return False, (
                f"node {ver[0]}.{ver[1]}.{ver[2]} is too old for the Woz server "
                f"(needs >= {_MIN_NODE[0]}.{_MIN_NODE[1]}: it imports util.styleText, "
                f"added in Node 20.12; older node crashes on import with "
                f"\"'node:util' does not provide an export named 'styleText'\"). "
                f"Set WOZ_NODE to a pinned node >= 20.12 (woz_probe ships a portable "
                f"node22)."
            )
        # If we're using the WOZ_PLUGIN_DIR default (not a raw WOZ_MCP_CMD), the
        # server file must exist — check it so a typo'd dir SKIPs with a reason.
        if not os.environ.get("WOZ_MCP_CMD"):
            js = _server_js()
            if js is None:
                return False, "WOZ_PLUGIN_DIR is not set (no plugin clone to launch)."
            if not os.path.exists(js):
                return False, (
                    f"Woz server entrypoint missing: {js!r}. Clone the plugin: "
                    f"git clone https://github.com/WithWoz/wozcode-plugin into WOZ_PLUGIN_DIR."
                )
        return True, "ok"

    def setup(self) -> None:
        """One-time CLI LOGIN so the MCP server has a session to authenticate
        against. This is the auth flow — NOT forwarding the key as a server env
        var (that yields ``auth.login_required``).

        Runs::

            <node> <WOZ_PLUGIN_DIR>/scripts/wozcode-cli.js login --token "$WOZ_API_KEY"

        with ``CLAUDE_PLUGIN_ROOT=<WOZ_PLUGIN_DIR>`` and node on PATH. The key
        travels via the subprocess ENV (never argv) so it can't leak into process
        listings. Idempotent: a fresh login simply refreshes the creds stored
        under ``~/.claude/wozcode/``.

        On failure we raise WozLoginError with a clear message (and log it) rather
        than crashing the whole run silently — the runner catches arm-setup errors
        and surfaces them as an infra failure for THIS arm only.
        """
        api_key = os.environ.get("WOZ_API_KEY")
        if not api_key:
            # ready() already gates on this; defensive only.
            raise WozLoginError("WOZ_API_KEY is not set; cannot perform Woz login.")

        # WOZ_MCP_CMD operators own their own auth (custom launch); skip the
        # default CLI login so we don't assume a plugin layout that may not exist.
        if os.environ.get("WOZ_MCP_CMD"):
            return

        cli = _login_cli()
        if cli is None:
            raise WozLoginError(
                "WOZ_PLUGIN_DIR is not set; cannot locate scripts/wozcode-cli.js "
                "to perform the Woz login."
            )
        if not os.path.exists(cli):
            raise WozLoginError(
                f"Woz login CLI missing: {cli!r}. Clone the plugin "
                f"(github.com/WithWoz/wozcode-plugin) into WOZ_PLUGIN_DIR."
            )

        node = _node_exe()
        # Pass the key via env (never argv). CLAUDE_PLUGIN_ROOT mirrors the plugin
        # root the CLI expects; ensure node's own dir is on PATH when WOZ_NODE is a
        # pinned binary outside PATH (the CLI may shell out to node).
        env = dict(os.environ)
        env["CLAUDE_PLUGIN_ROOT"] = os.environ.get("WOZ_PLUGIN_DIR", "")
        node_dir = os.path.dirname(node)
        if node_dir and os.path.isdir(node_dir):
            env["PATH"] = node_dir + os.pathsep + env.get("PATH", "")

        argv = [node, *_NODE_FLAGS, cli, "login", "--token", api_key]
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=120, env=env
            )
        except Exception as e:  # noqa: BLE001 — surface a clear, non-secret error
            raise WozLoginError(
                f"Woz login subprocess failed to run ({type(e).__name__}: {e}). "
                f"Check node ({node!r}) and the plugin CLI ({cli!r})."
            ) from e
        if proc.returncode != 0:
            # NEVER echo the key. Surface stdout/stderr tails (CLI prints the
            # authenticated identity on success / the reason on failure).
            tail = ((proc.stderr or "") + (proc.stdout or "")).strip()[-400:]
            raise WozLoginError(
                f"Woz login failed (exit {proc.returncode}). Check WOZ_API_KEY is a "
                f"valid Woz account key. CLI output tail: {tail!r}"
            )
        # Success: the CLI prints "Authenticated as <email>"; creds are now stored
        # under ~/.claude/wozcode/ and the MCP server will read them. (Best-effort
        # log; no secret in the output.)
        print(f"  woz: login ok -> {(proc.stdout or '').strip()[-160:]}", flush=True)

    def _mcp_server_cmd(self) -> list[str] | None:
        """Resolve the argv that launches the Woz MCP stdio server.

        Precedence:
          1. WOZ_MCP_CMD (operator override) — split a shell-style string to argv.
          2. WOZ_PLUGIN_DIR default — reproduce the plugin's .mcp.json command:
             ``node --no-warnings=ExperimentalWarning <DIR>/servers/code-server.js``.
          3. Neither configured -> None ("not configured"), so ready() reports it
             cleanly rather than the runner spawning a bogus process.

        WOZ_API_KEY is NEVER placed here — it authenticates via the stored login
        session (see setup()), not via the server command or environment.
        """
        override = os.environ.get("WOZ_MCP_CMD")
        if override:
            return override.split()
        js = _server_js()
        if js is None:
            return None
        return [_node_exe(), *_NODE_FLAGS, js]

    def attach(self) -> ToolAttach:
        """Return the MCP server to spawn + the replace-tools contract.

        The REAL tools (Search/Edit/Recall/Sql) come from the live ``tools/list``
        the runner issues after the handshake — we do NOT hardcode mirrored
        schemas (``tools=[]``). ``replace_tools=True``: Woz REPLACES the scaffold's
        bash tool with its own set; that replacement IS the arm.

        The server env carries ONLY the plugin's .mcp.json config (cwd hook +
        telemetry). The API key is NOT here — the server authenticates from the
        login session ``setup()`` established (forwarding the key as a server env
        var yields ``auth.login_required``).
        """
        return ToolAttach(
            tools=[],  # discovery-driven: no hand-mirrored schemas
            mcp_server_cmd=self._mcp_server_cmd(),
            replace_tools=True,
            server_env=dict(_WOZ_SERVER_ENV),
        )
