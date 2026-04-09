"""
strategy/signals.py — Hybrid OBI + Mean-Reversion Signal Engine
Long-Only Crypto  |  ~$150 account  |  No shorting  |  No equities (PDT)

╔══════════════════════════════════════════════════════════════════╗
║  Mathematical Basis                                              ║
║                                                                  ║
║  Mean-Reversion z-score  [Avellaneda & Lee, 2010]:              ║
║    z_t = (close_t − μ_w) / σ_w                                  ║
║    where μ_w, σ_w are rolling mean/std over a W-bar window      ║
║                                                                  ║
║  Order-Book Imbalance    [Cartea et al., 2018]:                  ║
║    ρ_t = (V^b_t − V^a_t) / (V^b_t + V^a_t)                     ║
║    where V^b, V^a are aggregated bid/ask depth at top N levels   ║
║                                                                  ║
║  Entry:  z_t < −Z_ENTRY  AND  ρ_t > OBI_THETA                  ║
║    → price is statistically oversold AND buy pressure confirms   ║
║                                                                  ║
║  Exit:   z_t > −Z_EXIT                                          ║
║    → mean-reversion has completed sufficiently                   ║
╚══════════════════════════════════════════════════════════════════╝

Two public update paths (called by the event loop):
  signals.evaluate(bar)        → per bar; may return order dict or None
  signals.update_orderbook(ob) → per L2 snapshot; updates OBI state only

Output dict format (required by order_manager.py):
  {
    "symbol":   str,           e.g. "ETH/USD"
    "side":     OrderSide.BUY,
    "qty":      float,         fractional crypto units
    "limit_px": float,         aggressive limit ≈ best ask
    "notional": float,         actual $ deployed
  }
"""

from __future__ import annotations

import math
import numpy as np
import structlog
from alpaca.trading.enums import OrderSide

from config.risk_params import MAX_ORDER_NOTIONAL, SYMBOL_CAPS

log = structlog.get_logger(__name__)

# ── Strategy Parameters ────────────────────────────────────────────────────────
SYMBOLS             = ["ETH/USD", "BTC/USD"]
WINDOW              = 60       # rolling bars for z-score (Avellaneda & Lee: 60-day)
Z_ENTRY             = -1.25   # enter long when z < Z_ENTRY (oversold)
Z_EXIT              = -0.50   # exit long when z reverts above Z_EXIT
OBI_THETA           = 0.00    # any net buy pressure confirms entry (bid depth > ask depth)
OBI_LEVELS          = 5       # top N order-book levels to aggregate depth
LIMIT_SLIPPAGE      = 0.0010  # limit price = close × (1 + LIMIT_SLIPPAGE)
NOTIONAL_PER_TRADE  = 15.0    # $ per trade — Alpaca $10 minimum; $15 gives headroom

# Alpaca minimum qty precision per symbol (fractional crypto)
# BTC/ETH at current prices need 6+ decimals to express sub-$5 notional.
# Alpaca supports up to 9 decimal places for crypto fractional orders.
_QTY_DECIMALS: dict[str, int] = {
    "ETH/USD":  6,   # 0.000001 ETH (~$0.002 at $2000)
    "BTC/USD":  6,   # 0.000001 BTC (~$0.08 at $80000)
    "SOL/USD":  4,   # 0.0001 SOL (~$0.01 at $130)
    "DOGE/USD": 2,   # 0.01 DOGE (~$0.001 at $0.08)
    "AVAX/USD": 4,   # 0.0001 AVAX (~$0.002 at $20)
    "LINK/USD": 4,   # 0.0001 LINK (~$0.001 at $13)
    "SHIB/USD": 0,   # whole SHIB units (~$0.000012/SHIB → ~1.25M units per $15)
}


# ── Rolling Circular Buffer ────────────────────────────────────────────────────
class _RollingBuffer:
    """
    Fixed-size circular buffer backed by a contiguous float64 numpy array.

    push()   — O(1) scalar write, no allocation
    mean()   — O(W) numpy SIMD reduction (W=60 ≈ trivial on M4)
    std()    — O(W) numpy SIMD reduction
    zscore() — single call combining both + normalization

    Memory: 60 × 8 bytes = 480 bytes per symbol — entirely in L1 cache.
    """

    __slots__ = ("_buf", "_idx", "_count", "_size")

    def __init__(self, size: int) -> None:
        self._buf   = np.empty(size, dtype=np.float64)
        self._idx   = 0
        self._count = 0
        self._size  = size

    def push(self, val: float) -> None:
        """Write new value; wraps around when full."""
        self._buf[self._idx] = val
        self._idx = (self._idx + 1) % self._size
        if self._count < self._size:
            self._count += 1

    @property
    def is_full(self) -> bool:
        return self._count == self._size

    # Internal: returns view of filled portion — zero copy
    def _active(self) -> np.ndarray:
        return self._buf[: self._count]

    def zscore(self, current: float) -> float | None:
        """
        Returns z_t = (current − μ_w) / σ_w, or None if window not yet full.
        Uses sample std (ddof=1) consistent with pandas rolling().std().
        """
        if not self.is_full:
            return None
        a   = self._active()                  # contiguous float64 view
        mu  = np.mean(a)                      # single SIMD pass
        sig = np.std(a, ddof=1)               # second SIMD pass
        if sig < 1e-10:                       # flat price — no signal
            return None
        return float((current - mu) / sig)


# ── Per-Symbol Runtime State ───────────────────────────────────────────────────
class _SymbolState:
    """All mutable state for one symbol; no heap allocation after __init__."""

    __slots__ = (
        "symbol",
        "price_buf",     # _RollingBuffer of close prices
        "obi",           # latest ρ_t scalar (updated by update_orderbook)
        "best_ask",      # latest best ask (for precise limit price)
        "in_position",   # bool: True if a long is currently open
        "entry_px",      # price at which the current trade was entered
    )

    def __init__(self, symbol: str, window: int) -> None:
        self.symbol       = symbol
        self.price_buf    = _RollingBuffer(window)
        self.obi          = 0.0
        self.best_ask     = float("nan")
        self.in_position  = False
        self.entry_px     = float("nan")


# ── Signal Engine ──────────────────────────────────────────────────────────────
class SignalEngine:
    """
    Stateful signal engine.  Thread-safety: single-threaded asyncio; no locks needed.

    Usage (from live_engine.py _strategy_loop):
        msg = await bar_q.get()
        if msg["type"] == "bar":
            signal = signals.evaluate(msg)
        elif msg["type"] == "orderbook":
            signals.update_orderbook(msg)
    """

    def __init__(
        self,
        symbols:            list[str] = SYMBOLS,
        window:             int       = WINDOW,
        z_entry:            float     = Z_ENTRY,
        z_exit:             float     = Z_EXIT,
        obi_theta:          float     = OBI_THETA,
        obi_levels:         int       = OBI_LEVELS,
        notional_per_trade: float     = NOTIONAL_PER_TRADE,
    ) -> None:
        self._z_entry            = z_entry
        self._z_exit             = z_exit
        self._obi_theta          = obi_theta
        self._obi_levels         = obi_levels
        self._notional_per_trade = notional_per_trade

        self._state: dict[str, _SymbolState] = {
            s: _SymbolState(s, window) for s in symbols
        }

    # ── Bar Update (primary path) ─────────────────────────────────────────────
    def evaluate(self, bar: dict) -> dict | None:
        """
        Called once per bar from the event loop.

        bar keys (from feed.py):
          type, symbol, open, high, low, close, volume, timestamp, recv_ns

        Returns order dict when both entry conditions are met, else None.
        """
        sym = bar.get("symbol")
        if sym not in self._state:
            return None

        st    = self._state[sym]
        close = float(bar["close"])

        # 1. Feed the rolling price buffer
        st.price_buf.push(close)

        # 2. Compute z-score (returns None if window not yet warm)
        z = st.price_buf.zscore(close)
        if z is None:
            return None

        log.debug(
            "signal_tick",
            symbol=sym,
            z=round(z, 4),
            obi=round(st.obi, 4),
            in_position=st.in_position,
        )

        # 3. Exit path — check before considering a new entry
        if st.in_position:
            if z > self._z_exit:
                # Price has reverted: z crossed back above exit threshold.
                # Emit log; the order_manager monitors open positions separately
                # and should receive a complementary SELL signal here in a full
                # implementation.  For now we update state and log for audit.
                log.info(
                    "exit_signal",
                    symbol=sym,
                    z=round(z, 4),
                    entry_px=st.entry_px,
                    close=close,
                    pnl_est=round((close - st.entry_px) / st.entry_px * 100, 3),
                )
                st.in_position = False
                st.entry_px    = float("nan")
            return None     # never open a second position while one is live

        # 4. Entry path — BOTH conditions must hold
        oversold     = z < self._z_entry                  # condition 1: z-score
        buy_pressure = st.obi > self._obi_theta           # condition 2: OBI
        if not (oversold and buy_pressure):
            return None

        # 5. Size the order
        qty, notional = self._size_order(sym, close)
        if qty <= 0.0:
            log.warning("sizing_returned_zero", symbol=sym, close=close)
            return None

        # 6. Set aggressive limit: max(close, best_ask) × (1 + slippage)
        #    Guarantees limit_px ≥ close even when cached orderbook is stale.
        ref_px   = close
        if not np.isnan(st.best_ask) and st.best_ask > 0:
            ref_px = max(close, st.best_ask)
        limit_px = round(ref_px * (1.0 + LIMIT_SLIPPAGE), 2)

        log.info(
            "entry_signal",
            symbol=sym,
            z=round(z, 4),
            obi=round(st.obi, 4),
            qty=qty,
            limit_px=limit_px,
            notional=notional,
        )
        st.in_position = True
        st.entry_px    = close

        return {
            "symbol":   sym,
            "side":     OrderSide.BUY,
            "qty":      qty,
            "limit_px": limit_px,
            "notional": notional,
        }

    # ── Orderbook Update (secondary path) ─────────────────────────────────────
    def update_orderbook(self, ob: dict) -> None:
        """
        Called per L2 snapshot from the crypto orderbook stream.

        ob keys (normalized by feed.py):
          type, symbol, bids [[price, size], ...], asks [[price, size], ...]

        Computes ρ_t = (V^b − V^a) / (V^b + V^a) and caches best ask.
        Does NOT generate a trade signal on its own — OBI is a gating condition
        only; the bar event drives signal generation.
        """
        sym = ob.get("symbol")
        if sym not in self._state:
            return

        bids: list[list[float]] = ob.get("bids", [])
        asks: list[list[float]] = ob.get("asks", [])
        if not bids or not asks:
            return

        n = self._obi_levels

        # Vectorized depth aggregation — numpy fromiter avoids Python loop overhead
        bid_sizes = np.fromiter(
            (float(b[1]) for b in bids[:n]),
            dtype=np.float64,
            count=min(n, len(bids)),
        )
        ask_sizes = np.fromiter(
            (float(a[1]) for a in asks[:n]),
            dtype=np.float64,
            count=min(n, len(asks)),
        )

        vb  = bid_sizes.sum()
        va  = ask_sizes.sum()
        rho = (vb - va) / (vb + va + 1e-8)   # epsilon guards /0 on empty book

        st      = self._state[sym]
        st.obi  = float(rho)

        # Cache best ask for limit price precision
        if asks:
            st.best_ask = float(asks[0][0])

    # ── Position state rollback ───────────────────────────────────────────────
    def rollback_entry(self, symbol: str) -> None:
        """
        Called by the engine loop when an order is blocked or fails after
        evaluate() has already set in_position=True.  Resets state so the
        engine can retry on the next qualifying bar.
        """
        st = self._state.get(symbol)
        if st and st.in_position:
            log.warning("entry_rollback", symbol=symbol,
                        reason="order_blocked_or_failed")
            st.in_position = False
            st.entry_px    = float("nan")

    # ── Private: Position Sizing ───────────────────────────────────────────────
    def _size_order(self, symbol: str, price: float) -> tuple[float, float]:
        """
        Returns (qty, notional).

        Notional is the minimum of:
          • NOTIONAL_PER_TRADE (strategy-level cap for small account)
          • SYMBOL_CAPS[symbol] (risk_params.py per-symbol cap)
          • MAX_ORDER_NOTIONAL  (circuit-breaker hard cap)

        qty is rounded to exchange-allowed decimal precision.
        """
        cap = min(
            self._notional_per_trade,
            SYMBOL_CAPS.get(symbol, self._notional_per_trade),
            MAX_ORDER_NOTIONAL,
        )
        decimals = _QTY_DECIMALS.get(symbol, 6)
        # Floor (not round) so actual notional never exceeds cap.
        # round() can push qty × price above cap, causing circuit breaker rejection.
        qty      = math.floor(cap / price * 10 ** decimals) / 10 ** decimals

        if qty <= 0.0:
            return 0.0, 0.0

        actual_notional = round(qty * price, 2)
        return qty, actual_notional
