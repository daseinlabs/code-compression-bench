#!/usr/bin/env bash
# Launch the FORKED Edgee local gateway for the bench's `edgee` arm.
#
# Starts `edgee local-gateway` (the standalone, headless gateway — runs until
# killed, NO auth/TLS, local dev) listening on EDGEE_PORT, with the Anthropic
# upstream pointed at the run gateway via EDGEE_ANTHROPIC_UPSTREAM (the patch this
# fork adds). Claude Code's /v1/messages traffic is then compressed by Edgee's real
# CompressionLayer and forwarded to the run gateway -> Vertex.
#
#   chain:  Claude Code --(ANTHROPIC_BASE_URL=http://127.0.0.1:$EDGEE_PORT)-->
#           edgee local-gateway (compresses /v1/messages)
#           --(EDGEE_ANTHROPIC_UPSTREAM=$GATEWAY_URL)--> run gateway --> Vertex
#
# Usage:
#   GATEWAY_URL=http://127.0.0.1:NNNNN bash selfhost/edgee/launch.sh
#   EDGEE_PORT=8787 GATEWAY_URL=... bash selfhost/edgee/launch.sh
#
# The bench arm (`arms/edgee.py`) points ANTHROPIC_BASE_URL at http://127.0.0.1:$EDGEE_PORT
# (EDGEE_BASE_URL) and TCP-probes that port in ready().
set -euo pipefail

EDGEE_BIN="${EDGEE_BIN:-edgee}"
EDGEE_PORT="${EDGEE_PORT:-8787}"            # edgee local-gateway's real default port
EDGEE_BIND="${EDGEE_BIND:-127.0.0.1}"
GATEWAY_URL="${GATEWAY_URL:-${EDGEE_ANTHROPIC_UPSTREAM:-}}"

if [ -z "$GATEWAY_URL" ]; then
  echo "ERROR: set GATEWAY_URL (or EDGEE_ANTHROPIC_UPSTREAM) to the run gateway URL" >&2
  echo "       the runner prints it per run: 'its UPSTREAM must be http://127.0.0.1:<port>'" >&2
  exit 1
fi

command -v "$EDGEE_BIN" >/dev/null 2>&1 || {
  echo "ERROR: '$EDGEE_BIN' not found. Build the fork first: bash selfhost/edgee/build.sh" >&2
  exit 1
}

export EDGEE_ANTHROPIC_UPSTREAM="$GATEWAY_URL"
echo "[edgee] local-gateway on $EDGEE_BIND:$EDGEE_PORT  upstream=$EDGEE_ANTHROPIC_UPSTREAM" >&2
exec "$EDGEE_BIN" local-gateway --port "$EDGEE_PORT" --bind "$EDGEE_BIND"
