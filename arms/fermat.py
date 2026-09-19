"""fermat — Quotient Labs' "Fermat's Last Token", run as its shipped `claude` shim.

Fermat (quotientlabs.com; install: ``curl -fsSL
https://downloads.quotientlabs.com/fermat/install.sh | bash && fermat login``)
is a Node runtime that ships a ``claude`` SHIM (``bin/claude`` → ``node
cli.js``). The shim starts a local proxy daemon and spawns the REAL Claude Code
with ANTHROPIC_BASE_URL pointed at that proxy, forces ``--agent fermat-code``,
attaches its Search/Edit MCP servers + a native-file tool-gate, bash-output and
idle compression, and a hosted "Stage 3" sidecar. Every feature is entitlement-
gated: without an entitled login it silently falls back to vanilla passthrough.

We run the product AS SHIPPED: point the SDK's executable at the Fermat shim
(``cli_path``) and let Fermat do everything — we disallow no tools ourselves
(Fermat's own tool-gate does that). The shim's real-Claude target is
``FERMAT_CLAUDE_BIN`` and its proxy upstream is our gateway; the exact upstream
var name is confirmed at box smoke, so we forward EVERY ``FERMAT_*`` var present
in the operator env into the child (they ride through automatically because the
child env starts from ``os.environ``), and also set ``ANTHROPIC_BASE_URL`` to the
gateway as the fallback upstream signal (done by the runner for this arm).

FAIL-CLOSED: ``ready()`` requires the shim to exist and run ``--version`` AND
``fermat whoami`` to report an entitled/logged-in account. If entitlement can't
be confirmed the arm SKIPS — otherwise it would silently run VANILLA and the
row would be mislabeled "fermat" (their RUNBOOK says so).

ENV:
  FERMAT_SHIM              path to the Fermat `claude` shim (e.g. ~/fermat/bin/claude). REQUIRED.
  FERMAT_BIN               path to the `fermat` CLI (for whoami/--version); default: a
                           sibling `fermat` next to FERMAT_SHIM, else `fermat` on PATH.
  FERMAT_CLAUDE_BIN        the REAL claude executable Fermat wraps; if unset we try to
                           resolve `claude-anthropic` (the installer preserves it there).
  FERMAT_UPSTREAM_BASE_URL / FERMAT_PROXY_BASE_URL
                           candidate vars for Fermat's proxy upstream — CONFIRM AT BOX
                           SMOKE; forwarded verbatim if the operator sets them.
  (any other FERMAT_* var in the operator env is forwarded to the child too.)
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Optional

from bench.arm import Arm, ArmKind, register

# Substrings in `fermat whoami` output that indicate an ENTITLED/logged-in account.
_ENTITLED_HINTS = ("entitled", "logged in", "logged-in", "active", "subscription",
                   "plan:", "@")
# Substrings that indicate NOT logged in / not entitled (checked first).
_NOT_ENTITLED_HINTS = ("not logged in", "not logged-in", "no account", "unauthenticated",
                       "please log in", "please login", "run `fermat login`",
                       "not entitled", "logged out", "no active")

# ClaudeAgentOptions field names (across SDK versions) that point the SDK at a
# specific `claude` executable. _run_sdk tries the same candidate names to place cli_path.
CLI_PATH_FIELD_CANDIDATES = ("cli_path", "path_to_claude_code_executable",
                            "claude_code_executable", "claude_executable", "executable")


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


def _fermat_version(cli: str) -> str:
    try:
        out = subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=15)
        return (out.stdout or out.stderr or "").strip()[:120]
    except Exception:
        return ""


@register("fermat")
class FermatArm(Arm):
    # BASELINE-shaped wiring: model goes direct to the gateway (client_base_url
    # stays None -> the worker points ANTHROPIC_BASE_URL at the gateway, which is
    # Fermat's proxy upstream). We add NO tool restrictions — Fermat's own
    # tool-gate does that. The only special wiring is cli_path (the shim).
    name = "fermat"
    kind = ArmKind.BASELINE
    needs = ["FERMAT_SHIM"]

    def __init__(self) -> None:
        # _run_sdk points ClaudeAgentOptions' cli-path field at this shim.
        self.cli_path = os.environ.get("FERMAT_SHIM")
        self.fermat_version = ""

    def setup(self) -> None:
        # Ensure the shim knows which real claude to wrap. If the operator didn't
        # set FERMAT_CLAUDE_BIN, try the installer's preserved `claude-anthropic`.
        if not os.environ.get("FERMAT_CLAUDE_BIN"):
            real = shutil.which("claude-anthropic")
            if real:
                os.environ["FERMAT_CLAUDE_BIN"] = real

    def ready(self) -> tuple[bool, str]:
        ok, reason = super().ready()  # FERMAT_SHIM set
        if not ok:
            return ok, reason
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
        # entitlement: `fermat whoami` must report a logged-in / entitled account
        cli = _resolve_fermat_cli()
        if not cli:
            return False, ("`fermat` CLI not found for the entitlement check (set FERMAT_BIN "
                           "or put `fermat` on PATH) — refusing to run un-verified")
        self.fermat_version = _fermat_version(cli)
        try:
            who = subprocess.run([cli, "whoami"], capture_output=True, text=True, timeout=25)
        except Exception as e:  # noqa: BLE001
            return False, f"`fermat whoami` failed: {type(e).__name__}: {str(e)[:160]}"
        text = ((who.stdout or "") + "\n" + (who.stderr or "")).strip()
        low = text.lower()
        not_entitled = who.returncode != 0 or any(h in low for h in _NOT_ENTITLED_HINTS)
        entitled = (not not_entitled) and any(h in low for h in _ENTITLED_HINTS)
        if not entitled:
            return False, ("Fermat not entitled — the arm would silently run vanilla "
                           "(their RUNBOOK says so). `fermat whoami` did not confirm a "
                           f"logged-in/entitled account (rc={who.returncode}): "
                           f"{text[:160]!r}")
        # Fail-closed: the SDK must expose a field to point its executable at the
        # shim, else the run would silently use the stock `claude` (not Fermat).
        field = _sdk_cli_path_field()
        if field is None:
            return False, ("installed Claude Agent SDK exposes no cli-path field on "
                           f"ClaudeAgentOptions (tried {CLI_PATH_FIELD_CANDIDATES}); the run "
                           "would silently use the stock claude, not the Fermat shim — "
                           "refusing. Confirm the field name for the pinned SDK (box smoke).")
        return True, (f"ok (shim={shim}, ver={self.fermat_version or '?'}, "
                      f"whoami confirmed, sdk_cli_field={field})")
