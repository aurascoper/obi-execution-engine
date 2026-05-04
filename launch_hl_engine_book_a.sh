#!/usr/bin/env bash
# Engine launcher for Book A (engine subaccount = 0xdae9...f2ef).
#
# Inherits the FULL live universe + execution config from env.sh
# (HL_UNIVERSE, HIP3_DEXS, HIP3_UNIVERSE, HIP3_LEVERAGE, EXECUTION_MODE,
# EXECUTION_STYLE, ALPACA_TRADING_MODE).
#
# Overrides ONLY:
#   1. The three HL routing fields (signer, query target, vault routing)
#   2. First-launch dust caps so $250 capital + full universe is bounded
#
# .env (Book B credentials) is untouched. All ad-hoc Book B scripts continue
# to read .env and target the master account 0x32D178...6C048.
#
# After clean acceptance of the first soak, loosen or remove the dust caps
# in a controlled second step.
set -euo pipefail

cd "$(dirname "$0")"

# ── 1. Pre-flight checks ────────────────────────────────────────────────────
if [[ ! -f .env.bookA ]]; then
    echo "ERROR: .env.bookA not found in $(pwd). Aborting."
    exit 2
fi
if [[ ! -f env.sh ]]; then
    echo "ERROR: env.sh not found in $(pwd). Aborting."
    exit 2
fi

# Refuse to launch if an engine is already running — prevents accidentally
# starting two engines on the same wallet/subaccount.
if pgrep -fl "venv/bin/python3 hl_engine.py" >/dev/null 2>&1; then
    echo "ERROR: hl_engine.py is already running. Stop it first:"
    echo "    pkill -f 'venv/bin/python3 hl_engine.py'"
    exit 2
fi

# ── 2. Load credentials + base config ───────────────────────────────────────
# .env.bookA must define (operator-chosen names, lowercase OK):
#   subaccount_agent_pk         — the agent private key (only real secret)
#   subaccount_agent_address    — the agent address (used for sanity print)
# The subaccount address itself is hardcoded below — it's public and
# fixed for this launcher.
# shellcheck disable=SC1091
source .env.bookA
# shellcheck disable=SC1091
source env.sh

# Fail loud if the agent key is missing
: "${subaccount_agent_pk:?subaccount_agent_pk not set in .env.bookA}"

# ── 3. Override ONLY the three HL routing fields ────────────────────────────
# Book A subaccount address — public, fixed for this launcher.
# (If you ever provision a Book A2, copy this script and update the address.)
BOOKA_SUBACCOUNT="0xdae99e77b9859a1526782e3815253e8f09c1f2ef"

export HL_WALLET_ADDRESS="$BOOKA_SUBACCOUNT"
export HL_PRIVATE_KEY="$subaccount_agent_pk"
export HL_VAULT_ADDRESS="$BOOKA_SUBACCOUNT"

# ── 4. First-launch dust caps (Option B from the cutover plan) ─────────────
# These are env vars the engine actually honors:
#   NOTIONAL_PER_TRADE_OVERRIDE — sizing override (line 396 hl_engine.py).
#                                 Was Bug A; now correctly takes precedence
#                                 over PER_PAIR_NOTIONAL.
#   MAX_NEW_ENTRIES_PER_SESSION — per-engine-instance ceiling on entries.
#   HL_SESSION_LOSS_GUARD_USD   — halt new entries when session realized
#                                 PnL hits -X dollars.
# Plus the engine's built-in MAX_NET_NOTIONAL default of 200 (line 136)
# already provides an entry-side net cap at $200; we don't override it.
# 12 (not 10) gives ~20% headroom above HL's $10 venue minimum after qty
# rounding to szDecimals — at $10 flat, rounding consistently lands the
# actual notional at $9.6-$9.98 → exchange rejects with "minimum value of $10"
# → entry_rollback fires → zero fills accumulate. $12 nominal × 20 max
# entries = $240 max gross, still inside $250 Book A funding.
export NOTIONAL_PER_TRADE_OVERRIDE=12
export MAX_NEW_ENTRIES_PER_SESSION=20
export HL_SESSION_LOSS_GUARD_USD=50

# ── 5. Sanity print before launch (operator can ctrl-C if anything wrong) ──
echo
echo "=================== Book A engine launch ==================="
echo "  subaccount (HL_WALLET_ADDRESS)  = $HL_WALLET_ADDRESS"
echo "  vault_address (HL_VAULT_ADDRESS) = $HL_VAULT_ADDRESS"
echo "  agent (subaccount_agent_address) = ${subaccount_agent_address:-<not set in .env.bookA>}"
echo "  HL_PRIVATE_KEY                   = <set, length=${#HL_PRIVATE_KEY}>"
echo
echo "  Inherited from env.sh:"
echo "    EXECUTION_MODE = $EXECUTION_MODE"
echo "    EXECUTION_STYLE = $EXECUTION_STYLE"
echo "    HIP3_LEVERAGE = $HIP3_LEVERAGE"
echo "    HL_UNIVERSE coins = $(echo "$HL_UNIVERSE" | tr ',' '\n' | wc -l | tr -d ' ')"
echo "    HIP3_UNIVERSE perps = $(echo "$HIP3_UNIVERSE" | tr ',' '\n' | wc -l | tr -d ' ')"
echo "    HIP3_DEXS = $HIP3_DEXS"
echo
echo "  First-launch dust caps:"
echo "    NOTIONAL_PER_TRADE_OVERRIDE = \$$NOTIONAL_PER_TRADE_OVERRIDE / trade"
echo "    MAX_NEW_ENTRIES_PER_SESSION = $MAX_NEW_ENTRIES_PER_SESSION entries"
echo "    HL_SESSION_LOSS_GUARD_USD   = -\$$HL_SESSION_LOSS_GUARD_USD halt threshold"
echo "============================================================"
echo "Launching in 5 seconds — ctrl-C to abort"
sleep 5

# ── 6. Launch ───────────────────────────────────────────────────────────────
nohup caffeinate -i venv/bin/python3 hl_engine.py \
    > logs/hl_engine.stdout 2>&1 &
ENGINE_PID=$!
echo "Engine launched in Book A. PID: $ENGINE_PID"
echo
echo "Verify after ~30s with:"
echo "    tail -50 logs/hl_engine.jsonl | grep -E '\"event\":\"hl_manager_vault_routing\"|\"event\":\"hl_manager_initialized\"'"
echo
echo "Should see vault_address=$HL_VAULT_ADDRESS and wallet=$HL_WALLET_ADDRESS"
