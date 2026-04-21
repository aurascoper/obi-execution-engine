#!/bin/bash
# hl_supervisor.sh — keep hl_engine.py alive. Restart on exit with backoff.
#
# Usage:
#   nohup scripts/hl_supervisor.sh >> logs/hl_supervisor.log 2>&1 &
#
# Stop:
#   pkill -f hl_supervisor.sh  # then pkill -f hl_engine.py

set -u
cd "$(dirname "$0")/.." || exit 1

BACKOFF_MIN=10
BACKOFF_MAX=300
BACKOFF=$BACKOFF_MIN

while true; do
    if pgrep -f "hl_engine.py" >/dev/null; then
        sleep 30
        BACKOFF=$BACKOFF_MIN
        continue
    fi
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) hl_engine down — restarting (backoff=${BACKOFF}s)"
    # shellcheck disable=SC1091
    source env.sh
    nohup venv/bin/python hl_engine.py >> logs/hl_engine.stdout 2>&1 &
    disown
    sleep "$BACKOFF"
    # Doubling backoff until next up-check succeeds; resets to min once alive.
    BACKOFF=$(( BACKOFF * 2 ))
    if [ $BACKOFF -gt $BACKOFF_MAX ]; then BACKOFF=$BACKOFF_MAX; fi
done
