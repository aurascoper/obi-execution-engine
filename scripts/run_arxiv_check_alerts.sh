#!/bin/bash
# scripts/run_arxiv_check_alerts.sh — daily arxiv watch poller.
#
# Invoked by ~/Library/LaunchAgents/com.aurascoper.arxiv-check-alerts.plist
# at 06:00 America/Chicago. Spawns a one-shot claude headless session that
# calls the locally-registered arxiv MCP server's check_alerts tool, which
# reads ~/.claude/arxiv-papers/watched_topics.json, queries arxiv for new
# papers since each topic's last_checked, and updates last_checked on success.
#
# Output goes to STDOUT (captured by launchd's StandardOutPath) plus an
# explicit timestamped append to ~/.claude/arxiv-papers/check_alerts.log.
#
# Cost ceiling: --max-budget-usd 0.50 — claude -p will refuse to spend more.
# Tool allowlist: only mcp__arxiv__check_alerts. Refuses to use any other tool
# even if the model is tempted.
#
# Manual test: bash scripts/run_arxiv_check_alerts.sh
# Manual reload: launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.aurascoper.arxiv-check-alerts.plist

set -euo pipefail

LOG_DIR="/Users/aurascoper/.claude/arxiv-papers"
LOG_FILE="${LOG_DIR}/check_alerts.log"
TS_HUMAN="$(date '+%Y-%m-%d %H:%M:%S %Z')"
TS_UTC="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

mkdir -p "${LOG_DIR}"

{
  echo "═══════════════════════════════════════════════════════════════════"
  echo "arxiv check_alerts run — ${TS_HUMAN} (${TS_UTC})"
  echo "═══════════════════════════════════════════════════════════════════"
} >> "${LOG_FILE}"

# Drive the local MCP via claude headless mode. --bare keeps the session
# minimal (no plugins, no auto-memory, no CLAUDE.md auto-discovery) so the
# only thing the model can do is the one allowed tool.
#
# We do NOT pass --bare here because --bare disables MCP server resolution
# from the global config. Instead, we constrain via --allowed-tools and a
# tight prompt + budget cap.
PROMPT='Use the mcp__arxiv__check_alerts tool with no arguments to poll all my saved arxiv watch topics. After the tool returns, write a compact human-readable digest of new papers per topic to stdout in this format:

# arxiv check_alerts digest (UTC: TIMESTAMP)

## Topic: <topic string, truncated to 80 chars>
- arxiv_id  title (≤120 chars)  authors (first 2 + et al.)  YYYY-MM-DD

If a topic returned no new papers, list it under a final "## No new papers since last check" section. If the tool errors, print only the error message and exit. Do not run any other tools. Do not call check_alerts more than once. Do not search, fetch, or store anything else.'

# Hardcap at 60s wall-clock for the whole claude invocation. Daily polls
# should finish in 5-15s; anything longer is a stuck session.
if ! /usr/bin/timeout 60 /opt/homebrew/bin/claude \
        -p "${PROMPT}" \
        --allowed-tools "mcp__arxiv__check_alerts" \
        --output-format text \
        --max-budget-usd 0.50 \
        2>&1 | tee -a "${LOG_FILE}"; then
    {
      echo ""
      echo "[ERROR] claude headless invocation failed at ${TS_UTC}"
      echo "[ERROR] exit code from previous pipeline (claude+timeout): non-zero"
    } >> "${LOG_FILE}"
    exit 1
fi

echo "" >> "${LOG_FILE}"
exit 0
