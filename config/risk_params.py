"""
config/risk_params.py — Hardcoded circuit breaker thresholds.
These are constants — not configurable at runtime.
"""

# --- Daily P&L Circuit Breakers ---
MAX_DAILY_DRAWDOWN_PCT   = 0.02    # Hard halt if equity drops 2% intraday
MAX_DAILY_LOSS_DOLLARS   = 500.0   # Absolute dollar floor (whichever hits first)

# --- Per-Order Size Caps ---
# Intentionally set to $5.00 during live validation phase.
# Raise only after latency + slippage are verified on real fills.
MAX_ORDER_NOTIONAL       = 15.00   # Alpaca minimum order is $10; set to $15 with margin
MAX_CONTRACTS_PER_LEG    =  10     # Options: max contracts per leg
MAX_SHARES_PER_ORDER     = 500     # Equities: max shares per order

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
}

# --- API Rate Limiting ---
MAX_ORDERS_PER_MINUTE    =  30     # Alpaca limit is 200/min; stay well under
BACKOFF_BASE_SECONDS     =   1.0
BACKOFF_MAX_SECONDS      =  60.0
BACKOFF_MULTIPLIER       =   2.0

# --- Slippage Alert Threshold ---
SLIPPAGE_ALERT_PCT       = 0.005   # Log warning if fill > 0.5% from expected
