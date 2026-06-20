"""rtk — the REAL rtk-ai/rtk product, run as a TOOL/HOOK arm (not a proxy).

WHAT RTK IS (and why it's a hook arm, NOT a proxy)
--------------------------------------------------
RTK ("Rust Token Killer", github.com/rtk-ai/rtk) is a single Rust CLI binary
that reduces the tokens a coding agent spends on SHELL-COMMAND OUTPUT by 60-90%.
It is NOT a network/HTTP compression proxy: it has no ``serve`` mode, no
``--upstream``, and it never sits on the model endpoint. It integrates with
Claude Code ONLY as a **PreToolUse hook** that transparently rewrites Bash
commands before they run — e.g. ``git status`` -> ``rtk git status`` — so the
``rtk`` wrapper executes the command and emits COMPRESSED stdout into the
agent's context. The native Read/Grep/Glob tools BYPASS it entirely: rtk only
touches the shell boundary (Bash), so only command stdout is compressed.

Its own install instructions wire this with ``rtk init -g``, which writes a
Claude Code PreToolUse hook that prepends ``rtk `` to the recognized base
commands. In this bench we reproduce that EXACT integration through the harness
PreToolUse hook capability (``Arm.pre_tool_hook``): when the agent issues a Bash
call whose command starts with an rtk-supported base command, we front it with
``rtk `` so the real binary compresses the output. The model call itself goes
STRAIGHT TO THE GATEWAY, exactly like the A0 baseline — RTK changes the tool
boundary, not the model seam.

WHERE RTK'S TOKEN SAVINGS COME FROM
-----------------------------------
The lever is SHELL-OUTPUT COMPRESSION at the Bash boundary. ``rtk git status``,
``rtk grep ...``, ``rtk pytest``, ``rtk cargo test``, ``rtk ls``, etc. filter and
compress the underlying command's stdout (drop noise, summarize, cap volume)
before that text accrues in the transcript. Because the gateway sits at the
bottom of the chain, it bills the REAL post-compression tokens: the smaller,
rewritten Bash output is what enters context, so the KPI path captures the win
with no extra accounting.

RECOGNIZED BASE COMMANDS (faithful to the product surface)
----------------------------------------------------------
RTK wraps these command families (per the product README): git, gh; the file
tools ls/cat/grep/rg/find/tree/diff; the test runners pytest/jest/vitest/
playwright/rspec/rake plus ``cargo test``/``go test``; build/lint cargo/tsc/
next/prettier/eslint/biome/ruff/golangci-lint/rubocop; package managers pnpm/
pip/bundle/prisma; cloud/containers docker/kubectl/aws/oc; and the network
utilities curl/wget. These are all real HOST EXECUTABLES the agent shells out
to — we deliberately do NOT wrap ``read`` (a bash builtin; the agent uses the
native Read tool, which bypasses rtk) or rtk's own SUBCOMMANDS (smart/json/
deps/env/log/summary — they're ``rtk smart``/``rtk json``, not standalone host
binaries, so fronting a bare ``env``/``log`` line would wrap an unrelated host
command). We only prepend ``rtk `` for a command whose FIRST token is one of
this recognized set — anything else (cd, mkdir, python, make, source, &&-chains
we can't safely split, ...) is left untouched, mirroring the real hook (it
no-ops on commands it doesn't support).

CLEAN-ROOM
----------
This module contains NO proprietary logic and imports NOTHING from
``adaptive_context``. It names the rtk base commands and prepends the ``rtk ``
wrapper; the actual compression lives in the rtk binary installed on the runner.

INSTALL (the binary IS the product — install it on the runner)
--------------------------------------------------------------
``rtk`` must be on PATH on the runner box. Install via the project's published
channels (any one):

    brew install rtk
    curl -fsSL https://raw.githubusercontent.com/rtk-ai/rtk/refs/heads/master/install.sh | sh
    cargo install --git https://github.com/rtk-ai/rtk

``ready()`` verifies the binary with ``rtk --version`` and SKIPs with a precise
reason if it's absent (mirroring how woz.ready() gates on node + plugin
presence). There is NO proxy to provision and NO ``RTK_BASE_URL`` — model routing
is gateway-direct.

Env:
  RTK_BIN  — (optional) path/name of the rtk binary (default ``rtk`` on PATH).
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess

from bench.arm import Arm, ArmKind, PreToolResult, register


def _rtk_bin() -> str:
    """The rtk binary to invoke. Honor RTK_BIN for a pinned path, else 'rtk'."""
    return os.environ.get("RTK_BIN", "rtk")


# The base commands the real rtk binary wraps. Faithful to the product surface
# (github.com/rtk-ai/rtk README): we only front-load ``rtk `` when the Bash
# command's FIRST token is one of these, mirroring rtk's own hook (a no-op on
# unsupported commands). Multi-word forms like ``cargo test`` / ``go test`` are
# still keyed on the first token (``cargo`` / ``go``); rtk dispatches the sub-verb.
#
# We list ONLY real HOST EXECUTABLES the agent actually shells out to — the
# programs whose stdout rtk wraps when fronted (``rtk git status`` runs git and
# compresses its output). We deliberately EXCLUDE:
#   * ``read`` — a bash *builtin*; the agent inspects files via the native Read
#     tool (which bypasses rtk), never a bare ``read ...`` shell line, so wrapping
#     it would only mis-fire on the rare builtin use;
#   * ``smart``/``json``/``deps``/``env``/``log``/``summary`` — these are rtk's own
#     SUBCOMMANDS (``rtk smart``, ``rtk json``), NOT standalone host binaries the
#     agent types. Prepending ``rtk `` to a bare ``env`` or ``log`` line would wrap
#     an *unrelated* host command (the POSIX ``env`` utility, a project ``log``
#     script), which the real rtk hook would never do. Narrowing to true host
#     executables keeps the rewrite faithful to rtk's recognized wrappable set.
_RTK_BASE_COMMANDS = frozenset(
    {
        # version control
        "git", "gh",
        # file/search tools (real host binaries the agent shells out to)
        "ls", "cat", "grep", "rg", "find", "tree", "diff",
        # test runners
        "pytest", "jest", "vitest", "playwright", "rspec", "rake", "go",
        # build / lint
        "cargo", "tsc", "next", "prettier", "eslint", "biome", "ruff",
        "golangci-lint", "rubocop",
        # package managers
        "pnpm", "pip", "bundle", "prisma",
        # cloud / containers
        "docker", "kubectl", "aws", "oc",
        # network utilities
        "curl", "wget",
    }
)


def _first_token(command: str) -> str | None:
    """The first shell token of ``command`` (the base command), or None if the
    command can't be safely parsed / is empty. Used to decide whether rtk wraps it.

    We deliberately only act on a SIMPLE leading command (no leading env-var
    assignment, pipe, or &&-chain): a single ``rtk `` prefix is only correct for a
    bare command, exactly as rtk's own hook treats it. Anything we can't cleanly
    front is left untouched (the real hook no-ops on it too)."""
    s = (command or "").strip()
    if not s:
        return None
    # Skip anything that isn't a plain leading command: a subshell/substitution
    # opener, or an already-wrapped command, would make a bare ``rtk `` prefix
    # wrong. Let those pass through unwrapped (faithful to the hook's no-op).
    if s[0] in "(${`" or s.startswith("rtk "):
        return None
    # Shell operators/redirects/chains mean a single leading ``rtk `` can't safely
    # front the WHOLE line (it would wrap only the first command, or swallow the
    # pipe). The real hook no-ops on these; so do we. (Check the RAW string: shlex
    # would split ``|``/``&&`` into bare tokens and hide them.)
    for op in ("|", "&", ";", ">", "<", "\n", "`", "$("):
        if op in s:
            return None
    try:
        tokens = shlex.split(s, posix=True)
    except ValueError:
        return None
    if not tokens:
        return None
    head = tokens[0]
    # A leading env assignment (FOO=bar cmd) — not a base command we wrap.
    if "=" in head and not head.startswith("-"):
        return None
    return head


@register("rtk")
class RtkArm(Arm):
    """Run rtk-ai/rtk's REAL product as a Claude Code PreToolUse HOOK arm.

    rtk compresses SHELL-COMMAND stdout at the Bash boundary; it is NOT a model
    proxy. We subclass ``Arm`` with ``kind = BASELINE`` so model routing is
    gateway-direct (identical to A0), and override ``pre_tool_hook`` to rewrite a
    Bash ``<cmd>`` into ``rtk <cmd>`` for the base commands rtk supports — the same
    rewrite rtk's own ``rtk init -g`` hook performs. The native Read/Grep/Glob
    tools are untouched (rtk only wraps Bash), faithful to the product.

    ``ready()`` requires the ``rtk`` binary on PATH (``rtk --version``); there is
    NO proxy and NO RTK_BASE_URL. SKIPs cleanly with a precise reason if rtk isn't
    installed on the runner.
    """

    name = "rtk"
    # BASELINE kind => model goes straight to the gateway (like A0). The COMPRESSION
    # is the Bash-rewrite hook below, wired by the runner's PreToolUse capability —
    # not a model-call seam. (needs is empty: the binary, not an env var, is the
    # requirement; ready() checks it directly.)
    kind = ArmKind.BASELINE
    needs: list[str] = []

    def ready(self) -> tuple[bool, str]:
        """Ready iff the real ``rtk`` binary is installed on the runner.

        The product IS the binary; without it on PATH the rewritten ``rtk git
        status`` would just fail in the shell. We resolve the binary and probe
        ``rtk --version``; SKIP with a precise, actionable reason if it's missing
        or non-functional (mirrors woz.ready() gating on node + plugin presence).
        Model routing is gateway-direct — there is nothing else to provision."""
        rtk = _rtk_bin()
        # Resolve to a concrete path so subprocess invokes the real binary directly
        # (also lets Windows resolve a .cmd/.exe wrapper that bare argv would miss);
        # on the Linux runner ``rtk`` resolves to itself.
        resolved = shutil.which(rtk) or (rtk if os.path.exists(rtk) else None)
        if resolved is None:
            return False, (
                f"rtk binary not found on PATH or disk: {rtk!r}. Install rtk-ai/rtk on "
                f"the runner box (e.g. `brew install rtk`, "
                f"`curl -fsSL https://raw.githubusercontent.com/rtk-ai/rtk/refs/heads/master/install.sh | sh`, "
                f"or `cargo install --git https://github.com/rtk-ai/rtk`), or set RTK_BIN."
            )
        try:
            proc = subprocess.run(
                [resolved, "--version"], capture_output=True, text=True, timeout=15
            )
        except Exception as e:  # noqa: BLE001 — surface a clear, non-secret reason
            return False, (
                f"rtk binary ({rtk!r}) failed to run `--version` "
                f"({type(e).__name__}: {e}). Reinstall rtk-ai/rtk on the runner box."
            )
        if proc.returncode != 0:
            tail = ((proc.stderr or "") + (proc.stdout or "")).strip()[-200:]
            return False, (
                f"`{rtk} --version` exited {proc.returncode}. The rtk install is broken; "
                f"reinstall rtk-ai/rtk. Output tail: {tail!r}"
            )
        ver = (proc.stdout or proc.stderr or "").strip().splitlines()
        return True, f"ok ({ver[0] if ver else 'rtk'})"

    def pre_tool_hook(self, tool_name: str, tool_input: dict) -> PreToolResult:
        """Rewrite a Bash ``<cmd>`` into ``rtk <cmd>`` for the commands rtk wraps.

        This reproduces exactly what rtk's own ``rtk init -g`` PreToolUse hook does:
        front a recognized shell command with the ``rtk `` wrapper so the binary
        compresses its stdout before it enters context. We only intercept Bash; the
        native Read/Grep/Glob tools are NOT touched (rtk only wraps the shell), and
        we only prepend when the command's first token is a base command rtk
        supports — otherwise we return None (leave the call untouched), matching the
        hook's no-op on unsupported commands. Already-wrapped commands (``rtk ...``)
        pass through unchanged so we never double-wrap."""
        if tool_name != "Bash":
            return None
        command = (tool_input or {}).get("command", "")
        head = _first_token(command)
        if head is None or head not in _RTK_BASE_COMMANDS:
            return None
        rtk = _rtk_bin()
        new_input = dict(tool_input)
        new_input["command"] = f"{rtk} {command.lstrip()}"
        return {"tool_input": new_input}
