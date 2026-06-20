#!/usr/bin/env bash
# Build the FORKED Edgee CLI for the bench's `edgee` arm.
#
# WHY a fork: upstream `edgee-ai/edgee`'s local gateway (the `edgee local-gateway`
# subcommand and the `--local-gateway` flag on `edgee launch claude`) hardcodes the
# Anthropic Messages upstream to `https://api.anthropic.com`. The passthrough config
# has always carried a `with_base_url` override (unit-tested in `edgee_gateway_core`),
# but `local_gateway::start()` built the service with the DEFAULT config, so there was
# no way to point the Anthropic upstream at our run gateway. The patch in
# `anthropic_upstream.patch` wires `EDGEE_ANTHROPIC_UPSTREAM` (and, symmetrically,
# `EDGEE_OPENAI_UPSTREAM`) into `start()` via that existing override.
#
# This is a REAL build of the REAL product (compression layer + Anthropic-native
# passthrough), not an approximation: only the upstream base_url becomes configurable.
#
# Usage (on the runner box, Linux):
#   bash selfhost/edgee/build.sh            # clone+patch+build into ./build
#   EDGEE_FORK_DIR=/opt/edgee bash selfhost/edgee/build.sh
#
# Output: the `edgee` release binary path is printed on the last line and also
# symlinked/copied to $EDGEE_BIN_OUT (default: ~/.local/bin/edgee).
set -euo pipefail

# Pin the exact upstream commit the patch was cut against (edgee-cli 0.2.9). Override
# only when re-cutting the patch against a newer upstream.
EDGEE_REPO="${EDGEE_REPO:-https://github.com/edgee-ai/edgee.git}"
EDGEE_COMMIT="${EDGEE_COMMIT:-402004f6bc472eb989b7a89d96cf919761b54c52}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCH="$HERE/anthropic_upstream.patch"
FORK_DIR="${EDGEE_FORK_DIR:-$HERE/build/edgee-fork}"
EDGEE_BIN_OUT="${EDGEE_BIN_OUT:-$HOME/.local/bin/edgee}"

[ -f "$PATCH" ] || { echo "missing patch: $PATCH" >&2; exit 1; }

# Rust toolchain (rustup) — install non-interactively if absent.
if ! command -v cargo >/dev/null 2>&1; then
  echo "[edgee] installing rustup toolchain..." >&2
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
  # shellcheck disable=SC1090
  source "$HOME/.cargo/env"
fi

echo "[edgee] clone $EDGEE_REPO @ $EDGEE_COMMIT -> $FORK_DIR" >&2
rm -rf "$FORK_DIR"
mkdir -p "$(dirname "$FORK_DIR")"
git clone --quiet "$EDGEE_REPO" "$FORK_DIR"
git -C "$FORK_DIR" checkout --quiet "$EDGEE_COMMIT"

echo "[edgee] apply patch: $(basename "$PATCH")" >&2
git -C "$FORK_DIR" apply --check "$PATCH"
git -C "$FORK_DIR" apply "$PATCH"

# edgee-cli is a binary-only crate (no lib target), so the unit tests live in the
# binary and run WITHOUT --lib. `--bin edgee` scopes to the CLI binary's tests.
echo "[edgee] cargo test -p edgee-cli --bin edgee local_gateway (fork wiring)" >&2
( cd "$FORK_DIR" && cargo test -p edgee-cli --bin edgee local_gateway:: 2>&1 | tail -25 )

echo "[edgee] cargo build --release -p edgee-cli" >&2
( cd "$FORK_DIR" && cargo build --release -p edgee-cli )

# Locate the produced binary (workspace target dir).
BIN="$FORK_DIR/target/release/edgee"
[ -x "$BIN" ] || BIN="$(find "$FORK_DIR/target/release" -maxdepth 1 -name edgee -type f | head -1)"
[ -x "$BIN" ] || { echo "[edgee] build produced no edgee binary" >&2; exit 1; }

mkdir -p "$(dirname "$EDGEE_BIN_OUT")"
cp -f "$BIN" "$EDGEE_BIN_OUT"
echo "[edgee] installed -> $EDGEE_BIN_OUT" >&2
"$EDGEE_BIN_OUT" --version >&2 || true
echo "$EDGEE_BIN_OUT"
