"""
strategy/signals.py — Hybrid OBI + Mean-Reversion Signal Engine
Default: Long-Only (Alpaca crypto/equities).  allow_short=True unlocks the
bi-directional Hyperliquid path (Phase 4).

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
║  Long entry:  z_t < Z_ENTRY        AND  ρ_t >  OBI_THETA        ║
║  Long exit:   z_t > Z_EXIT                                       ║
║  Short entry: z_t > Z_SHORT_ENTRY  AND  ρ_t < −OBI_THETA        ║
║               (gated by allow_short; off for Alpaca engines)     ║
║  Short exit:  z_t < Z_EXIT_SHORT                                 ║
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
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
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
Z_SHORT_ENTRY       = +1.25   # enter short when z > Z_SHORT_ENTRY (overbought) — HL only
Z_EXIT_SHORT        = +0.50   # cover short when z reverts below Z_EXIT_SHORT    — HL only
STOP_LOSS_PCT       = 0.010   # force-exit if adverse move ≥ 1% of entry price — trending-regime safety net
MAX_POSITION_SECS_RTH = 30 * 60   # time-stop during US RTH (M-F 09:30-16:00 ET)
MAX_POSITION_SECS_OVN = 60 * 60   # time-stop overnight / weekends — slower reversion
TREND_MA_WINDOW     = 240     # 240-bar (4h at 1-min) SMA for regime gate — block entries opposing trend
_ET = ZoneInfo("America/New_York")
OBI_THETA           = 0.00    # any net buy pressure confirms entry (bid depth > ask depth)
OBI_LEVELS          = 20      # top N order-book levels (deepened from 5:
                              # live burn-in on HL BTC/ETH showed OBI-5 captures
                              # high-frequency MM flicker at the front row, with
                              # 2.6–5.8× more std than OBI-20 and occasional sign
                              # contradictions vs the deeper committed book —
                              # specifically a "bid-side façade at levels 1-5
                              # over an ask-heavy levels 6-20" trap on ETH.
LIMIT_SLIPPAGE      = 0.0010  # limit price = close × (1 + LIMIT_SLIPPAGE)
NOTIONAL_PER_TRADE  = 750.0 if __import__("os").environ.get("EXECUTION_MODE","PAPER").upper()=="LIVE" else 2_000.0

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
        "price_buf",      # _RollingBuffer of close prices (WINDOW bars, z-score)
        "trend_buf",      # _RollingBuffer of close prices (TREND_MA_WINDOW bars, regime gate)
        "obi",            # latest ρ_t scalar (updated by update_orderbook)
        "best_ask",       # latest best ask (taker aggressive limit)
        "best_bid",       # latest best bid (maker passive limit)
        "positions",      # dict[str, float]  tag → open qty
        "entry_prices",   # dict[str, float]  tag → entry price
        "entry_ts",       # dict[str, int]    tag → epoch-sec at entry (for time-stop)
        "pending_exits",  # dict[str, bool]   tag → sell order submitted, awaiting fill
        "z_entry",        # per-symbol override (None = use engine default)
        "z_exit",
        "z_short_entry",
        "z_exit_short",
    )

    def __init__(self, symbol: str, window: int) -> None:
        self.symbol         = symbol
        self.price_buf      = _RollingBuffer(window)
        self.trend_buf      = _RollingBuffer(TREND_MA_WINDOW)
        self.obi            = 0.0
        self.best_ask       = float("nan")
        self.best_bid       = float("nan")
        self.positions      : dict[str, float] = {}
        self.entry_prices   : dict[str, float] = {}
        self.entry_ts       : dict[str, int]   = {}
        self.pending_exits  : dict[str, bool]  = {}
        self.z_entry:        float | None = None
        self.z_exit:         float | None = None
        self.z_short_entry:  float | None = None
        self.z_exit_short:   float | None = None

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
        z_short_entry:      float     = Z_SHORT_ENTRY,
        z_exit_short:       float     = Z_EXIT_SHORT,
        obi_theta:          float     = OBI_THETA,
        obi_levels:         int       = OBI_LEVELS,
        notional_per_trade: float     = NOTIONAL_PER_TRADE,
        strategy_tag:       str       = "taker",
        allow_short:        bool      = False,
    ) -> None:
        self._z_entry            = z_entry
        self._z_exit             = z_exit
        self._z_short_entry      = z_short_entry
        self._z_exit_short       = z_exit_short
        self._obi_theta          = obi_theta
        self._obi_levels         = obi_levels
        self._notional_per_trade = notional_per_trade
        self.strategy_tag        = strategy_tag
        self._allow_short        = allow_short

        self._state: dict[str, _SymbolState] = {
            s: _SymbolState(s, window) for s in symbols
        }

    def set_symbol_z(
        self,
        symbol: str,
        z_entry: float,
        z_exit: float,
        z_short_entry: float,
        z_exit_short: float,
    ) -> None:
        st = self._state.get(symbol)
        if st is None:
            return
        st.z_entry       = z_entry
        st.z_exit        = z_exit
        st.z_short_entry = z_short_entry
        st.z_exit_short  = z_exit_short

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

        # 1. Feed the rolling price buffers
        st.price_buf.push(close)
        st.trend_buf.push(close)

        # 2. Compute z-score (returns None if window not yet warm)
        z = st.price_buf.zscore(close)
        if z is None:
            return None

        tag = self.strategy_tag
        log.info(
            "signal_tick",
            symbol=sym,
            z=round(z, 4),
            obi=round(st.obi, 4),
            tag=tag,
            in_position=st.is_open(tag),
        )

        # 3. Exit path — check before considering a new entry
        if st.is_open(tag):
            # Close order already submitted; waiting for on_fill() to clear state.
            if st.pending_exits.get(tag, False):
                return None

            cur_qty  = st.open_qty(tag)     # signed: +long, −short
            is_long  = cur_qty > 0
            entry_px = st.entry_prices.get(tag, float("nan"))

            # Trending-regime safety net: exit on adverse-move stop OR time-stop.
            # Evaluated before z-revert so a reversion that happens AFTER the
            # stop breach still closes the position (z-revert would also fire).
            stop_reason: str | None = None
            if not math.isnan(entry_px) and entry_px > 0:
                adverse = ((entry_px - close) / entry_px) if is_long \
                          else ((close - entry_px) / entry_px)
                if adverse >= STOP_LOSS_PCT:
                    stop_reason = f"stop_loss_{adverse:.4f}"
            entry_ts = st.entry_ts.get(tag, 0)
            if entry_ts > 0:
                age_s = int(time.time()) - entry_ts
                now_et = datetime.now(_ET)
                is_rth = (now_et.weekday() < 5
                          and 930 <= now_et.hour * 100 + now_et.minute < 1600)
                max_secs = MAX_POSITION_SECS_RTH if is_rth else MAX_POSITION_SECS_OVN
                if age_s >= max_secs:
                    stop_reason = stop_reason or f"time_stop_{age_s}s"

            _z_exit = st.z_exit if st.z_exit is not None else self._z_exit
            _z_exit_short = st.z_exit_short if st.z_exit_short is not None else self._z_exit_short
            z_revert = (z > _z_exit) if is_long else (z < _z_exit_short)
            if not (z_revert or stop_reason):
                return None

            exit_side = OrderSide.SELL if is_long else OrderSide.BUY
            exit_qty  = abs(cur_qty)
            exit_px   = self._limit_px(st, close, exit_side)
            notional  = round(exit_qty * close, 2)
            if not math.isnan(entry_px) and entry_px > 0:
                raw_pnl   = (close - entry_px) if is_long else (entry_px - close)
                pnl_est   = round(raw_pnl / entry_px * 100, 3)
            else:
                pnl_est   = float("nan")
            log.info(
                "exit_signal",
                symbol=sym,
                z=round(z, 4),
                tag=tag,
                direction=("long" if is_long else "short"),
                entry_px=entry_px,
                close=close,
                exit_px=exit_px,
                qty=exit_qty,
                pnl_est=pnl_est,
                reason=(stop_reason or "z_revert"),
            )
            st.pending_exits[tag] = True
            return {
                "symbol":   sym,
                "side":     exit_side,
                "qty":      exit_qty,
                "limit_px": exit_px,
                "notional": notional,
            }

        # 4. Entry path — long xor short (both conditions must hold)
        _z_entry = st.z_entry if st.z_entry is not None else self._z_entry
        _z_short_entry = st.z_short_entry if st.z_short_entry is not None else self._z_short_entry
        long_entry  = (z < _z_entry)       and (st.obi >  self._obi_theta)
        short_entry = (
            self._allow_short
            and (z > _z_short_entry)
            and (st.obi < -self._obi_theta)
        )
        if not (long_entry or short_entry):
            return None

        # 4b. Trend gate — block entries that oppose the 240-bar SMA slope.
        #     During warmup (< 240 bars), allow all entries; stops protect.
        if st.trend_buf.is_full:
            trend_sma = float(np.mean(st.trend_buf._active()))
            if long_entry and close < trend_sma:
                log.info("trend_gate_blocked", symbol=sym, direction="long",
                         z=round(z, 4), close=close, sma=round(trend_sma, 2))
                return None
            if short_entry and close > trend_sma:
                log.info("trend_gate_blocked", symbol=sym, direction="short",
                         z=round(z, 4), close=close, sma=round(trend_sma, 2))
                return None

        entry_side = OrderSide.BUY if long_entry else OrderSide.SELL
        direction  = "long" if long_entry else "short"

        # 5. Size the order (absolute qty; sign applied to positions below)
        qty, notional = self._size_order(sym, close)
        if qty <= 0.0:
            log.warning("sizing_returned_zero", symbol=sym, close=close)
            return None

        # 6. Compute limit price (strategy-aware via _limit_px)
        limit_px = self._limit_px(st, close, entry_side)

        log.info(
            "entry_signal",
            symbol=sym,
            z=round(z, 4),
            obi=round(st.obi, 4),
            tag=tag,
            direction=direction,
            qty=qty,
            limit_px=limit_px,
            notional=notional,
        )
        st.positions[tag]    = qty if long_entry else -qty
        st.entry_prices[tag] = close
        st.entry_ts[tag]     = int(time.time())

        return {
            "symbol":   sym,
            "side":     entry_side,
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

        # Tag match: our client_order_ids are `{tag}_{sym}_{epoch}`, so a
        # prefix + trailing underscore uniquely identifies this engine's fills
        # and tolerates multi-underscore tags like "hl_z".
        if not client_order_id.startswith(self.strategy_tag + "_"):
            return   # fill belongs to a different engine instance

        fill_tag = self.strategy_tag
        side_l = side.lower()
        # pending_exits[tag]=True means evaluate() emitted a close; any fill
        # that arrives while the flag is set is the cover/close, regardless of
        # side (SELL covers long, BUY covers short).
        if st.pending_exits.get(fill_tag, False):
            st.positions[fill_tag]     = 0.0
            st.entry_prices[fill_tag]  = float("nan")
            st.entry_ts[fill_tag]      = 0
            st.pending_exits[fill_tag] = False
            log.info("fill_recorded", symbol=symbol, tag=fill_tag,
                     qty=0.0, side=side_l, role="exit")
            return

        # Entry fill — sign the recorded qty by side.
        # Short-entry branch is gated by allow_short so an untracked SELL on a
        # long-only engine (e.g. manual UI close) does NOT leave a phantom
        # short position in memory.
        if side_l in ("buy", "b"):
            st.positions[fill_tag] = qty
            log.info("fill_recorded", symbol=symbol, tag=fill_tag,
                     qty=qty, side=side_l, role="entry")
        elif self._allow_short:
            st.positions[fill_tag] = -qty
            log.info("fill_recorded", symbol=symbol, tag=fill_tag,
                     qty=-qty, side=side_l, role="entry")
        else:
            # Long-only engine received a SELL fill we didn't author as an
            # exit. Treat as a force-close (old behaviour) and flag it.
            st.positions[fill_tag]     = 0.0
            st.entry_prices[fill_tag]  = float("nan")
            st.entry_ts[fill_tag]      = 0
            st.pending_exits[fill_tag] = False
            log.warning("untracked_sell_treated_as_close",
                        symbol=symbol, tag=fill_tag, qty=qty)

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
            st.entry_ts[tag]     = 0

    def reconcile_positions(
        self,
        alpaca_positions: list,
        alpaca_orders:    list,
    ) -> None:
        """
        Seed position state from Alpaca's open positions on engine startup.
        Prevents re-entry into positions opened by a previous engine instance.

        Bug 2: engines start with empty state after restart → would re-enter
               symbols already held, doubling exposure.
        Bug 4: pre-Phase-3 orders have UUID client_order_ids (no tag prefix) →
               reconcile defaults them to self.strategy_tag.

        alpaca_positions : list of Alpaca Position objects (client.get_all_positions())
        alpaca_orders    : list of recent filled orders    (status=CLOSED, limit=100)
                           used to look up client_order_id per symbol.
        """
        # Build: normalized_symbol → most-recent tagged client_order_id
        # Alpaca orders use "BTC/USD" format; normalize to "BTCUSD" for matching.
        cid_by_sym: dict[str, str] = {}
        for order in alpaca_orders:
            sym_norm = (getattr(order, "symbol", "") or "").replace("/", "")
            cid      = getattr(order, "client_order_id", "") or ""
            side_raw = getattr(order, "side", "")
            side     = side_raw.value if hasattr(side_raw, "value") else str(side_raw)
            status_raw = getattr(order, "status", "")
            status   = status_raw.value if hasattr(status_raw, "value") else str(status_raw)
            if "buy" in side.lower() and "filled" in status.lower():
                if sym_norm not in cid_by_sym:   # most-recent first
                    cid_by_sym[sym_norm] = cid

        # Build reverse map: "BTCUSD" → "BTC/USD" for our state keys
        norm_to_state = {s.replace("/", ""): s for s in self._state}

        for pos in alpaca_positions:
            alpaca_sym = getattr(pos, "symbol", "")         # e.g. "BTCUSD"
            state_sym  = norm_to_state.get(alpaca_sym)
            if state_sym is None:
                continue                                     # not in our universe

            qty       = float(getattr(pos, "qty",             0) or 0)
            avg_entry = float(getattr(pos, "avg_entry_price", 0) or 0)
            if qty <= 0:
                continue

            cid   = cid_by_sym.get(alpaca_sym, "")
            parts = cid.split("_", 1)
            cid_tag = parts[0] if (len(parts) > 1 and parts[0] in ("taker", "maker")) \
                               else None

            if cid_tag is not None and cid_tag != self.strategy_tag:
                # Position belongs to the other engine — skip.
                continue

            # UUID (pre-Phase-3) orders are treated as "taker" regardless of
            # which engine is reconciling — the original engine crossed the spread.
            tag = cid_tag if cid_tag is not None else "taker"

            # Only seed positions this engine owns.
            if tag != self.strategy_tag:
                continue

            st = self._state[state_sym]
            st.positions[tag]    = qty
            st.entry_prices[tag] = avg_entry
            st.entry_ts[tag]     = int(time.time())   # adopt-now: gives full time-stop budget
            log.info(
                "position_reconciled",
                symbol=state_sym,
                tag=tag,
                qty=qty,
                avg_entry_px=avg_entry,
                client_order_id=cid or "uuid_pre_phase3",
            )

    def reconcile_hl_positions(
        self,
        hl_positions:  list[dict],
        coin_to_symbol: dict[str, str],
        dust_caps_by_coin: dict[str, float] | None = None,
    ) -> None:
        """
        Seed state from Hyperliquid open positions on engine startup.
        Parallel to reconcile_positions() but consumes the signed HL schema
        ({"coin","szi","entry_px",...}) so it never mixes with the Alpaca path.

        coin_to_symbol    : {"BTC": "BTC/USD", ...} — HL coin name → state key.
        dust_caps_by_coin : {"BTC": 1.5e-5, ...} — sub-lot residuals at or below
                            this absolute szi are treated as flat and not
                            written to memory. Matches the flip-guard dust
                            tolerance so one-lot leftovers don't perpetually
                            re-seed an exit signal. Missing coins default to
                            no-dust (exact-zero only).
        """
        dust_caps_by_coin = dust_caps_by_coin or {}
        live_open_syms: set[str] = set()
        for pos in hl_positions:
            coin      = str(pos.get("coin", "")).upper()
            state_sym = coin_to_symbol.get(coin)
            if state_sym is None or state_sym not in self._state:
                continue

            szi = float(pos.get("szi", 0) or 0)
            dust_cap = dust_caps_by_coin.get(coin, 0.0)
            if abs(szi) <= dust_cap:
                if szi != 0.0:
                    log.info(
                        "hl_reconcile_dust_skipped",
                        symbol=state_sym, coin=coin,
                        szi=szi, dust_cap=dust_cap,
                    )
                continue
            entry_px = float(pos.get("entry_px", 0) or 0)

            live_open_syms.add(state_sym)
            st = self._state[state_sym]
            st.positions[self.strategy_tag]    = szi        # signed
            st.entry_prices[self.strategy_tag] = entry_px
            st.entry_ts[self.strategy_tag]     = int(time.time())   # adopt-now
            log.info(
                "hl_position_reconciled",
                symbol=state_sym,
                coin=coin,
                tag=self.strategy_tag,
                szi=szi,
                entry_px=entry_px,
            )

        # Flat-on-chain sweep: any memory position under our tag whose symbol
        # is NOT in live_open_syms is stale (e.g. SHADOW mock entry, or a
        # missed-fill WebSocket event in LIVE). Wipe it so the flip-guard
        # exit deadlock self-heals on the next bar.
        tag = self.strategy_tag
        for sym, st in self._state.items():
            if sym in live_open_syms:
                continue
            if st.positions.get(tag, 0.0) != 0.0:
                stale_qty = st.positions[tag]
                st.positions[tag]     = 0.0
                st.entry_prices[tag]  = float("nan")
                st.entry_ts[tag]      = 0
                st.pending_exits[tag] = False
                log.warning(
                    "hl_memory_wiped_stale",
                    symbol=sym, tag=tag,
                    stale_qty=stale_qty,
                    reason="mem_nonzero_but_live_flat",
                )

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
