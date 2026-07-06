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

THE PRODUCT'S OWN REWRITER IS THE SOURCE OF TRUTH (no Python re-implementation)
------------------------------------------------------------------------------
Since v0.24.0, rtk ships a "thin delegator" Claude Code hook
(``~/.claude/hooks/rtk-rewrite.sh``, written by ``rtk init -g``) that does NOT
re-decide which commands to wrap in shell — it calls the binary's OWN subcommand
``rtk rewrite <cmd>`` to make that decision. ``rtk rewrite`` is documented as the
"single source of truth for hooks": it prints the rewritten command to stdout if
rtk has an equivalent, and prints NOTHING if it doesn't. The product's real hook
is literally::

    REWRITTEN=$(rtk rewrite "$CMD") || exit 0   # empty/err => run $CMD unchanged

This arm reproduces THAT EXACT integration: for a Bash call we shell out to
``rtk rewrite -- <command>`` and use its stdout VERBATIM as the rewritten
command (no-op when it returns nothing). We do NOT re-implement rtk's
command-detection in Python — so the arm wraps rtk's FULL command set (100+
commands incl. ``cat`` -> ``rtk read``, ``git pull``, ``git commit``, …) and gets
rtk's OWN pipe/&&-chain/env-prefix handling for free (e.g.
``cargo test && git push`` -> ``rtk cargo test && rtk git push``,
``FOO=bar git status`` -> ``FOO=bar rtk git status``). The model call itself goes
STRAIGHT TO THE GATEWAY, exactly like the A0 baseline — RTK changes the tool
boundary, not the model seam.

WHY DELEGATE INSTEAD OF A HAND-CURATED LIST
-------------------------------------------
A prior version of this arm hand-curated a ~30-entry base-command set and a
``_first_token`` parser, then prepended a bare ``rtk ``. That was an
APPROXIMATION of the product, not the product: it under-wrapped (missed
``cat``->``read``, ``docker compose``, etc.), keyed multiword forms on the first
token only, and no-opped ENTIRELY on any pipe/chain/subshell/env-prefix — so the
agent's transcript saw LESS rtk compression than the real ``rtk init -g`` hook
produces, mis-measuring rtk's token win. Delegating to ``rtk rewrite`` runs the
REAL detection over the full set, eliminating that gap. (For the commands it
wraps, the rewritten ``rtk git status`` invokes the real installed binary, so the
compression is genuine — the only thing that ever differed was the trigger layer.)

WHERE RTK'S TOKEN SAVINGS COME FROM
-----------------------------------
The lever is SHELL-OUTPUT COMPRESSION at the Bash boundary. ``rtk git status``,
``rtk grep ...``, ``rtk pytest``, ``rtk cargo test``, ``rtk ls``, etc. filter and
compress the underlying command's stdout (drop noise, summarize, cap volume)
before that text accrues in the transcript. Because the gateway sits at the
bottom of the chain, it bills the REAL post-compression tokens: the smaller,
rewritten Bash output is what enters context, so the KPI path captures the win
with no extra accounting.

CLEAN-ROOM
----------
This module contains NO proprietary logic and imports NOTHING from
any vendor internals. It shells out to the rtk binary's own ``rewrite``
subcommand; the actual rewrite DECISION and the compression both live in the rtk
binary installed on the runner.

INSTALL (the binary IS the product — install it on the runner)
--------------------------------------------------------------
``rtk`` (>= 0.24.0, for the ``rewrite`` subcommand) must be on PATH on the
runner box. Install via the project's published channels (any one):

    brew install rtk
    curl -fsSL https://raw.githubusercontent.com/rtk-ai/rtk/refs/heads/master/install.sh | sh
    cargo install --git https://github.com/rtk-ai/rtk

``ready()`` verifies the binary with ``rtk --version`` AND smokes the real
``rtk rewrite -- 'git status'`` integration path (SKIPping with a precise reason
if the subcommand is absent — i.e. a < 0.24.0 binary), so a paid panel never runs
against a legacy rtk that lacks the product's rewrite hook. There is NO proxy to
provision and NO ``RTK_BASE_URL`` — model routing is gateway-direct.

Env:
  RTK_BIN          — (optional) path/name of the rtk binary (default ``rtk`` on PATH).
  RTK_REWRITE_TIMEOUT_S — (optional) per-call timeout for ``rtk rewrite`` (default 10s).
"""

from __future__ import annotations

import os
import shutil
import subprocess

from bench.arm import Arm, ArmKind, PreToolResult, register


def _rtk_bin() -> str:
    """The rtk binary to invoke. Honor RTK_BIN for a pinned path, else 'rtk'."""
    return os.environ.get("RTK_BIN", "rtk")


def _resolve_rtk(rtk: str) -> str | None:
    """Resolve ``rtk`` to a concrete path (PATH lookup, then literal-path check).

    Returns the resolved path, or None if the binary can't be found. Resolving to
    a path lets subprocess invoke the real binary directly (and lets Windows pick
    up a .cmd/.exe wrapper a bare argv would miss); on the Linux runner ``rtk``
    resolves to itself."""
    return shutil.which(rtk) or (rtk if os.path.exists(rtk) else None)


def _rewrite_timeout_s() -> float:
    """Per-call timeout (seconds) for ``rtk rewrite``. Env-overridable; small —
    the subcommand is a pure string transform that does NOT run the command."""
    try:
        return float(os.environ.get("RTK_REWRITE_TIMEOUT_S", "10"))
    except ValueError:
        return 10.0


@register("rtk")
class RtkArm(Arm):
    """Run rtk-ai/rtk's REAL product as a Claude Code PreToolUse HOOK arm.

    rtk compresses SHELL-COMMAND stdout at the Bash boundary; it is NOT a model
    proxy. We subclass ``Arm`` with ``kind = BASELINE`` so model routing is
    gateway-direct (identical to A0), and override ``pre_tool_hook`` to DELEGATE
    the rewrite decision to the product's own ``rtk rewrite`` subcommand — the
    same single-source-of-truth the real ``rtk init -g`` hook calls — using its
    stdout verbatim as the rewritten Bash command. The native Read/Grep/Glob
    tools are untouched (rtk only wraps Bash), faithful to the product.

    ``ready()`` requires the ``rtk`` binary on PATH (``rtk --version``) AND a
    working ``rtk rewrite`` (>= 0.24.0); there is NO proxy and NO RTK_BASE_URL. It
    SKIPs cleanly with a precise reason if rtk isn't installed or is too old.
    """

    name = "rtk"
    # BASELINE kind => model goes straight to the gateway (like A0). The COMPRESSION
    # is the Bash-rewrite hook below, wired by the runner's PreToolUse capability —
    # not a model-call seam. (needs is empty: the binary, not an env var, is the
    # requirement; ready() checks it directly.)
    kind = ArmKind.BASELINE
    needs: list[str] = []

    def ready(self) -> tuple[bool, str]:
        """Ready iff the real ``rtk`` binary is installed AND ships ``rtk rewrite``.

        The product IS the binary; without it on PATH the rewritten ``rtk git
        status`` would just fail in the shell. We (1) resolve the binary and probe
        ``rtk --version``, then (2) smoke the actual integration path the arm
        relies on — ``rtk rewrite -- 'git status'`` — and require it to emit a
        rewritten command. Step (2) catches a < 0.24.0 binary whose ``--version``
        passes but which lacks the product's ``rewrite`` source-of-truth (a legacy
        rtk would silently never compress). SKIP with a precise, actionable reason
        if anything is missing (mirrors woz.ready() gating on the real login
        session, not just node presence). Model routing is gateway-direct — there
        is nothing else to provision."""
        rtk = _rtk_bin()
        resolved = _resolve_rtk(rtk)
        if resolved is None:
            return False, (
                f"rtk binary not found on PATH or disk: {rtk!r}. Install rtk-ai/rtk on "
                f"the runner box (e.g. `brew install rtk`, "
                f"`curl -fsSL https://raw.githubusercontent.com/rtk-ai/rtk/refs/heads/master/install.sh | sh`, "
                f"or `cargo install --git https://github.com/rtk-ai/rtk`), or set RTK_BIN."
            )
        # (1) the binary runs at all.
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
        ver_lines = (proc.stdout or proc.stderr or "").strip().splitlines()
        ver = ver_lines[0] if ver_lines else "rtk"

        # (2) the REAL integration path: `rtk rewrite` must exist (>= 0.24.0) and
        #     actually rewrite a known-wrapped command. A legacy binary errors
        #     ("unrecognized subcommand") or prints nothing for `git status` — we
        #     SKIP with the >= 0.24.0 requirement named, BEFORE a paid panel.
        try:
            rw = subprocess.run(
                [resolved, "rewrite", "--", "git status"],
                capture_output=True, text=True, timeout=15,
            )
        except Exception as e:  # noqa: BLE001
            return False, (
                f"rtk binary ({rtk!r}) failed to run `rtk rewrite` "
                f"({type(e).__name__}: {e}). The arm needs rtk >= 0.24.0 (the "
                f"`rewrite` subcommand the real Claude Code hook calls). Reinstall a "
                f"current rtk-ai/rtk."
            )
        out = (rw.stdout or "").strip()
        # `rtk rewrite` prints the rewritten command on stdout when supported (its
        # exit code is non-zero by design for the "rewritten" case, so we GATE ON
        # STDOUT, not rc — matching the product hook `REWRITTEN=$(rtk rewrite …)`).
        # A version that lacks the subcommand writes an "unrecognized subcommand"
        # error to STDERR and leaves stdout empty.
        if not out:
            tail = ((rw.stderr or "") + (rw.stdout or "")).strip()[-200:]
            return False, (
                f"`{rtk} rewrite -- 'git status'` produced no rewritten command "
                f"(rc={rw.returncode}). This rtk lacks the `rewrite` subcommand the "
                f"real Claude Code hook relies on — install rtk-ai/rtk >= 0.24.0. "
                f"Output tail: {tail!r}"
            )
        if "rtk " not in out:
            # Defensive: stdout present but not a recognizable rtk rewrite.
            return False, (
                f"`{rtk} rewrite -- 'git status'` returned an unexpected result "
                f"{out!r} (expected an `rtk …`-wrapped command). Verify the rtk "
                f"install (>= 0.24.0)."
            )
        return True, f"ok ({ver}; rewrite -> {out!r})"

    def pre_tool_hook(self, tool_name: str, tool_input: dict) -> PreToolResult:
        """Rewrite a Bash command by DELEGATING to the product's ``rtk rewrite``.

        This reproduces exactly what rtk's own ``rtk init -g`` PreToolUse hook
        does: instead of re-deciding in Python which commands to wrap, we shell out
        to ``rtk rewrite -- <command>`` (the binary's documented single source of
        truth for hooks) and use its stdout VERBATIM as the new Bash command. rtk
        decides everything — the full 100+ command set, the ``cat`` -> ``rtk read``
        remap, pipe/&&-chain/env-prefix handling — so the bench sees the SAME
        rewrites the real hook produces.

        We only intercept Bash (rtk wraps the shell, never the native Read/Grep/
        Glob tools). ``rtk rewrite`` prints the rewritten command on stdout when it
        has an equivalent and prints NOTHING otherwise; we therefore GATE ON STDOUT
        (not the exit code, which rtk sets non-zero for the rewritten case):

          * non-empty stdout that DIFFERS from the input -> REWRITE (use it);
          * empty stdout, or stdout equal to the input -> None (leave untouched —
            faithful to the hook's no-op on unsupported commands, and never
            double-wrapping an already-``rtk ``-prefixed command, which rtk returns
            unchanged).

        If the rtk binary can't be resolved or ``rtk rewrite`` fails/ times out, we
        return None (run the original command unchanged) — exactly like the product
        hook's ``$(rtk rewrite "$CMD") || exit 0`` fallback. ready() has already
        gated the binary's presence + rewrite support before any run, so this is a
        belt-and-suspenders safety net, not the primary check."""
        if tool_name != "Bash":
            return None
        command = (tool_input or {}).get("command", "")
        if not command or not command.strip():
            return None
        rtk = _rtk_bin()
        resolved = _resolve_rtk(rtk)
        if resolved is None:
            return None  # no binary -> run unchanged (product hook's `|| exit 0`)
        try:
            proc = subprocess.run(
                [resolved, "rewrite", "--", command],
                capture_output=True,
                text=True,
                timeout=_rewrite_timeout_s(),
            )
        except Exception:  # noqa: BLE001 — never break the agent's tool call
            return None
        rewritten = (proc.stdout or "").strip()
        # No equivalent (empty stdout), or rtk returned the command unchanged
        # (already wrapped / nothing to do) -> leave the call untouched.
        if not rewritten or rewritten == command.strip():
            return None
        new_input = dict(tool_input)
        new_input["command"] = rewritten
        return {"tool_input": new_input}
