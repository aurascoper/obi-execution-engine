"""
config/risk_params.py — Hardcoded circuit breaker thresholds.
These are constants — not configurable at runtime.
"""

import os as _os


def _is_live() -> bool:
    return _os.environ.get("EXECUTION_MODE", "PAPER").upper().strip() == "LIVE"


# --- Daily P&L Circuit Breakers ---
MAX_DAILY_DRAWDOWN_PCT = 0.02  # Hard halt if equity drops 2% intraday
MAX_DAILY_LOSS_DOLLARS = 100.0 if _is_live() else 500.0  # $100 live / $500 paper

# --- Per-Order Size Caps ---
MAX_ORDER_NOTIONAL = 550.00 if _is_live() else 2_000.00  # $550 live / $2000 paper
MAX_CONTRACTS_PER_LEG = 10  # Options: max contracts per leg (circuit breaker)
MAX_SHARES_PER_ORDER = 500  # Equities: max shares per order

# --- Options Engine Caps ---
# Budget per opening trade (total debit = premium × 100 × qty).
# At $445 equity: $110 = ~25% of account per trade — sized for HPE near-ATM puts.
MAX_OPTIONS_BUDGET = 110.00 if _is_live() else 1_100.00
MAX_OPTIONS_POSITIONS = 3  # max concurrent open options positions
MAX_CONTRACTS_PER_TRADE = 1  # single contract per signal (small account)

# --- Portfolio-Level Caps ---
MAX_OPEN_POSITIONS = 10  # Total open positions allowed
MAX_POSITION_PCT_EQUITY = 0.25  # No single position > 25% of equity (unused today; doc)

# --- Momentum Strategy Caps ---
MOMENTUM_MAX_POSITIONS = 5  # max concurrent momentum positions across all venues

# --- Per-Symbol Caps ---
SYMBOL_CAPS = {
    "BTC/USD": 5_000.0,
    "ETH/USD": 3_000.0,
    "SOL/USD": 3_000.0,
    "DOGE/USD": 1_000.0,
    "AVAX/USD": 1_000.0,
    "LINK/USD": 1_000.0,
    "SHIB/USD": 500.0,
    "VOO": 5_000.0,
    "NVDA": 3_000.0,
    "SPY": 5_000.0,
    # HIP-3 equity perps (TradeXYZ on Hyperliquid) — screener_hip3.py top 20
    # Bumped $100→$200 2026-04-20 to deploy $366 free USDC (60% unused).
    "xyz:HIMS/USD": 200.0,
    "xyz:HOOD/USD": 200.0,
    "xyz:CRCL/USD": 200.0,
    "xyz:ORCL/USD": 200.0,
    "xyz:EWY/USD": 200.0,
    "xyz:XYZ100/USD": 200.0,
    "xyz:COIN/USD": 200.0,
    "xyz:CRWV/USD": 200.0,
    "xyz:TSLA/USD": 200.0,
    "xyz:CL/USD": 200.0,
    "xyz:SNDK/USD": 200.0,
    "xyz:MSTR/USD": 200.0,
    "xyz:SKHX/USD": 200.0,
    "xyz:MSFT/USD": 200.0,
    "xyz:MU/USD": 200.0,
    "xyz:SP500/USD": 200.0,
    "xyz:AMD/USD": 200.0,
    "xyz:PLTR/USD": 200.0,
    "xyz:BRENTOIL/USD": 200.0,
    "xyz:INTC/USD": 200.0,
    "xyz:RIVN/USD": 50.0,  # unchanged: CoinCodex $12 forecast hedge
    # ── HIP-3 expansion (screened 2026-04-19) — commodities + mega-caps ──
    "xyz:GOLD/USD": 200.0,
    "xyz:SILVER/USD": 200.0,
    "xyz:NATGAS/USD": 200.0,
    "xyz:COPPER/USD": 200.0,
    "xyz:PLATINUM/USD": 200.0,
    "xyz:TSM/USD": 200.0,
    "xyz:AMZN/USD": 200.0,
    "xyz:GOOGL/USD": 200.0,
    "xyz:META/USD": 200.0,
    "xyz:NVDA/USD": 200.0,
    # ── HIP-3 additions (env.sh 2026-04-20) — mega-caps, FX, commodities, indices ──
    "xyz:AAPL/USD": 200.0,
    "xyz:LLY/USD": 200.0,
    "xyz:NFLX/USD": 200.0,
    "xyz:COST/USD": 200.0,
    "xyz:BABA/USD": 200.0,
    "xyz:RKLB/USD": 200.0,
    "xyz:MRVL/USD": 200.0,
    "xyz:VIX/USD": 200.0,
    "xyz:DXY/USD": 200.0,
    "xyz:EUR/USD": 200.0,
    "xyz:JPY/USD": 200.0,
    "xyz:JP225/USD": 200.0,
    "xyz:XLE/USD": 200.0,
    "xyz:PALLADIUM/USD": 200.0,
    "xyz:URANIUM/USD": 200.0,
    "xyz:WHEAT/USD": 200.0,
    "xyz:CORN/USD": 200.0,
    # ── para (crypto dominance indices) — unfunded today but parse-safe ──
    "para:BTCD/USD": 200.0,
    "para:OTHERS/USD": 200.0,
    "para:TOTAL2/USD": 200.0,
    # ── Momentum / trend-following candidates (screened 2026-04-17) ────────
    "AMZN": 1_500.0 if _is_live() else 1_500.0,
    "NKE": 1_500.0 if _is_live() else 1_500.0,
    "INTC": 1_500.0 if _is_live() else 1_500.0,
    "HPE": 1_500.0 if _is_live() else 1_500.0,
    "CSCO": 1_500.0 if _is_live() else 1_500.0,
}

# --- API Rate Limiting ---
MAX_ORDERS_PER_MINUTE = 30  # Alpaca limit is 200/min; stay well under
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 60.0
BACKOFF_MULTIPLIER = 2.0

# --- Slippage Alert Threshold ---
SLIPPAGE_ALERT_PCT = 0.005  # Log warning if fill > 0.5% from expected
