"""Woz arm — a ToolArm that loads Woz's REAL Claude Code PLUGIN.

WHAT WOZ IS (and why it's a ToolArm, not a proxy or transform)
--------------------------------------------------------------
Woz (WOZCODE) is a *paid* Claude Code plugin (github.com/WithWoz/wozcode-plugin,
plugin name ``woz`` v0.3.82) that ships: a ``code`` MCP server
(``servers/code-server.js``), its own subagents (``agents/code.md`` main +
``agents/explore.md`` — a **haiku** explorer with Woz's Search/Sql tools and a
terse Defs/Refs/Callers format), session hooks, and skills. It does NOT compress
the model's prompt stream and it does NOT sit on the model endpoint. Instead it
changes the agent's TOOLS: it replaces the generic shell-and-grep surface with a
smaller, sharper set, and delegates exploration to its cheap haiku subagent.

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
small — the indirect "sharper tools → smaller transcript" effect, plus the haiku
``explore`` subagent doing scans off the main thread. That IS the ToolArm
contract under Claude Code: we return the PLUGIN DIRECTORY (+ ``replace_tools``)
and the runner loads the whole plugin via the SDK
``plugins=[{"type":"local","path":WOZ_PLUGIN_DIR}]`` — so Claude Code activates
Woz's OWN code/explore subagents, its ``code`` MCP server (tools as
``mcp__plugin_woz_code__*``), hooks and skills, exactly as shipped. We do NOT
spawn a bare server or reconstruct the explorer (that would measure an
approximation, not Woz). The model endpoint is untouched.

CLEAN-ROOM
----------
This module contains NO proprietary logic and imports NOTHING from
``adaptive_context``. It only *names* the plugin DIRECTORY to load and runs the
one-time login that authenticates it. The actual Search/Edit/Recall/Sql
implementations + the subagents live inside the Woz plugin — we neither import
nor reimplement them, and we do NOT hand-mirror their schemas or redefine their
subagents: loading the plugin makes Claude Code surface the plugin's real tools
(``mcp__plugin_woz_code__*``) and real agents directly.

AUTH (a CLI login, NOT a server env var)
----------------------------------------
Forwarding ``WOZ_API_KEY`` to the server as an env var does NOT authenticate it —
that path yields ``auth.login_required``. The REAL flow is a one-time CLI login
that stores a session under ``~/.claude/wozcode/``; the MCP server then serves
``Search`` against that STORED session:

    <node> <WOZ_PLUGIN_DIR>/scripts/wozcode-cli.js login --token "$WOZ_API_KEY"
        (with CLAUDE_PLUGIN_ROOT=<WOZ_PLUGIN_DIR> and node on PATH)

``WozArm.setup()`` runs this ONCE before the run (idempotent: a fresh login simply
refreshes the stored creds). When the plugin loads, Claude Code applies the
plugin's OWN ``.mcp.json`` server env (cwd hook + telemetry); the API key
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

REPO VISIBILITY (handled by the plugin's cwd hook — verify at smoke)
--------------------------------------------------------------------
We run the agent on the HOST against a host checkout of the task repo at its
``base_commit`` (``AC_REPO_ROOT/<iid>``, provisioned by ``bench.prepare_repos``);
the official Docker grader applies the produced patch separately, so the agent
does NOT need to run inside the task container. The SDK ``cwd`` is that checkout,
and the plugin's own cwd hook (``WOZCODE_MCP_CWD_HOOK_INJECTED`` in its .mcp.json)
points Woz's ``Search``/``Sql`` at it. SMOKE MUST CONFIRM: (1) the plugin's
``code`` MCP server actually starts under the SDK (its .mcp.json declares
``command: node`` and relies on Woz's session hook to wire the entry/cwd), and
(2) ``Search`` greps the task checkout, not the runner's dir. If the server fails
to start, the run yields NO patch (native file tools are disallowed on purpose) —
which surfaces the failure rather than silently measuring the native agent.

Env:
  WOZ_API_KEY      — Woz account/license key (the website ``{refreshToken,
                     organizationId}`` token). Used by ``setup()`` to perform the
                     one-time CLI login (NOT forwarded as the server's auth — that
                     does not work). Passed to the login subprocess via env, never
                     argv, so it can't leak into process listings.
  WOZ_PLUGIN_DIR   — path to a clone of github.com/WithWoz/wozcode-plugin on the
                     runner box. The runner loads this WHOLE DIR as a plugin
                     (``plugins=[{"type":"local","path":WOZ_PLUGIN_DIR}]``); the
                     login CLI is ``<WOZ_PLUGIN_DIR>/scripts/wozcode-cli.js``.
  WOZ_NODE         — (optional) a pinned node binary (>= 20.12) used for the login,
                     e.g. woz_probe's portable node22. If unset, ``node`` from PATH
                     is used (and version-checked). Claude Code spawns the plugin's
                     MCP server with the system ``node``, so that must be >= 20.12.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

from bench.arm import ToolArm, ToolAttach, register


# NB: the plugin's own ``.mcp.json`` carries the server env (the cwd hook +
# PostHog telemetry) — when we load the whole plugin, Claude Code applies it. We
# no longer reproduce it here (it would be dead config). The API key never goes in
# the server env regardless: the server authenticates from the stored login
# session (see WozArm.setup()); forwarding it yields auth.login_required.

# node flag used for the login subprocess (suppress ExperimentalWarning noise).
_NODE_FLAGS = ["--no-warnings=ExperimentalWarning"]

# Minimum Node the bundled server needs (it imports util.styleText, added in
# Node 20.12; 20.11 crashes on import). ready() enforces this when node resolves.
_MIN_NODE = (20, 12)


def _node_exe() -> str:
    """The node binary to launch. Honor WOZ_NODE for a pinned node, else 'node'."""
    return os.environ.get("WOZ_NODE", "node")


def _plugin_dir() -> str | None:
    """The Woz plugin root (WOZ_PLUGIN_DIR), or None if unset. This is what the
    runner loads via the SDK ``plugins=[{"type":"local","path":...}]`` so Claude
    Code activates Woz's OWN code/explore subagents, MCP server, hooks and skills."""
    d = os.environ.get("WOZ_PLUGIN_DIR")
    return d or None


def _plugin_manifest() -> str | None:
    """Path to ``.claude-plugin/plugin.json`` (the plugin marker), or None."""
    d = _plugin_dir()
    return os.path.join(d, ".claude-plugin", "plugin.json") if d else None


def _explore_agent_md() -> str | None:
    """Path to ``agents/explore.md`` — Woz's REAL haiku explorer (the thing we
    must run, not approximate), or None if WOZ_PLUGIN_DIR is unset."""
    d = _plugin_dir()
    return os.path.join(d, "agents", "explore.md") if d else None


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
    """Load Woz's REAL Claude Code plugin as the agent's tool layer.

    Woz changes the agent's TOOLS (Search/Edit/Recall/Sql) + delegates exploration
    to its haiku ``explore`` subagent, rather than compressing the prompt stream or
    proxying the model. The benefit is indirect: a consolidated grep+read tool with
    output-size knobs + a cheap explorer steer the agent to short, bounded calls →
    a smaller transcript. ToolArm is the right pattern; the runner loads the whole
    plugin (cfg.plugins) so the shipped subagents/MCP/hooks/skills run as-is.

    Requires a Woz account key (WOZ_API_KEY), a resolvable node >= 20.12, and a
    clone of the plugin (WOZ_PLUGIN_DIR) with ``.claude-plugin/plugin.json`` +
    ``agents/explore.md``. ``setup()`` performs the one-time CLI login that the
    plugin's MCP server then authenticates against.
    """

    name = "woz"
    needs = ["WOZ_API_KEY"]

    def ready(self) -> tuple[bool, str]:
        """Ready iff WOZ_API_KEY is set AND the REAL plugin is present on disk
        (``.claude-plugin/plugin.json`` + ``agents/explore.md``) AND node >= 20.12
        resolves. The runner loads the whole plugin via the SDK, so what must exist
        is the plugin tree (its subagents/MCP/hooks), not a server we spawn. Skips
        cleanly with a precise reason when Woz isn't installed / node is too old."""
        ok, reason = super().ready()  # checks WOZ_API_KEY is set & non-empty
        if not ok:
            return ok, reason
        d = _plugin_dir()
        if not d:
            return False, (
                "WOZ_PLUGIN_DIR is not set. Clone the plugin on the runner box: "
                "git clone https://github.com/WithWoz/wozcode-plugin, and point "
                "WOZ_PLUGIN_DIR at it (the runner loads the whole plugin)."
            )
        manifest = _plugin_manifest()
        if not manifest or not os.path.exists(manifest):
            return False, (
                f"Not a Woz plugin dir: {manifest!r} missing. WOZ_PLUGIN_DIR must be a "
                f"clone of github.com/WithWoz/wozcode-plugin (with .claude-plugin/plugin.json)."
            )
        explore = _explore_agent_md()
        if not explore or not os.path.exists(explore):
            return False, (
                f"Woz's real explore subagent missing: {explore!r}. The plugin clone "
                f"is incomplete (agents/explore.md is the haiku explorer we run)."
            )
        # Node >= 20.12 gate: the plugin's MCP server imports util.styleText (Node
        # 20.12+); 20.11 crashes on import. Claude Code spawns the server with the
        # system `node` (or WOZ_NODE). Only gate when we can read a version.
        exe = _node_exe()
        if shutil.which(exe) is None and not os.path.exists(exe):
            return False, (
                f"node not found on PATH or disk: {exe!r} (install Node.js >= 20.12 "
                f"on the runner box, or set WOZ_NODE)."
            )
        ver = _node_version(exe)
        if ver is not None and ver[:2] < _MIN_NODE:
            return False, (
                f"node {ver[0]}.{ver[1]}.{ver[2]} is too old for the Woz plugin server "
                f"(needs >= {_MIN_NODE[0]}.{_MIN_NODE[1]}: it imports util.styleText, "
                f"added in Node 20.12). Set WOZ_NODE to a node >= 20.12."
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

    def attach(self) -> ToolAttach:
        """Load Woz's REAL plugin (not a hand-spawned server).

        The runner loads ``WOZ_PLUGIN_DIR`` via the SDK
        ``plugins=[{"type":"local","path":...}]``, so Claude Code activates Woz's
        OWN definitions as shipped: the ``code`` main agent, the **haiku**
        ``explore`` subagent (its Search/Sql tools + terse Defs/Refs/Callers
        format), the ``code`` MCP server (tools as ``mcp__plugin_woz_code__*``,
        with the plugin's .mcp.json env + cwd hook), its session hooks, and skills.
        We deliberately do NOT spawn a bare server or redefine the explorer — that
        would measure an approximation, not Woz.

        ``replace_tools=True`` makes the runner drop the native file surface on the
        main thread (faithful to Woz's ``agents/code.md``, which disallows
        Read/Edit/Write/Grep/Glob), so the run genuinely works THROUGH Woz's tools.
        The API key authenticates via the login session ``setup()`` established (the
        plugin's MCP server reads it); it is never placed in argv or env here.
        """
        return ToolAttach(
            plugin_dir=_plugin_dir(),
            plugin_tool_globs=["mcp__plugin_woz_code__*"],
            replace_tools=True,
        )
