"""
config/risk_params.py — Hardcoded circuit breaker thresholds.
These are constants — not configurable at runtime.
"""

import os as _os

def _is_live() -> bool:
    return _os.environ.get("EXECUTION_MODE", "PAPER").upper().strip() == "LIVE"

# --- Daily P&L Circuit Breakers ---
MAX_DAILY_DRAWDOWN_PCT   = 0.02    # Hard halt if equity drops 2% intraday
MAX_DAILY_LOSS_DOLLARS   =  50.0 if _is_live() else 500.0   # $50 live / $500 paper

# --- Per-Order Size Caps ---
MAX_ORDER_NOTIONAL       = 550.00 if _is_live() else 2_000.00  # $550 live / $2000 paper
MAX_CONTRACTS_PER_LEG    =  10     # Options: max contracts per leg (circuit breaker)
MAX_SHARES_PER_ORDER     = 500     # Equities: max shares per order

# --- Options Engine Caps ---
# Budget per opening trade (total debit = premium × 100 × qty).
# At $445 equity: $110 = ~25% of account per trade — sized for HPE near-ATM puts.
MAX_OPTIONS_BUDGET       = 110.00 if _is_live() else 1_100.00
MAX_OPTIONS_POSITIONS    =   3     # max concurrent open options positions
MAX_CONTRACTS_PER_TRADE  =   1     # single contract per signal (small account)

# --- Portfolio-Level Caps ---
MAX_OPEN_POSITIONS       =  10     # Total open positions allowed
MAX_POSITION_PCT_EQUITY  = 0.10    # No single position > 10% of equity

# --- Per-Symbol Caps ---
SYMBOL_CAPS = {
    "BTC/USD":  5_000.0,
    "ETH/USD":  3_000.0,
    "SOL/USD":  3_000.0,
    "DOGE/USD": 1_000.0,
    "AVAX/USD": 1_000.0,
    "LINK/USD": 1_000.0,
    "SHIB/USD":   500.0,
    "VOO":      5_000.0,
    "NVDA":     3_000.0,
    "SPY":      5_000.0,
    # HIP-3 equity perps (TradeXYZ on Hyperliquid) — screener_hip3.py top 20
    "xyz:HIMS/USD":     100.0,
    "xyz:HOOD/USD":     100.0,
    "xyz:CRCL/USD":     100.0,
    "xyz:ORCL/USD":     100.0,
    "xyz:EWY/USD":      100.0,
    "xyz:XYZ100/USD":   100.0,
    "xyz:COIN/USD":     100.0,
    "xyz:CRWV/USD":     100.0,
    "xyz:TSLA/USD":     100.0,
    "xyz:CL/USD":       100.0,
    "xyz:SNDK/USD":     100.0,
    "xyz:MSTR/USD":     100.0,
    "xyz:SKHX/USD":     100.0,
    "xyz:MSFT/USD":     100.0,
    "xyz:MU/USD":       100.0,
    "xyz:SP500/USD":    100.0,
    "xyz:AMD/USD":      100.0,
    "xyz:PLTR/USD":     100.0,
    "xyz:BRENTOIL/USD": 100.0,
    "xyz:INTC/USD":     100.0,
}

# --- API Rate Limiting ---
MAX_ORDERS_PER_MINUTE    =  30     # Alpaca limit is 200/min; stay well under
BACKOFF_BASE_SECONDS     =   1.0
BACKOFF_MAX_SECONDS      =  60.0
BACKOFF_MULTIPLIER       =   2.0

# --- Slippage Alert Threshold ---
SLIPPAGE_ALERT_PCT       = 0.005   # Log warning if fill > 0.5% from expected
