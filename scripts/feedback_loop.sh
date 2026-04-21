#!/bin/bash
# feedback_loop.sh — run feedback_loop.py every 1h for 16 iterations (~16h total)
#
# Usage:
#   nohup scripts/feedback_loop.sh >> logs/feedback_loop.stdout 2>&1 &
#
# Stop with:
#   pkill -f "feedback_loop.sh"

set -u
cd "$(dirname "$0")/.." || exit 1

ITERATIONS=${ITERATIONS:-16}
INTERVAL=${INTERVAL:-3600}  # seconds

for i in $(seq 0 $((ITERATIONS - 1))); do
    echo "=== iteration $i at $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
    LOOP_ITER=$i venv/bin/python scripts/feedback_loop.py || echo "iter $i failed with $?"
    if [ $i -lt $((ITERATIONS - 1)) ]; then
        sleep "$INTERVAL"
    fi
done

echo "=== feedback loop complete at $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
