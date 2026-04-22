#!/usr/bin/env bash
# Push a sanitized monitor snapshot to the public repo so cloud agents can read it.
# Safe by construction: only the aggregated monitor report is published; raw logs
# and anything matching sensitive regexes are redacted.
set -euo pipefail
cd "$(dirname "$0")/.."

SRC="logs/monitor/latest.md"
OUT_DIR="snapshots"
OUT="$OUT_DIR/latest.md"
STATE_SRC="logs/monitor/_state.json"
STATE_OUT="$OUT_DIR/state.json"

[[ -f "$SRC" ]] || { echo "no monitor report at $SRC"; exit 1; }
mkdir -p "$OUT_DIR"

# Sanitizer: redact any 40-hex Ethereum-style address + any key=value pair whose
# key contains private_key / api_key / secret / wallet / mnemonic (case-insensitive).
sanitize() {
  sed -E \
    -e 's/0x[a-fA-F0-9]{40}/0xREDACTED/g' \
    -e 's/(private_key|api_key|secret|mnemonic|wallet)[[:space:]]*[:=][[:space:]]*"[^"]*"/\1: "REDACTED"/gI' \
    -e 's/(private_key|api_key|secret|mnemonic|wallet)[[:space:]]*[:=][[:space:]]*[^[:space:],}]+/\1=REDACTED/gI'
}

{
  echo "<!-- generated_at: $(date -u +%Y-%m-%dT%H:%M:%SZ) -->"
  echo "<!-- source: logs/monitor/latest.md (sanitized) -->"
  echo
  sanitize < "$SRC"
} > "$OUT"

if [[ -f "$STATE_SRC" ]]; then
  sanitize < "$STATE_SRC" > "$STATE_OUT"
fi

# Only commit/push if the snapshot changed.
git add "$OUT_DIR"
if git diff --cached --quiet; then
  echo "snapshot unchanged — skipping commit"
  exit 0
fi

git commit -m "snapshot: monitor update $(date -u +%Y-%m-%dT%H:%MZ)" --quiet
git push origin HEAD --quiet
echo "snapshot pushed"
