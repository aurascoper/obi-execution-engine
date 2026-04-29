#!/bin/bash
# scripts/deploy_bars_hydrate_daemon.sh — install / verify / smoke / uninstall
# the every-72h bars hydrate LaunchAgent.
#
# Mirrors the arxiv-check-alerts deployer pattern (commit b3505d4).
#
# Usage:
#   bash scripts/deploy_bars_hydrate_daemon.sh           # install/reload
#   bash scripts/deploy_bars_hydrate_daemon.sh --seed    # install + run seed pull NOW (Phase 1)
#   bash scripts/deploy_bars_hydrate_daemon.sh --smoke   # install + fire one rolling run via launchctl kickstart
#   bash scripts/deploy_bars_hydrate_daemon.sh --status
#   bash scripts/deploy_bars_hydrate_daemon.sh --uninstall

set -euo pipefail

PLIST_NAME="com.aurascoper.bars-hydrate"
PLIST_SRC="${HOME}/Library/LaunchAgents/${PLIST_NAME}.plist"
WRAPPER="$(cd "$(dirname "$0")"; pwd)/hydrate_bars_daemon.sh"
HYDRATE_SCRIPT="$(cd "$(dirname "$0")"; pwd)/hydrate_bars.py"
REPO_ROOT="/Users/aurascoper/Developer/live_trading"
LOG="${REPO_ROOT}/logs/bar_hydrate.stdout"
DAEMON_OUT="${REPO_ROOT}/logs/bar_hydrate.daemon.out"
DAEMON_ERR="${REPO_ROOT}/logs/bar_hydrate.daemon.err"
DOMAIN="gui/$(id -u)"

usage() {
    cat <<USAGE
deploy_bars_hydrate_daemon.sh — control the every-72h bars-hydrate agent

Commands:
  (no args)     Install or reload the LaunchAgent. Idempotent.
  --seed        Install + run scripts/hydrate_bars.py --mode seed in foreground
                (Phase 1 initial pull — recommended on first deployment).
  --smoke       Install + fire one rolling run NOW via launchctl kickstart.
  --status      Print launchctl status + last 30 audit lines + bar counts.
  --uninstall   Stop and remove the LaunchAgent (keeps logs and bars.sqlite).

Files:
  plist:    ${PLIST_SRC}
  wrapper:  ${WRAPPER}
  script:   ${HYDRATE_SCRIPT}
  log:      ${LOG}
  audit:    ${REPO_ROOT}/logs/bar_hydrate.jsonl

Schedule: every 259200s (72h, front-running HL 1m retention).
USAGE
}

require_files() {
    if [[ ! -f "${PLIST_SRC}" ]]; then
        echo "[ERROR] plist not found at ${PLIST_SRC}" >&2
        exit 2
    fi
    if [[ ! -x "${WRAPPER}" ]]; then
        echo "[INFO] making wrapper executable: ${WRAPPER}"
        chmod +x "${WRAPPER}"
    fi
    if [[ ! -f "${HYDRATE_SCRIPT}" ]]; then
        echo "[ERROR] hydrate script not found: ${HYDRATE_SCRIPT}" >&2
        exit 2
    fi
    if [[ ! -d "${REPO_ROOT}/venv" ]]; then
        echo "[ERROR] expected ${REPO_ROOT}/venv with hyperliquid SDK installed" >&2
        exit 2
    fi
}

reload_agent() {
    require_files
    if launchctl print "${DOMAIN}/${PLIST_NAME}" >/dev/null 2>&1; then
        echo "[INFO] agent already loaded — bootout first"
        launchctl bootout "${DOMAIN}/${PLIST_NAME}" || true
    fi
    echo "[INFO] bootstrap ${PLIST_SRC} into ${DOMAIN}"
    launchctl bootstrap "${DOMAIN}" "${PLIST_SRC}"
    echo "[INFO] agent loaded. next fire in 72h."
}

seed_pull() {
    require_files
    reload_agent
    echo ""
    echo "─── Phase 1 seed pull (foreground) ───"
    echo "─── this is the initial population of bars.sqlite ───"
    echo ""
    cd "${REPO_ROOT}"
    venv/bin/python3 scripts/hydrate_bars.py --mode seed --rps 1.0
    echo ""
    show_status
}

smoke_test() {
    require_files
    reload_agent
    echo ""
    echo "─── firing immediate run via launchctl kickstart ───"
    launchctl kickstart -k "${DOMAIN}/${PLIST_NAME}"
    echo "[INFO] kickstart issued. Run is async; use --status to poll."
    sleep 3
    show_status
}

show_status() {
    echo "─── launchctl status ───"
    if launchctl print "${DOMAIN}/${PLIST_NAME}" 2>/dev/null | head -20; then
        :
    else
        echo "[INFO] agent not currently loaded."
    fi
    echo ""
    echo "─── last 30 audit lines (logs/bar_hydrate.jsonl) ───"
    if [[ -f "${REPO_ROOT}/logs/bar_hydrate.jsonl" ]]; then
        tail -30 "${REPO_ROOT}/logs/bar_hydrate.jsonl"
    else
        echo "[INFO] audit not yet created. agent has never run."
    fi
    echo ""
    echo "─── bars.sqlite stats ───"
    if [[ -s "${REPO_ROOT}/data/cache/bars.sqlite" ]]; then
        cd "${REPO_ROOT}"
        venv/bin/python3 -c "
from data.bar_cache import BarCache
import collections
c = BarCache('data/cache/bars.sqlite')
rows = c.stats()
by_iv = collections.Counter()
for r in rows: by_iv[r['interval']] += r['bars']
total = sum(by_iv.values())
print(f'  intervals: {dict(by_iv)}')
print(f'  total rows: {total:,}')
print(f'  symbols with data: {len(set(r[\"symbol\"] for r in rows))}')
c.close()
" 2>/dev/null || echo "  (could not read bars.sqlite — schema mismatch or empty)"
    else
        echo "  bars.sqlite is empty or missing — run --seed to populate"
    fi
}

uninstall() {
    if launchctl print "${DOMAIN}/${PLIST_NAME}" >/dev/null 2>&1; then
        echo "[INFO] booting out ${PLIST_NAME}"
        launchctl bootout "${DOMAIN}/${PLIST_NAME}" || true
    fi
    if [[ -f "${PLIST_SRC}" ]]; then
        echo "[INFO] removing ${PLIST_SRC}"
        rm -f "${PLIST_SRC}"
    fi
    echo "[INFO] uninstall complete. bars.sqlite and logs preserved."
}

case "${1:-}" in
    "")          reload_agent ;;
    --seed)      seed_pull ;;
    --smoke)     smoke_test ;;
    --status)    show_status ;;
    --uninstall) uninstall ;;
    --help|-h)   usage ;;
    *)           echo "[ERROR] unknown arg: $1" >&2; usage; exit 2 ;;
esac
