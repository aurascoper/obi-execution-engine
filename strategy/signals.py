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

Phase 3 — Tag-aware inventory
  _SymbolState.positions     : dict[str, float]  tag → open qty (0.0 = flat)
  _SymbolState.entry_prices  : dict[str, float]  tag → entry price
  SignalEngine.strategy_tag  : str               "taker" | "maker"

  Each engine instance owns one tag.  evaluate() checks/writes only its
  own tag so taker and maker can share the same Alpaca account without
  reading each other's inventory.

  Backward compatibility for EquitiesSignalEngine:
    _SymbolState.in_position  →  @property  (any tag has qty > 0)
    _SymbolState.entry_px     →  @property  (first entry price found)
    Both have setters that map to the new dicts for legacy write paths.

Two public update paths (called by the event loop):
  signals.evaluate(bar)        → per bar; may return order dict or None
  signals.update_orderbook(ob) → per L2 snapshot; updates OBI state only
  signals.on_fill(...)         → called by OrderManager trade-update stream

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
WINDOW              = 60       # rolling bars for z-score
                               # Crypto engine:   60 one-minute bars = 60-min micro-structure window
                               #                  (24/7 stream; no historical pre-seed; warmup ~60 min)
                               # Equities engine: 60 daily bars = ~3-month macro window
                               #                  (pre-seeded from IEX history at startup; warm on bar 1)
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
    """
    All mutable state for one symbol; no heap allocation after __init__.

    Phase 3 — tag-aware inventory replaces the flat bool/float pair:
      positions    : dict[str, float]  — tag → open qty (0.0 = flat)
      entry_prices : dict[str, float]  — tag → entry price
      best_bid     : float             — cached for maker limit placement

    Backward-compat @property descriptors expose the old names so
    EquitiesSignalEngine (which inherits SignalEngine's evaluate path) keeps
    working without modification.
    """

    __slots__ = (
        "symbol",
        "price_buf",      # _RollingBuffer of close prices
        "obi",            # latest ρ_t scalar (updated by update_orderbook)
        "best_ask",       # latest best ask (taker aggressive limit)
        "best_bid",       # latest best bid (maker passive limit)
        "positions",      # dict[str, float]  tag → open qty
        "entry_prices",   # dict[str, float]  tag → entry price
        "pending_exits",  # dict[str, bool]   tag → sell order submitted, awaiting fill
    )

    def __init__(self, symbol: str, window: int) -> None:
        self.symbol         = symbol
        self.price_buf      = _RollingBuffer(window)
        self.obi            = 0.0
        self.best_ask       = float("nan")
        self.best_bid       = float("nan")
        self.positions      : dict[str, float] = {}
        self.entry_prices   : dict[str, float] = {}
        self.pending_exits  : dict[str, bool]  = {}

    # ── Tag-aware helpers ──────────────────────────────────────────────────────

    def is_open(self, tag: str) -> bool:
        """True if this tag currently holds a non-zero position."""
        return self.positions.get(tag, 0.0) != 0.0

    def open_qty(self, tag: str) -> float:
        return self.positions.get(tag, 0.0)

    def best_prices(self) -> tuple[float, float]:
        """Returns (best_bid, best_ask) — either may be nan if not yet cached."""
        return self.best_bid, self.best_ask

    # ── Backward-compat properties (used by EquitiesSignalEngine path) ─────────

    @property
    def in_position(self) -> bool:
        """True if any tag holds a non-zero position."""
        return any(q != 0.0 for q in self.positions.values())

    @in_position.setter
    def in_position(self, val: bool) -> None:
        """Legacy write path — maps to the 'taker' tag."""
        if not val:
            self.positions["taker"] = 0.0
        # True writes are handled through the tag-aware path; ignore here.

    @property
    def entry_px(self) -> float:
        """First non-nan entry price found across all tags."""
        for px in self.entry_prices.values():
            if not math.isnan(px):
                return px
        return float("nan")

    @entry_px.setter
    def entry_px(self, val: float) -> None:
        """Legacy write path — maps to the 'taker' tag."""
        self.entry_prices["taker"] = val


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
        strategy_tag:       str       = "taker",
    ) -> None:
        self._z_entry            = z_entry
        self._z_exit             = z_exit
        self._obi_theta          = obi_theta
        self._obi_levels         = obi_levels
        self._notional_per_trade = notional_per_trade
        self.strategy_tag        = strategy_tag

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

        tag = self.strategy_tag
        log.debug(
            "signal_tick",
            symbol=sym,
            z=round(z, 4),
            obi=round(st.obi, 4),
            tag=tag,
            in_position=st.is_open(tag),
        )

        # 3. Exit path — check before considering a new entry
        if st.is_open(tag):
            # Sell order already submitted; waiting for on_fill() to clear state.
            if st.pending_exits.get(tag, False):
                return None

            if z > self._z_exit:
                qty_to_sell = st.open_qty(tag)
                sell_px     = self._limit_px(st, close, OrderSide.SELL)
                notional    = round(qty_to_sell * close, 2)
                entry_px    = st.entry_prices.get(tag, float("nan"))
                pnl_est     = (
                    round((close - entry_px) / entry_px * 100, 3)
                    if not math.isnan(entry_px) else float("nan")
                )
                log.info(
                    "exit_signal",
                    symbol=sym,
                    z=round(z, 4),
                    tag=tag,
                    entry_px=entry_px,
                    close=close,
                    sell_px=sell_px,
                    qty=qty_to_sell,
                    pnl_est=pnl_est,
                )
                st.pending_exits[tag] = True
                return {
                    "symbol":   sym,
                    "side":     OrderSide.SELL,
                    "qty":      qty_to_sell,
                    "limit_px": sell_px,
                    "notional": notional,
                }

            return None     # still in position, mean-reversion incomplete

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

        # 6. Compute limit price (strategy-aware via _limit_px)
        limit_px = self._limit_px(st, close, OrderSide.BUY)

        log.info(
            "entry_signal",
            symbol=sym,
            z=round(z, 4),
            obi=round(st.obi, 4),
            tag=tag,
            qty=qty,
            limit_px=limit_px,
            notional=notional,
        )
        st.positions[tag]    = qty
        st.entry_prices[tag] = close

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

        # Cache best ask (taker limit) and best bid (maker limit)
        if asks:
            st.best_ask = float(asks[0][0])
        if bids:
            st.best_bid = float(bids[0][0])

    # ── Fill handler (called by OrderManager TradingStream) ──────────────────
    def on_fill(
        self,
        client_order_id: str,
        symbol:          str,
        qty:             float,
        side:            str,
    ) -> None:
        """
        Called by OrderManager when a fill arrives on the TradingStream.

        Parses the client_order_id prefix to identify the owning tag and
        updates positions accordingly.  Ignores fills belonging to a
        different tag (e.g. maker fills arriving on a taker engine).

        client_order_id format: "{tag}_{sym_no_slash}_{epoch_s}"
          e.g. "taker_ETHUSD_1744566000"
        """
        st = self._state.get(symbol)
        if st is None:
            return

        # Extract tag from prefix — robust to any suffix format
        parts = client_order_id.split("_", 1)
        fill_tag = parts[0] if parts else ""

        if fill_tag != self.strategy_tag:
            return   # fill belongs to a different engine instance

        if side.lower() in ("buy", "b"):
            st.positions[fill_tag] = qty
            log.info("fill_recorded", symbol=symbol, tag=fill_tag, qty=qty, side="buy")
        else:
            st.positions[fill_tag]     = 0.0
            st.entry_prices[fill_tag]  = float("nan")
            st.pending_exits[fill_tag] = False
            log.info("fill_recorded", symbol=symbol, tag=fill_tag, qty=0.0, side="sell")

    # ── Position state rollback ───────────────────────────────────────────────
    def rollback_entry(self, symbol: str) -> None:
        """
        Called by the engine loop when a BUY order is blocked or fails after
        evaluate() has already written to positions.  Resets state so the
        engine can retry on the next qualifying bar.
        """
        tag = self.strategy_tag
        st  = self._state.get(symbol)
        if st and st.is_open(tag):
            log.warning("entry_rollback", symbol=symbol, tag=tag,
                        reason="order_blocked_or_failed")
            st.positions[tag]    = 0.0
            st.entry_prices[tag] = float("nan")

    def rollback_exit(self, symbol: str) -> None:
        """
        Called by the engine loop when a SELL order is blocked or fails after
        evaluate() set pending_exits[tag]=True.  Clears the pending flag so
        the exit will be retried on the next qualifying bar.
        Position and entry_price are intentionally preserved — we still hold
        the asset.
        """
        tag = self.strategy_tag
        st  = self._state.get(symbol)
        if st and st.pending_exits.get(tag, False):
            log.warning("exit_rollback", symbol=symbol, tag=tag,
                        reason="sell_blocked_or_failed")
            st.pending_exits[tag] = False

    # ── Private: Limit Price ──────────────────────────────────────────────────
    @staticmethod
    def _price_decimals(ref: float) -> int:
        """Dynamic decimal places so sub-penny assets never round to 0.00."""
        return max(2, -int(math.floor(math.log10(ref))) + 2) if ref > 0 else 2

    def _limit_px(
        self, st: _SymbolState, close: float, side: OrderSide
    ) -> float:
        """
        Compute the limit price for an order based on strategy tag and side.

        Taker (cross-spread, guaranteed fill):
          BUY  → max(close, best_ask) × (1 + LIMIT_SLIPPAGE)
          SELL → min(close, best_bid) × (1 − LIMIT_SLIPPAGE)

        Maker (post-at-book, earn spread):
          BUY  → best_bid  (join the bid; no slippage markup)
          SELL → best_ask  (join the ask; no slippage discount)
          Falls back to close ± LIMIT_SLIPPAGE when book is stale.
        """
        if self.strategy_tag == "maker":
            if side == OrderSide.BUY:
                ref = st.best_bid if not math.isnan(st.best_bid) and st.best_bid > 0 else close
                # No spread-crossing adjustment for maker orders
                dec = self._price_decimals(ref)
                return round(ref, dec)
            else:
                ref = st.best_ask if not math.isnan(st.best_ask) and st.best_ask > 0 else close
                dec = self._price_decimals(ref)
                return round(ref, dec)
        else:  # taker
            if side == OrderSide.BUY:
                ref = close
                if not math.isnan(st.best_ask) and st.best_ask > 0:
                    ref = max(close, st.best_ask)
                dec = self._price_decimals(ref)
                return round(ref * (1.0 + LIMIT_SLIPPAGE), dec)
            else:
                ref = close
                if not math.isnan(st.best_bid) and st.best_bid > 0:
                    ref = min(close, st.best_bid)
                dec = self._price_decimals(ref)
                return round(ref * (1.0 - LIMIT_SLIPPAGE), dec)

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
