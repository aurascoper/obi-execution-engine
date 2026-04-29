#!/bin/bash
# scripts/hydrate_bars_daemon.sh — wrapper invoked by the bars-hydrate LaunchAgent.
#
# Runs scripts/hydrate_bars.py in rolling mode every 72h to keep the bars.sqlite
# cache fresh ahead of HL's 1m retention boundary (~3.6 days). Phase 2 of the
# data-ingestion plan; operator-approved 2026-04-29.
#
# Output: rolling per-run audit appended to logs/bar_hydrate.jsonl. Stderr
# (the human-readable per-symbol status line stream) is captured by launchd
# at ~/.claude/arxiv-papers/.. wait, NO — bar logs live under logs/ inside the
# repo so they're co-located with the engine logs. See StandardOutPath in
# scripts/com.aurascoper.bars-hydrate.plist.

set -euo pipefail

REPO_ROOT="/Users/aurascoper/Developer/live_trading"
TS_HUMAN="$(date '+%Y-%m-%d %H:%M:%S %Z')"
TS_UTC="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

cd "${REPO_ROOT}"
mkdir -p logs

{
  echo "═══════════════════════════════════════════════════════════════════"
  echo "bars-hydrate rolling run — ${TS_HUMAN} (${TS_UTC})"
  echo "═══════════════════════════════════════════════════════════════════"
} >> logs/bar_hydrate.stdout

# Hardcap at 30 minutes wall-clock — typical run is ~5 min for 96 symbols × 3
# intervals at 1 RPS.
if ! /usr/bin/timeout 1800 venv/bin/python3 scripts/hydrate_bars.py \
        --mode rolling \
        --rps 1.0 \
        2>>logs/bar_hydrate.stdout; then
    {
      echo ""
      echo "[ERROR] hydrate_bars.py invocation failed at ${TS_UTC}"
    } >> logs/bar_hydrate.stdout
    exit 1
fi

echo "" >> logs/bar_hydrate.stdout
exit 0
