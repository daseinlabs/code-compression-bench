"""caveman — JuliusBrussee/caveman, the Claude Code output-compression plugin (ToolArm).

Caveman (github.com/JuliusBrussee/caveman, ~85k*) is a *Claude Code plugin* — not a
proxy, not a context compressor. It compresses the agent's OUTPUT ~65% by making it
answer in terse "caveman speak" (drop articles / filler / hedging, fragments OK)
while keeping the technical substance. It ships a real `.claude-plugin/plugin.json`
whose ``SessionStart`` hook (``src/hooks/caveman-activate.js``) reads
``skills/caveman/SKILL.md`` and emits the full caveman ruleset as injected session
context at intensity ``full`` (the product's default), plus a ``UserPromptSubmit``
hook that tracks the active mode. So the only faithful way to measure the PRODUCT is
to load its real plugin into Claude Code exactly as shipped — the Woz pattern — NOT
to hand-roll its ruleset (that would measure our interpretation, not caveman).

Topology: caveman never touches the model call, so this is the BASELINE chain —
ClaudeCode -> gateway -> Vertex — with caveman's plugin loaded on top. Its hooks
inject the ruleset; the native tool surface (Read/Edit/Write/Grep/Glob/Bash) is
UNCHANGED, because caveman only shapes what the agent SAYS, not how it solves. That
makes it the clean foil to the input-side compressors: output degraded to fragments,
input + reasoning untouched — so the bench prices its real (small) whole-request
saving and measures its effect on solve rate.

Clean-room: this module only NAMES the plugin directory to load and TCP/file-checks
it. No caveman logic is imported or vendored here; the compression behavior lives
entirely inside the loaded plugin (its hooks + SKILL.md), applied by Claude Code.

Env:
  CAVEMAN_PLUGIN_DIR    — path to a clone of github.com/JuliusBrussee/caveman on the
                          runner box. The runner loads this whole dir via the SDK
                          ``plugins=[{"type":"local","path":...}]`` so Claude Code
                          activates caveman's OWN hooks / skills / commands as shipped.
  CAVEMAN_DEFAULT_MODE  — intensity the SessionStart hook activates (default 'full',
                          the product's own default). Pin it on the run so the hook is
                          deterministic regardless of any stray user/repo caveman
                          config on the box.
"""

from __future__ import annotations

import os
import shutil

from bench.arm import ToolArm, ToolAttach, register

# The product's own default intensity (caveman-config.js getDefaultMode() -> 'full').
DEFAULT_MODE = "full"


def _plugin_dir() -> str | None:
    """The caveman plugin root (CAVEMAN_PLUGIN_DIR), or None if unset. This is what
    the runner loads via the SDK ``plugins=[{"type":"local","path":...}]`` so Claude
    Code activates caveman's OWN SessionStart/UserPromptSubmit hooks, skills, and
    slash commands."""
    d = os.environ.get("CAVEMAN_PLUGIN_DIR")
    return d.rstrip("/") if d else None


@register("caveman")
class CavemanArm(ToolArm):
    name = "caveman"
    needs = ["CAVEMAN_PLUGIN_DIR"]

    def ready(self) -> tuple[bool, str]:
        """Ready iff the caveman plugin is present AND its activation hook + a node
        runtime exist — so a mis-pathed clone SKIPs cleanly instead of loading a
        plugin whose ruleset never injects (a silent no-op = baseline-vs-baseline,
        the exact trap to avoid)."""
        d = _plugin_dir()
        if not d:
            return (False, "CAVEMAN_PLUGIN_DIR unset — clone github.com/JuliusBrussee/caveman "
                           "and point CAVEMAN_PLUGIN_DIR at the repo root.")
        manifest = os.path.join(d, ".claude-plugin", "plugin.json")
        if not os.path.isfile(manifest):
            return (False, f"no plugin manifest at {manifest} — CAVEMAN_PLUGIN_DIR must be the "
                           f"caveman repo root (the dir holding .claude-plugin/plugin.json).")
        hook = os.path.join(d, "src", "hooks", "caveman-activate.js")
        if not os.path.isfile(hook):
            return (False, f"missing SessionStart activation hook {hook} — the plugin would load "
                           f"but never inject its ruleset (a silent no-op == baseline).")
        if shutil.which("node") is None:
            return (False, "node not found — caveman's activation hooks are node scripts; "
                           "install Node.js on the runner box.")
        return (True, "ok")

    def attach(self) -> ToolAttach:
        """Load caveman's REAL plugin. Claude Code activates its SessionStart +
        UserPromptSubmit hooks (which inject the caveman ruleset at ``full``), its
        skills and slash commands — the shipped product. ``replace_tools=False``:
        caveman only shapes OUTPUT, so the agent keeps the full native tool surface
        exactly like baseline; the ONLY difference from baseline is the injected
        caveman output ruleset. Caveman advertises no MCP solving tools, so no globs."""
        return ToolAttach(
            plugin_dir=_plugin_dir(),
            plugin_tool_globs=[],
            replace_tools=False,
        )
