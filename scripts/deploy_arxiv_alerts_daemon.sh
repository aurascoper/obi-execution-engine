#!/bin/bash
# scripts/deploy_arxiv_alerts_daemon.sh — install / verify / smoke-test
# the daily arxiv check_alerts LaunchAgent.
#
# Idempotent: safe to run repeatedly. Will bootout-then-bootstrap the agent
# so changes to the .plist are picked up. Does NOT run check_alerts itself
# (that's --smoke or wait for 06:00).
#
# Usage:
#   bash scripts/deploy_arxiv_alerts_daemon.sh           # install/reload
#   bash scripts/deploy_arxiv_alerts_daemon.sh --smoke   # also fire one run now to verify end-to-end
#   bash scripts/deploy_arxiv_alerts_daemon.sh --status  # show launchctl + log tail
#   bash scripts/deploy_arxiv_alerts_daemon.sh --uninstall

set -euo pipefail

PLIST_NAME="com.aurascoper.arxiv-check-alerts"
PLIST_SRC="${HOME}/Library/LaunchAgents/${PLIST_NAME}.plist"
WRAPPER="$(cd "$(dirname "$0")"; pwd)/run_arxiv_check_alerts.sh"
LOG="${HOME}/.claude/arxiv-papers/check_alerts.log"
ERR="${HOME}/.claude/arxiv-papers/check_alerts.err"
DOMAIN="gui/$(id -u)"

usage() {
    cat <<USAGE
deploy_arxiv_alerts_daemon.sh — control the daily arxiv check_alerts agent

Commands:
  (no args)     Install or reload the LaunchAgent. Idempotent.
  --smoke       Install + fire one run immediately to verify end-to-end.
  --status      Print launchctl status + last 20 log lines.
  --uninstall   Stop and remove the LaunchAgent (keeps logs).

Files:
  plist:    ${PLIST_SRC}
  wrapper:  ${WRAPPER}
  log:      ${LOG}
  err:      ${ERR}

Schedule: daily at 06:00 America/Chicago (local wall-clock).
USAGE
}

require_files() {
    if [[ ! -f "${PLIST_SRC}" ]]; then
        echo "[ERROR] plist not found at ${PLIST_SRC}" >&2
        echo "[ERROR] expected the file to have been written before this script runs." >&2
        exit 2
    fi
    if [[ ! -x "${WRAPPER}" ]]; then
        echo "[INFO] making wrapper executable: ${WRAPPER}"
        chmod +x "${WRAPPER}"
    fi
    if [[ ! -d "${HOME}/.claude/arxiv-papers" ]]; then
        echo "[ERROR] arxiv MCP storage dir not found: ${HOME}/.claude/arxiv-papers" >&2
        echo "[ERROR] is the local arxiv-mcp-server registered? run: claude mcp list" >&2
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
    echo "[INFO] agent loaded. next fire will be at 06:00 America/Chicago."
}

show_status() {
    echo "─── launchctl status ───"
    if launchctl print "${DOMAIN}/${PLIST_NAME}" 2>&1 | head -40; then
        :
    else
        echo "[INFO] agent not currently loaded."
    fi
    echo ""
    echo "─── last 20 log lines (${LOG}) ───"
    if [[ -f "${LOG}" ]]; then
        tail -20 "${LOG}"
    else
        echo "[INFO] log not yet created. agent has never run."
    fi
    echo ""
    echo "─── last 10 stderr lines (${ERR}) ───"
    if [[ -f "${ERR}" && -s "${ERR}" ]]; then
        tail -10 "${ERR}"
    else
        echo "[INFO] stderr empty or missing."
    fi
}

smoke_test() {
    require_files
    reload_agent
    echo ""
    echo "─── firing immediate run via launchctl kickstart ───"
    launchctl kickstart -k "${DOMAIN}/${PLIST_NAME}"
    echo "[INFO] kickstart issued. Waiting up to 60s for completion..."
    local deadline=$(($(date +%s) + 60))
    while [[ $(date +%s) -lt $deadline ]]; do
        # If the process is no longer running, it's done.
        if ! pgrep -lf "run_arxiv_check_alerts.sh" >/dev/null 2>&1; then
            sleep 1
            break
        fi
        sleep 2
    done
    echo ""
    show_status
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
    echo "[INFO] uninstall complete. logs preserved at ${LOG} and ${ERR}."
}

case "${1:-}" in
    "")          reload_agent ;;
    --smoke)     smoke_test ;;
    --status)    show_status ;;
    --uninstall) uninstall ;;
    --help|-h)   usage ;;
    *)           echo "[ERROR] unknown arg: $1" >&2; usage; exit 2 ;;
esac
