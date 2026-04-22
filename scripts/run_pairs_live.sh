#!/usr/bin/env bash
# scripts/run_pairs_live.sh — live-mode pairs launcher.
#
# Opposite of run_pairs_shadow.sh: PAIRS_SHADOW=0 + DRY_RUN=0 so pair entries
# become real Hyperliquid orders. Uses venv/bin/python for deps, sources
# env.sh for wallet creds + risk-param overrides.
set -euo pipefail
cd "$(dirname "$0")/.."

# Source env.sh first (wallet, KELLY_K, etc.)
# shellcheck disable=SC1091
source env.sh

# Explicit live-mode overrides — take precedence over anything env.sh set.
export PAIRS_SHADOW=0
export DRY_RUN=0
: "${PAIRS_HEDGE_MODE:=ols}"
: "${HEDGENET_WEIGHT_DIR:=models/weights}"
: "${HEDGENET_INTERVAL:=1h}"
export PAIRS_HEDGE_MODE HEDGENET_WEIGHT_DIR HEDGENET_INTERVAL

echo "[pairs-live] DRY_RUN=$DRY_RUN PAIRS_HEDGE_MODE=$PAIRS_HEDGE_MODE PAIRS_SHADOW=$PAIRS_SHADOW KELLY_K=${KELLY_K:-unset} KELLY_PAIRS=${KELLY_PAIRS:-unset}"
exec venv/bin/python -u hl_pairs.py "$@"
