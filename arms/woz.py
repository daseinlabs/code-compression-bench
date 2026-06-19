"""Woz arm — a ToolArm wrapping Woz's REAL Claude Code MCP server.

WHAT WOZ IS (and why it's a ToolArm, not a proxy or transform)
--------------------------------------------------------------
Woz is a *paid* Claude Code plugin that ships an MCP (Model Context Protocol)
server (``servers/code-server.js`` in github.com/WithWoz/wozcode-plugin). It does
NOT compress the model's prompt stream and it does NOT sit on the model endpoint.
Instead it changes the agent's TOOLS: it replaces the generic shell-and-grep tool
surface with a smaller, sharper set — an index-backed code-query/search/edit
surface — backed by its own repository index that lives MCP-server-side.

The compression mechanism is therefore *indirect*. By giving the agent
high-signal tools (a ranked semantic search instead of ``grep -r``, a structured
edit instead of re-``cat``-ing whole files), Woz steers the agent toward short,
targeted tool calls. Fewer giant ``cat``/``grep`` dumps land in the transcript,
so the prompt that accrues across turns stays smaller — without ever rewriting a
message or proxying the model. That is exactly the ToolArm contract: we return
the MCP server command to spawn (+ ``replace_tools``) and let the runner spawn it,
discover its REAL tools via ``tools/list``, and wire them into the one fixed
scaffold; the model endpoint is untouched.

CLEAN-ROOM
----------
This module contains NO proprietary logic and imports NOTHING from
``adaptive_context``. It only *names* the MCP server command to launch and the
env it needs. The actual Search/Edit/Query implementations live inside the Woz
plugin on the other side of the MCP stdio pipe — we neither import nor
reimplement them, and we do NOT hand-mirror their schemas: the runner discovers
the live tool surface from the server's own ``tools/list`` at run time.

LAUNCH (mirrors the plugin's .mcp.json)
---------------------------------------
The plugin's ``.mcp.json`` declares::

    "code": {
      "command": "node",
      "args": ["--no-warnings=ExperimentalWarning",
               "${CLAUDE_PLUGIN_ROOT}/servers/code-server.js"],
      "env": {
        "WOZCODE_MCP_CWD_HOOK_INJECTED": "1",
        "WOZCODE_POSTHOG_ENABLED": "true",
        "WOZCODE_POSTHOG_PROJECT_TOKEN": "phc_...",
        "WOZCODE_POSTHOG_PROJECT_REGION": "us"
      }
    }

We reproduce that exactly: ``${CLAUDE_PLUGIN_ROOT}`` is ``WOZ_PLUGIN_DIR`` (a
clone of the plugin repo on the runner box). ``WOZ_API_KEY`` is passed to the
server via the ENVIRONMENT (never argv, so it can't leak into process listings).

Env:
  WOZ_API_KEY     — license/account key for the Woz plugin; passed to the spawned
                    MCP server via its environment (never inlined into argv).
  WOZ_PLUGIN_DIR  — path to a clone of github.com/WithWoz/wozcode-plugin on the
                    runner box (the ``${CLAUDE_PLUGIN_ROOT}``). The server file is
                    ``<WOZ_PLUGIN_DIR>/servers/code-server.js``.
  WOZ_MCP_CMD     — (optional) operator override: a full shell-style command that
                    launches the MCP stdio server, in case the plugin layout
                    differs. If set it wins over the WOZ_PLUGIN_DIR default.
"""

from __future__ import annotations

import os
import shutil

from bench.arm import ToolArm, ToolAttach, register


# ── env the plugin's .mcp.json sets on the server process ────────────────────
# Reproduced verbatim from github.com/WithWoz/wozcode-plugin/.mcp.json so the
# server behaves identically to a real Claude Code launch (the cwd hook + the
# vendor's PostHog telemetry config). WOZ_API_KEY is added at attach() time.
_WOZ_SERVER_ENV: dict[str, str] = {
    "WOZCODE_MCP_CWD_HOOK_INJECTED": "1",
    "WOZCODE_POSTHOG_ENABLED": "true",
    "WOZCODE_POSTHOG_PROJECT_TOKEN": "phc_F3mo2emdspgzD4QmFMQxHQfab1TyXgCAU7eYBakKq9k",
    "WOZCODE_POSTHOG_PROJECT_REGION": "us",
}

# node flag the plugin uses (suppress the ExperimentalWarning noise on stdout).
_NODE_FLAGS = ["--no-warnings=ExperimentalWarning"]


def _node_exe() -> str:
    """The node binary to launch. Honor WOZ_NODE for a pinned node, else 'node'."""
    return os.environ.get("WOZ_NODE", "node")


def _server_js() -> str | None:
    """Path to ``servers/code-server.js`` under WOZ_PLUGIN_DIR, or None if unset."""
    plugin_dir = os.environ.get("WOZ_PLUGIN_DIR")
    if not plugin_dir:
        return None
    return os.path.join(plugin_dir, "servers", "code-server.js")


@register("woz")
class WozArm(ToolArm):
    """Attach Woz's REAL Claude Code MCP server as the agent's tool layer.

    Woz changes the agent's TOOLS rather than compressing the prompt stream or
    proxying the model. The benefit is indirect: sharper tools -> shorter tool
    calls -> a smaller transcript. ToolArm is the right pattern.

    Requires a Woz license (WOZ_API_KEY), a resolvable ``node``, and a clone of
    the plugin (WOZ_PLUGIN_DIR) whose ``servers/code-server.js`` exists.
    """

    name = "woz"
    needs = ["WOZ_API_KEY"]

    def ready(self) -> tuple[bool, str]:
        """Ready iff WOZ_API_KEY is set AND the MCP server is actually runnable:
        a resolvable node + a real server entrypoint on disk. So the runner skips
        this arm cleanly (with a precise reason) when Woz isn't installed here,
        rather than crashing mid-run."""
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
                f"(install Node.js on the runner box, or set WOZ_NODE)."
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

    def _mcp_server_cmd(self) -> list[str] | None:
        """Resolve the argv that launches the Woz MCP stdio server.

        Precedence:
          1. WOZ_MCP_CMD (operator override) — split a shell-style string to argv.
          2. WOZ_PLUGIN_DIR default — reproduce the plugin's .mcp.json command:
             ``node --no-warnings=ExperimentalWarning <DIR>/servers/code-server.js``.
          3. Neither configured -> None ("not configured"), so ready() reports it
             cleanly rather than the runner spawning a bogus process.

        WOZ_API_KEY is NEVER placed here — it travels via the environment (see
        attach().server_env), so it can't leak into process listings / logs.
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

        The REAL tools come from the live ``tools/list`` the runner issues after
        the handshake — we do NOT hardcode mirrored schemas (``tools=[]``).
        ``replace_tools=True``: Woz REPLACES the scaffold's bash tool with its own
        index-backed set; that replacement IS the arm. The API key + the plugin's
        server env ride in via ``server_env`` (environment, never argv).
        """
        server_env = dict(_WOZ_SERVER_ENV)
        api_key = os.environ.get("WOZ_API_KEY")
        if api_key:
            # The server reads its license from the environment. We forward under
            # the same name; a build expecting a different var can be bridged via
            # WOZ_MCP_CMD or by exporting that var on the runner box.
            server_env["WOZ_API_KEY"] = api_key
        return ToolAttach(
            tools=[],  # discovery-driven: no hand-mirrored schemas
            mcp_server_cmd=self._mcp_server_cmd(),
            replace_tools=True,
            server_env=server_env,
        )
