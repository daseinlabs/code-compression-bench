"""Woz arm — a ToolArm wrapping the Woz Claude Code MCP server.

WHAT WOZ IS (and why it's a ToolArm, not a proxy or transform)
--------------------------------------------------------------
Woz is a *paid* Claude Code plugin that ships an MCP (Model Context Protocol)
server. It does NOT compress the model's prompt stream and it does NOT sit on
the model endpoint. Instead it changes the agent's TOOLS: it replaces the
generic shell-and-grep tool surface with a smaller, sharper set — typically a
semantic Search tool, a structured Edit tool, and a Sql/index query tool —
backed by its own repository index that lives MCP-server-side.

The compression mechanism is therefore *indirect*. By giving the agent
high-signal tools (a ranked semantic search instead of `grep -r`, a structured
edit instead of re-`cat`-ing whole files), Woz steers the agent toward short,
targeted tool calls. Fewer giant `cat`/`grep` dumps land in the transcript, so
the prompt that accrues across turns stays smaller — without ever rewriting a
message or proxying the model. That is exactly the ToolArm contract: we return
the tool specs (+ the MCP server command to spawn) and let the runner wire them
into the one fixed scaffold; the model endpoint is untouched.

CLEAN-ROOM
----------
This module contains NO proprietary logic. It only *describes* the tool surface
(plain OpenAI function-tool dicts) and *names* the MCP server command to launch.
The actual Search/Edit/Sql implementations live inside the Woz plugin on the
other side of the MCP stdio pipe — we neither import nor reimplement them. Woz
is a paid plugin, so the launch command + WOZ_API_KEY wiring are stubbed with
explicit TODOs: a benchmark operator with a Woz license fills in the real
`node code-server.js` invocation and the arm runs unchanged.

Env:
  WOZ_API_KEY  — license/account key for the Woz plugin; passed to the spawned
                 MCP server via its environment (never inlined into argv).
  WOZ_MCP_CMD  — (optional) operator override: the full shell-style command that
                 launches the Woz MCP stdio server. If unset, the arm reports
                 "not configured" via ready() instead of spawning a bogus proc.
"""

from __future__ import annotations

import os
import shutil

from bench.arm import ToolArm, ToolAttach, register

# ── Tool surface Woz advertises to the agent ────────────────────────────────
# These are advertised in place of the scaffold's default bash/grep tools
# (replace_tools=True below): Woz's whole thesis is that swapping the *tools*
# changes what lands in the transcript. The schemas below mirror the MCP
# server's published tool surface so the model can call them by name; the MCP
# server is what actually services each call.
#
# NOTE: these are descriptors only — no behavior. The runner forwards each tool
# call over the MCP stdio pipe spawned from `mcp_server_cmd`.

_SEARCH_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "woz_search",
        "description": (
            "Semantic code search over the Woz repository index. Give a query "
            "describing a symptom, behavior, function, or concept (it need not "
            "match the source literally). Returns the most relevant code "
            "locations ranked by usefulness, with just the surrounding lines — "
            "not whole files. Prefer this over shelling out to grep/rg/find: it "
            "keeps the transcript small by returning narrow, ranked hits."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What you're looking for (symptom/behavior/concept).",
                },
                "k": {
                    "type": "integer",
                    "description": "Max number of ranked locations to return (default 8).",
                },
            },
            "required": ["query"],
        },
    },
}

_EDIT_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "woz_edit",
        "description": (
            "Apply a structured edit to a file: replace an exact span with new "
            "text, addressed by path + a unique anchor string. Avoids re-reading "
            "or re-dumping the whole file into the transcript. Use this instead "
            "of catting a file and pasting the full rewritten contents back."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File to edit (repo-relative)."},
                "anchor": {
                    "type": "string",
                    "description": "Exact existing text to replace (must be unique in the file).",
                },
                "replacement": {"type": "string", "description": "Text to write in its place."},
            },
            "required": ["path", "anchor", "replacement"],
        },
    },
}

_SQL_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "woz_sql",
        "description": (
            "Query the Woz code index with a structured query (symbols, "
            "definitions, references, call sites, file metadata). Returns rows, "
            "not raw file text — a compact way to answer 'where is X defined / "
            "who calls Y' without scanning files in the transcript."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Structured index query (e.g. defs/refs/callers of a symbol).",
                },
            },
            "required": ["query"],
        },
    },
}

# Woz's tool discipline: replace the scaffold's broad shell tooling with this
# narrow, index-backed trio. The agent is steered to ranked search + structured
# edits instead of grep/cat dumps — that's the whole compression effect.
_WOZ_TOOLS: list[dict] = [_SEARCH_TOOL, _EDIT_TOOL, _SQL_TOOL]


@register("woz")
class WozArm(ToolArm):
    """Attach the Woz Claude Code MCP server as the agent's tool layer.

    Woz changes the agent's TOOLS (Search/Edit/Sql) rather than compressing the
    prompt stream or proxying the model. The benefit is indirect: sharper tools
    -> shorter tool calls -> a smaller transcript. ToolArm is the right pattern.

    Requires a Woz license (WOZ_API_KEY) and the Woz MCP server binary on PATH.
    """

    name = "woz"
    needs = ["WOZ_API_KEY"]

    def ready(self) -> tuple[bool, str]:
        """Ready iff the Woz license key is present AND the MCP server is runnable.

        We extend the default env-presence check with a resolve of the MCP
        launch command, so the runner skips this arm cleanly (rather than
        crashing mid-run) when Woz isn't installed/configured on this box.
        """
        ok, reason = super().ready()  # checks WOZ_API_KEY is set & non-empty
        if not ok:
            return ok, reason
        cmd = self._mcp_server_cmd()
        if cmd is None:
            return False, (
                "Woz MCP server command not configured. Set WOZ_MCP_CMD to the "
                "launch argv (e.g. 'node /path/to/woz/code-server.js --stdio'), "
                "or install the Woz plugin and point WOZ_MCP_CMD at its server."
            )
        exe = cmd[0]
        if shutil.which(exe) is None and not os.path.exists(exe):
            return False, f"Woz MCP launcher not found on PATH or disk: {exe!r}"
        return True, "ok"

    def _mcp_server_cmd(self) -> list[str] | None:
        """Resolve the argv that launches the Woz MCP stdio server.

        Operator override: set WOZ_MCP_CMD to a shell-style command string and we
        split it into argv. Otherwise return None ("not configured") so ready()
        reports it cleanly rather than the runner spawning a bogus process.

        TODO(woz-license): Woz is a paid plugin. Wire the real launch command
        here (or via WOZ_MCP_CMD). The canonical form is the Node entrypoint
        shipped with the plugin, e.g.:

            node <woz-plugin-dir>/code-server.js --stdio

        The WOZ_API_KEY is passed to the server via the ENVIRONMENT (the runner
        forwards the process env to the spawned MCP server) — do NOT inline the
        key into argv (it would leak into process listings / logs). If your Woz
        build instead expects `--api-key`, read it from os.environ here; never
        hardcode a literal key.
        """
        override = os.environ.get("WOZ_MCP_CMD")
        if override:
            return override.split()
        # TODO(woz-license): fill in the real path to the Woz MCP entrypoint,
        # e.g. return ["node", "/opt/woz/code-server.js", "--stdio"].
        return None

    def setup(self) -> None:
        """One-time prep before a batch of Woz runs.

        TODO(woz-license): if the Woz MCP server needs an explicit index/warm
        step (e.g. `woz index .` against the target repo) or a session login,
        do it here. WOZ_API_KEY is read from the environment by the spawned MCP
        server process, so there is nothing to inject here beyond optional index
        warming. No-op until a Woz license is wired in.
        """

    def teardown(self) -> None:
        """Cleanup after the batch.

        The runner owns the lifecycle of the process it spawned from
        mcp_server_cmd and will terminate it; nothing extra to do here unless a
        Woz session/login needs an explicit logout. No-op for now.
        """

    def attach(self) -> ToolAttach:
        """Return Woz's tool surface + the MCP server to spawn.

        replace_tools=True: Woz REPLACES the scaffold's default bash/grep tools
        with its narrow Search/Edit/Sql set. That replacement IS the arm — it's
        how Woz keeps the transcript small.
        """
        return ToolAttach(
            tools=list(_WOZ_TOOLS),
            mcp_server_cmd=self._mcp_server_cmd(),
            replace_tools=True,
        )
