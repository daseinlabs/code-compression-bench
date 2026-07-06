#!/usr/bin/env bash
# Provision the REAL rtk-ai/rtk binary for the bench's `rtk` arm — onto a SYSTEM
# PATH dir the NON-INTERACTIVE runner process can see.
#
# WHY this script exists (the blocker it fixes)
# ---------------------------------------------
# rtk is a HOOK arm, not a proxy: `RtkArm.ready()` runs `rtk --version` and the
# `pre_tool_hook` rewrites `Bash <cmd>` -> `rtk <cmd>`. Both need the `rtk` binary
# resolvable BY THE RUNNER PROCESS. The panel launch is a NON-INTERACTIVE ssh:
#
#     gcloud compute ssh <runner> --command '<… python -m bench.cc_runner …>'
#
# which does NOT source ~/.bashrc or ~/.profile. So a binary under ~/.local/bin
# (added to PATH only by those interactive rc files) is INVISIBLE to the worker:
# `which rtk` => none => ready() returns (False, 'rtk binary not found …') => the
# arm self-SKIPs and emits NO rtk trajectory. That is exactly the audit blocker.
#
# This script lands rtk on a dir that IS on the stock non-interactive PATH
# (`/usr/local/bin`, present in `/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:…`),
# so the runner process resolves it with NO login shell. It is idempotent and
# verifies under the real non-interactive resolution at the end.
#
# Belt-and-suspenders: ALSO pin `RTK_BIN=/usr/local/bin/rtk` (or the source path)
# in the bench `.env`, which `cc_runner.main()` loads via python-dotenv — so
# `ready()` and the rewrite resolve an absolute path even if PATH provisioning is
# ever skipped. See arms/rtk.py (`_rtk_bin`) and .env.example.
#
# Usage (on the runner box):
#     bash selfhost/rtk/install.sh
#   Env knobs (all optional):
#     RTK_SRC      pre-existing rtk binary to symlink (default: autodetect
#                  ~/.local/bin/rtk, then `command -v rtk`). If neither exists and
#                  RTK_INSTALL=1, the official installer is run first.
#     RTK_DEST     system PATH target (default: /usr/local/bin/rtk).
#     RTK_INSTALL  =1 to fetch via the official installer when no binary is found.
set -euo pipefail

RTK_DEST="${RTK_DEST:-/usr/local/bin/rtk}"
RTK_INSTALL="${RTK_INSTALL:-0}"

echo "=== rtk provisioning (system-PATH for the non-interactive runner) ==="

# 1) Locate an rtk binary to publish.
src="${RTK_SRC:-}"
if [ -z "${src}" ]; then
  if [ -x "${HOME}/.local/bin/rtk" ]; then
    src="${HOME}/.local/bin/rtk"
  elif command -v rtk >/dev/null 2>&1; then
    src="$(command -v rtk)"
  fi
fi

# 2) Install via the official channel if asked and nothing is present.
if [ -z "${src}" ] && [ "${RTK_INSTALL}" = "1" ]; then
  echo "[rtk] no binary found; running the official installer (rtk-ai/rtk) …"
  curl -fsSL https://raw.githubusercontent.com/rtk-ai/rtk/refs/heads/master/install.sh | sh
  if [ -x "${HOME}/.local/bin/rtk" ]; then
    src="${HOME}/.local/bin/rtk"
  elif command -v rtk >/dev/null 2>&1; then
    src="$(command -v rtk)"
  fi
fi

if [ -z "${src}" ] || [ ! -x "${src}" ]; then
  echo "[rtk] ERROR: no rtk binary found to publish." >&2
  echo "      Install it first (brew install rtk | the official install.sh |" >&2
  echo "      cargo install --git https://github.com/rtk-ai/rtk), or re-run with" >&2
  echo "      RTK_INSTALL=1, or pass RTK_SRC=/path/to/rtk." >&2
  exit 1
fi
echo "[rtk] source binary: ${src}  ($("${src}" --version 2>&1 | head -1))"

# 3) Publish onto the system PATH dir (symlink; copy if symlink isn't possible).
#    /usr/local/bin is already on the non-interactive PATH, so no rc-file edit is
#    needed. Use sudo only if the dest dir isn't writable by us.
dest_dir="$(dirname "${RTK_DEST}")"
SUDO=""
if [ ! -w "${dest_dir}" ]; then SUDO="sudo"; fi
if ! ${SUDO} ln -sfn "${src}" "${RTK_DEST}" 2>/dev/null; then
  echo "[rtk] symlink failed; copying instead."
  ${SUDO} install -m 0755 "${src}" "${RTK_DEST}"
fi
echo "[rtk] published: ${RTK_DEST} -> ${src}"

# 4) VERIFY under the NON-INTERACTIVE resolution the runner actually uses.
#    `env -i` strips the environment so PATH falls back to the system default,
#    proving the runner process (which never sourced .bashrc) will find rtk.
echo "=== verify (non-interactive PATH resolution) ==="
if env -i PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
     rtk --version >/dev/null 2>&1; then
  echo "[ok] non-interactive shell resolves: rtk $(env -i PATH='/usr/local/bin:/usr/bin:/bin' rtk --version 2>&1 | head -1)"
else
  echo "[rtk] ERROR: rtk still not on the non-interactive PATH after publish." >&2
  exit 1
fi
echo "=== rtk provisioning DONE ==="
