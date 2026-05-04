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
from datetime import datetime
from zoneinfo import ZoneInfo
import numpy as np
import structlog
from alpaca.trading.enums import OrderSide

from config.risk_params import (
    FEATURE_SET,
    KELLY_CAP,
    KELLY_DEXES,
    KELLY_HL_MIN_BARS,
    KELLY_K,
    KELLY_SIGMA_FLOOR,
    KELLY_SYMBOLS,
    MAX_ORDER_NOTIONAL,
    MLOFI_ALPHA,
    MLOFI_NORM,
    SIGNAL_MODE,
    SIZING_MODE,
    SYMBOL_CAPS,
)
from strategy.baskets import BasketAggregator
from strategy.sizing import kelly_fraction

log = structlog.get_logger(__name__)

# ── Strategy Parameters ────────────────────────────────────────────────────────
SYMBOLS = ["ETH/USD", "BTC/USD"]
WINDOW = 60  # rolling bars for z-score
# Crypto engine:   60 one-minute bars = 60-min micro-structure window
#                  (24/7 stream; no historical pre-seed; warmup ~60 min)
# Equities engine: 60 daily bars = ~3-month macro window
#                  (pre-seeded from IEX history at startup; warm on bar 1)
import os as _os

Z_ENTRY = float(
    _os.environ.get("Z_ENTRY", "-1.25")
)  # enter long when z < Z_ENTRY (oversold)
Z_EXIT = float(
    _os.environ.get("Z_EXIT", "-0.50")
)  # exit long when z reverts above Z_EXIT
Z_SHORT_ENTRY = float(
    _os.environ.get("Z_SHORT_ENTRY", "1.25")
)  # enter short when z > Z_SHORT_ENTRY (overbought) — HL only
Z_EXIT_SHORT = float(
    _os.environ.get("Z_EXIT_SHORT", "0.50")
)  # cover short when z reverts below Z_EXIT_SHORT    — HL only
STOP_LOSS_PCT = (
    0.010  # force-exit if adverse move ≥ 1% of entry price — trending-regime safety net
)

# ── z-revert exit gate ───────────────────────────────────────────────────────
# Prevent premature z_revert exits caused by mean-drift (rolling mean catches up
# to a new price level, collapsing z toward 0 without actual price reversion).
# z_revert requires BOTH conditions; stop_loss / time_stop always fire immediately.
MIN_HOLD_FOR_REVERT_S = 60  # iter4: drop hold gate only
MIN_REVERT_BPS = 0.001  # iter4: keep 10 bps — isolate effect of removing hold only

# ── Momentum / trend-following parameters ─────────────────────────────────────
Z_MOMENTUM_ENTRY = +1.25  # enter momentum long when z > +1.25 (trending up)
Z_MOMENTUM_SHORT_ENTRY = -1.25  # enter momentum short when z < -1.25 (trending down)
Z_4H_MOMENTUM_THRESHOLD = 0.5  # macro regime confirmation: z_4h > 0.5 for longs
MOMENTUM_STOP_PCT = 0.03  # wider stop for trend trades (3% vs 1% mean-reversion)
MOMENTUM_TAG = "momentum"  # strategy tag — non-overlapping with mean-reversion tags
MAX_POSITION_SECS_RTH = 30 * 60  # time-stop during US RTH (M-F 09:30-16:00 ET)
MAX_POSITION_SECS_OVN = 60 * 60  # time-stop overnight / weekends — slower reversion
TREND_MA_WINDOW = (
    240  # 240-bar (4h at 1-min) SMA for regime gate — block entries opposing trend
)
_ET = ZoneInfo("America/New_York")
OBI_THETA = 0.00  # any net buy pressure confirms entry (bid depth > ask depth)
OBI_LEVELS = 20  # top N order-book levels (deepened from 5:
# live burn-in on HL BTC/ETH showed OBI-5 captures
# high-frequency MM flicker at the front row, with
# 2.6–5.8× more std than OBI-20 and occasional sign
# contradictions vs the deeper committed book —
# specifically a "bid-side façade at levels 1-5
# over an ask-heavy levels 6-20" trap on ETH.
LIMIT_SLIPPAGE = 0.0010  # limit price = close × (1 + LIMIT_SLIPPAGE)
# Default sizing per execution mode. Override via NOTIONAL_PER_TRADE_OVERRIDE
# env (used for dust-test smoke runs — set to e.g. "25" before unpausing).
DEFAULT_NOTIONAL_PER_TRADE = (
    750.0
    if __import__("os").environ.get("EXECUTION_MODE", "PAPER").upper() == "LIVE"
    else 2_000.0
)
NOTIONAL_PER_TRADE = float(
    __import__("os").environ.get(
        "NOTIONAL_PER_TRADE_OVERRIDE", DEFAULT_NOTIONAL_PER_TRADE
    )
)

# Alpaca minimum qty precision per symbol (fractional crypto)
# BTC/ETH at current prices need 6+ decimals to express sub-$5 notional.
# Alpaca supports up to 9 decimal places for crypto fractional orders.
_QTY_DECIMALS: dict[str, int] = {
    "ETH/USD": 6,  # 0.000001 ETH (~$0.002 at $2000)
    "BTC/USD": 6,  # 0.000001 BTC (~$0.08 at $80000)
    "SOL/USD": 4,  # 0.0001 SOL (~$0.01 at $130)
    "DOGE/USD": 2,  # 0.01 DOGE (~$0.001 at $0.08)
    "AVAX/USD": 4,  # 0.0001 AVAX (~$0.002 at $20)
    "LINK/USD": 4,  # 0.0001 LINK (~$0.001 at $13)
    "SHIB/USD": 0,  # whole SHIB units (~$0.000012/SHIB → ~1.25M units per $15)
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
        self._buf = np.empty(size, dtype=np.float64)
        self._idx = 0
        self._count = 0
        self._size = size

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

    def newest(self) -> float | None:
        if self._count == 0:
            return None
        return float(self._buf[(self._idx - 1) % self._size])

    def oldest(self) -> float | None:
        if self._count == 0:
            return None
        if self._count < self._size:
            return float(self._buf[0])
        return float(self._buf[self._idx])

    def zscore(self, current: float) -> float | None:
        """
        Returns z_t = (current − μ_w) / σ_w, or None if window not yet full.
        Uses sample std (ddof=1) consistent with pandas rolling().std().
        """
        if not self.is_full:
            return None
        a = self._active()  # contiguous float64 view
        mu = np.mean(a)  # single SIMD pass
        sig = np.std(a, ddof=1)  # second SIMD pass
        if sig < 1e-10:  # flat price — no signal
            return None
        return float((current - mu) / sig)

    def sigma(self) -> float | None:
        """Sample std (ddof=1) over the active window; None until warm."""
        if not self.is_full:
            return None
        s = float(np.std(self._active(), ddof=1))
        if s < 1e-10:
            return None
        return s

    def phi(self) -> float | None:
        """
        AR(1) coefficient φ over the active window: x_{t+1} = φ·x_t + ε.
        Estimated as cov(x[:-1], x[1:]) / var(x[:-1]) — mean-centred.
        Returns None until warm, or if variance collapses. Bounded to
        [-0.999, 0.999] to keep half-life finite for callers.
        """
        if not self.is_full:
            return None
        a = self._active()
        if a.size < 3:
            return None
        x0 = a[:-1]
        x1 = a[1:]
        m0 = float(np.mean(x0))
        var0 = float(np.mean((x0 - m0) ** 2))
        if var0 < 1e-12:
            return None
        cov = float(np.mean((x0 - m0) * (x1 - float(np.mean(x1)))))
        p = cov / var0
        if p >= 0.999:
            p = 0.999
        elif p <= -0.999:
            p = -0.999
        return p


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
        "price_buf",  # _RollingBuffer of close prices (WINDOW bars, z-score)
        "trend_buf",  # _RollingBuffer of close prices (TREND_MA_WINDOW bars, regime gate)
        "obi",  # latest ρ_t scalar (updated by update_orderbook)
        "log_gofi",  # Phase 3: tanh(log((vb+eps)/(va+eps))) ∈ [-1,1]
        "mlofi",  # Phase 3: tanh(Σ α^l · (Δvb_l − Δva_l) / MLOFI_NORM) ∈ [-1,1]
        "prev_bid_sizes",  # ndarray of prior snapshot bid-size levels (MLOFI delta)
        "prev_ask_sizes",  # ndarray of prior snapshot ask-size levels (MLOFI delta)
        "best_ask",  # latest best ask (taker aggressive limit)
        "best_bid",  # latest best bid (maker passive limit)
        "stability_ring",  # _StabilityRing micro-history for fill_observation telemetry
        "positions",  # dict[str, float]  tag → open qty
        "entry_prices",  # dict[str, float]  tag → entry price
        "entry_ts",  # dict[str, int]    tag → epoch-sec at entry (for time-stop)
        "pending_exits",  # dict[str, bool]   tag → sell order submitted, awaiting fill
        "z_entry",  # per-symbol override (None = use engine default)
        "z_exit",
        "z_short_entry",
        "z_exit_short",
        "z_momentum_entry",  # per-symbol momentum override (None = use default)
        "z_momentum_short_entry",
        "z_4h_exit_long",  # patient-hold override: require z_4h >= this to exit long (default 0)
        "z_4h_exit_short",  # patient-hold override: require z_4h <= this to exit short (default 0)
    )

    def __init__(self, symbol: str, window: int) -> None:
        self.symbol = symbol
        self.price_buf = _RollingBuffer(window)
        self.trend_buf = _RollingBuffer(TREND_MA_WINDOW)
        self.obi = 0.0
        self.log_gofi = 0.0
        self.mlofi = 0.0
        self.prev_bid_sizes: np.ndarray | None = None
        self.prev_ask_sizes: np.ndarray | None = None
        self.best_ask = float("nan")
        self.best_bid = float("nan")
        # Lazy-init: created on first update_orderbook tick to avoid the import
        # cost / cyclical risk at SymbolState construction time.
        self.stability_ring = None
        self.positions: dict[str, float] = {}
        self.entry_prices: dict[str, float] = {}
        self.entry_ts: dict[str, int] = {}
        self.pending_exits: dict[str, bool] = {}
        self.z_entry: float | None = None
        self.z_exit: float | None = None
        self.z_short_entry: float | None = None
        self.z_exit_short: float | None = None
        self.z_momentum_entry: float | None = None
        self.z_momentum_short_entry: float | None = None
        self.z_4h_exit_long: float | None = None
        self.z_4h_exit_short: float | None = None

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
        symbols: list[str] = SYMBOLS,
        window: int = WINDOW,
        z_entry: float = Z_ENTRY,
        z_exit: float = Z_EXIT,
        z_short_entry: float = Z_SHORT_ENTRY,
        z_exit_short: float = Z_EXIT_SHORT,
        obi_theta: float = OBI_THETA,
        obi_levels: int = OBI_LEVELS,
        notional_per_trade: float = NOTIONAL_PER_TRADE,
        strategy_tag: str = "taker",
        allow_short: bool = False,
        basket_agg: BasketAggregator | None = None,
    ) -> None:
        self._z_entry = z_entry
        self._z_exit = z_exit
        self._z_short_entry = z_short_entry
        self._z_exit_short = z_exit_short
        self._obi_theta = obi_theta
        self._obi_levels = obi_levels
        self._notional_per_trade = notional_per_trade
        self.strategy_tag = strategy_tag
        self._allow_short = allow_short
        # Phase 2: optional sector-residual aggregator. When None OR when
        # SIGNAL_MODE != "basket_residual", evaluate() uses raw per-symbol z.
        self._basket_agg = basket_agg

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
        st.z_entry = z_entry
        st.z_exit = z_exit
        st.z_short_entry = z_short_entry
        st.z_exit_short = z_exit_short

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

        st = self._state[sym]
        close = float(bar["close"])

        # 1. Feed the rolling price buffers
        st.price_buf.push(close)
        st.trend_buf.push(close)

        # 1b. Feed the basket aggregator (Phase 2) — no-op if close not finite
        # or symbol not in a basket. We pass log(close) so the residual lives
        # in log-return space (stationary under multiplicative drift).
        if self._basket_agg is not None and close > 0:
            self._basket_agg.update(sym, math.log(close))

        # 2. Compute z-score: basket-residual when SIGNAL_MODE is on AND the
        # aggregator has a warm buffer for this symbol; otherwise raw.
        z: float | None = None
        if (
            SIGNAL_MODE == "basket_residual"
            and self._basket_agg is not None
            and close > 0
        ):
            z = self._basket_agg.residual_z(sym, math.log(close))
        if z is None:
            z = st.price_buf.zscore(close)
        if z is None:
            return None

        z_4h = st.trend_buf.zscore(close)
        tag = self.strategy_tag
        log.info(
            "signal_tick",
            symbol=sym,
            z=round(z, 4),
            z_4h=round(z_4h, 4) if z_4h is not None else None,
            obi=round(st.obi, 4),
            tag=tag,
            in_position=st.is_open(tag),
        )

        # 3. Exit path — check before considering a new entry
        if st.is_open(tag):
            # Close order already submitted; waiting for on_fill() to clear state.
            if st.pending_exits.get(tag, False):
                return None

            cur_qty = st.open_qty(tag)  # signed: +long, −short
            is_long = cur_qty > 0
            entry_px = st.entry_prices.get(tag, float("nan"))

            # Trending-regime safety net: exit on adverse-move stop OR time-stop.
            # Evaluated before z-revert so a reversion that happens AFTER the
            # stop breach still closes the position (z-revert would also fire).
            stop_reason: str | None = None
            if not math.isnan(entry_px) and entry_px > 0:
                adverse = (
                    ((entry_px - close) / entry_px)
                    if is_long
                    else ((close - entry_px) / entry_px)
                )
                if adverse >= STOP_LOSS_PCT:
                    stop_reason = f"stop_loss_{adverse:.4f}"
            entry_ts = st.entry_ts.get(tag, 0)
            if entry_ts > 0:
                age_s = int(time.time()) - entry_ts
                now_et = datetime.now(_ET)
                is_rth = (
                    now_et.weekday() < 5
                    and 930 <= now_et.hour * 100 + now_et.minute < 1600
                )
                max_secs = MAX_POSITION_SECS_RTH if is_rth else MAX_POSITION_SECS_OVN
                if age_s >= max_secs:
                    stop_reason = stop_reason or f"time_stop_{age_s}s"

            _z_exit = st.z_exit if st.z_exit is not None else self._z_exit
            _z_exit_short = (
                st.z_exit_short if st.z_exit_short is not None else self._z_exit_short
            )
            z_revert = (z > _z_exit) if is_long else (z < _z_exit_short)

            # Gate z_revert: suppress mean-drift false reverts.
            # Require BOTH minimum hold time AND favorable price move.
            if z_revert and not stop_reason:
                hold_age = (int(time.time()) - entry_ts) if entry_ts > 0 else 0
                favorable = 0.0
                if not math.isnan(entry_px) and entry_px > 0:
                    favorable = (
                        (close - entry_px) / entry_px
                        if is_long
                        else (entry_px - close) / entry_px
                    )
                if hold_age < MIN_HOLD_FOR_REVERT_S or favorable < MIN_REVERT_BPS:
                    log.info(
                        "z_revert_suppressed",
                        symbol=sym,
                        z=round(z, 4),
                        tag=tag,
                        hold_age_s=hold_age,
                        favorable_bps=round(favorable * 10000, 1),
                        reason=(
                            "hold_too_short"
                            if hold_age < MIN_HOLD_FOR_REVERT_S
                            else "insufficient_move"
                        ),
                    )
                    z_revert = False

            if not (z_revert or stop_reason):
                return None

            exit_side = OrderSide.SELL if is_long else OrderSide.BUY
            exit_qty = abs(cur_qty)
            exit_px = self._limit_px(st, close, exit_side)
            notional = round(exit_qty * close, 2)
            if not math.isnan(entry_px) and entry_px > 0:
                raw_pnl = (close - entry_px) if is_long else (entry_px - close)
                pnl_est = round(raw_pnl / entry_px * 100, 3)
            else:
                pnl_est = float("nan")
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
                "symbol": sym,
                "side": exit_side,
                "qty": exit_qty,
                "limit_px": exit_px,
                "notional": notional,
                "_exit_pnl_pct": pnl_est,
                "_exit_tag": tag,
                "obi": round(st.obi, 4),
            }

        # 4. Entry path — long xor short (both conditions must hold)
        _z_entry = st.z_entry if st.z_entry is not None else self._z_entry
        _z_short_entry = (
            st.z_short_entry if st.z_short_entry is not None else self._z_short_entry
        )
        # Phase 3: the feature used for the flow-direction gate. Defaults to
        # OBI (existing behavior) — flag-flip only affects this selection.
        if FEATURE_SET == "log_gofi":
            _flow = st.log_gofi
        elif FEATURE_SET == "mlofi":
            _flow = st.mlofi
        else:
            _flow = st.obi
        long_entry = (z < _z_entry) and (_flow > self._obi_theta)
        short_entry = (
            self._allow_short and (z > _z_short_entry) and (_flow < -self._obi_theta)
        )
        if not (long_entry or short_entry):
            return None

        # 4b. Trend gate — block entries that oppose the 240-bar SMA slope.
        #     During warmup (< 240 bars), allow all entries; stops protect.
        if st.trend_buf.is_full:
            trend_sma = float(np.mean(st.trend_buf._active()))
            if long_entry and close < trend_sma:
                log.info(
                    "trend_gate_blocked",
                    symbol=sym,
                    direction="long",
                    z=round(z, 4),
                    close=close,
                    sma=round(trend_sma, 2),
                )
                return None
            if short_entry and close > trend_sma:
                log.info(
                    "trend_gate_blocked",
                    symbol=sym,
                    direction="short",
                    z=round(z, 4),
                    close=close,
                    sma=round(trend_sma, 2),
                )
                return None

        entry_side = OrderSide.BUY if long_entry else OrderSide.SELL
        direction = "long" if long_entry else "short"

        # 5. Size the order (absolute qty; sign applied to positions below)
        # Pass z + AR(1) φ + σ so Kelly-OU can shrink the cap when SIZING_MODE=kelly.
        # Under SIZING_MODE=fixed these arguments are ignored — same behavior as before.
        sigma = st.price_buf.sigma()
        phi = st.price_buf.phi()
        qty, notional = self._size_order(sym, close, z=z, phi=phi, sigma=sigma)
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
        st.positions[tag] = qty if long_entry else -qty
        st.entry_prices[tag] = close
        st.entry_ts[tag] = int(time.time())

        return {
            "symbol": sym,
            "side": entry_side,
            "qty": qty,
            "limit_px": limit_px,
            "notional": notional,
            "obi": round(st.obi, 4),
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

        vb = float(bid_sizes.sum())
        va = float(ask_sizes.sum())
        rho = (vb - va) / (vb + va + 1e-8)  # epsilon guards /0 on empty book

        st = self._state[sym]
        st.obi = float(rho)

        # ── Phase 3: log-GOFI (Su 2112.02947) ────────────────────────────────
        # Stationarized log-ratio; squash through tanh so range matches OBI.
        # Under symmetric flip (swap bids↔asks), log-GOFI negates exactly.
        _EPS = 1e-8
        raw_log_gofi = math.log((vb + _EPS) / (va + _EPS))
        st.log_gofi = math.tanh(raw_log_gofi)

        # ── Phase 3: MLOFI (Xu 1907.06230) ───────────────────────────────────
        # Σ_l α^l · (ΔV^b_l − ΔV^a_l). First tick has no prior snapshot ⇒ 0.
        # Level prices assumed stable between snapshots; correct enough for HL
        # tight-spread books. tanh-normalize so range matches OBI.
        mlofi_raw = 0.0
        if st.prev_bid_sizes is not None and st.prev_ask_sizes is not None:
            m = min(len(bid_sizes), len(st.prev_bid_sizes))
            k = min(len(ask_sizes), len(st.prev_ask_sizes))
            lvls = min(m, k)
            if lvls > 0:
                d_bid = bid_sizes[:lvls] - st.prev_bid_sizes[:lvls]
                d_ask = ask_sizes[:lvls] - st.prev_ask_sizes[:lvls]
                weights = MLOFI_ALPHA ** np.arange(lvls, dtype=np.float64)
                mlofi_raw = float(np.dot(weights, d_bid - d_ask))
        st.mlofi = math.tanh(mlofi_raw / MLOFI_NORM) if MLOFI_NORM > 0 else 0.0
        # Retain this snapshot for next tick's delta.
        st.prev_bid_sizes = bid_sizes.copy()
        st.prev_ask_sizes = ask_sizes.copy()

        # Cache best ask (taker limit) and best bid (maker limit)
        if asks:
            st.best_ask = float(asks[0][0])
        if bids:
            st.best_bid = float(bids[0][0])

        # Phase B fill_observation telemetry: push top-N book microstate
        # into the per-symbol stability ring. Reuses vb/va already computed
        # above; no extra aggregation. Failure is silent — telemetry must
        # never block the order path.
        try:
            if st.stability_ring is None:
                from strategy.fill_observation import _StabilityRing
                st.stability_ring = _StabilityRing()
            spread = (
                st.best_ask - st.best_bid
                if math.isfinite(st.best_ask) and math.isfinite(st.best_bid)
                else 0.0
            )
            st.stability_ring.push(
                ts_s=time.time(),
                obi=float(rho),
                depth_top_bid_sum=vb,
                depth_top_ask_sum=va,
                spread=float(spread),
            )
        except Exception:
            pass

    # ── Fill handler (called by OrderManager TradingStream) ──────────────────
    def on_fill(
        self,
        client_order_id: str,
        symbol: str,
        qty: float,
        side: str,
        *,
        fill_px: float | None = None,
        fill_ts: float | None = None,
        submit_ts: float | None = None,
        venue_role: str | None = None,
        is_shadow: bool = False,
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
            return  # fill belongs to a different engine instance

        fill_tag = self.strategy_tag
        side_l = side.lower()

        # Phase B fill_observation telemetry: emit a sibling event with book
        # microstate + stability bucket alongside the existing fill_recorded
        # log. Convergence-point emission guarantees the structural invariant
        # count(fill_observation) == count(fill_recorded). Failure is silent.
        def _emit_observation(role: str, signed_qty: float) -> None:
            try:
                from strategy.fill_observation import build_fill_observation_payload
                bb = (
                    float(st.best_bid)
                    if math.isfinite(st.best_bid)
                    else None
                )
                ba = (
                    float(st.best_ask)
                    if math.isfinite(st.best_ask)
                    else None
                )
                vb = (
                    float(st.prev_bid_sizes.sum())
                    if st.prev_bid_sizes is not None
                    else None
                )
                va = (
                    float(st.prev_ask_sizes.sum())
                    if st.prev_ask_sizes is not None
                    else None
                )
                payload = build_fill_observation_payload(
                    cloid=client_order_id,
                    symbol=symbol,
                    side=side_l,
                    qty=signed_qty,
                    role=role,
                    fill_px=fill_px,
                    fill_ts=fill_ts,
                    submit_ts=submit_ts,
                    venue_role=venue_role,
                    is_shadow=is_shadow,
                    best_bid=bb,
                    best_ask=ba,
                    depth_top_bid_sum=vb,
                    depth_top_ask_sum=va,
                    obi=float(st.obi),
                    ring=st.stability_ring,
                    now_s=time.time(),
                )
                log.info("fill_observation", tag=fill_tag, **payload)
            except Exception:
                pass

        # pending_exits[tag]=True means evaluate() emitted a close; any fill
        # that arrives while the flag is set is the cover/close, regardless of
        # side (SELL covers long, BUY covers short).
        if st.pending_exits.get(fill_tag, False):
            st.positions[fill_tag] = 0.0
            st.entry_prices[fill_tag] = float("nan")
            st.entry_ts[fill_tag] = 0
            st.pending_exits[fill_tag] = False
            log.info(
                "fill_recorded",
                symbol=symbol,
                tag=fill_tag,
                qty=0.0,
                side=side_l,
                role="exit",
            )
            _emit_observation(role="exit", signed_qty=0.0)
            return

        # Entry fill — sign the recorded qty by side.
        # Short-entry branch is gated by allow_short so an untracked SELL on a
        # long-only engine (e.g. manual UI close) does NOT leave a phantom
        # short position in memory.
        if side_l in ("buy", "b"):
            st.positions[fill_tag] = qty
            log.info(
                "fill_recorded",
                symbol=symbol,
                tag=fill_tag,
                qty=qty,
                side=side_l,
                role="entry",
            )
            _emit_observation(role="entry", signed_qty=qty)
        elif self._allow_short:
            st.positions[fill_tag] = -qty
            log.info(
                "fill_recorded",
                symbol=symbol,
                tag=fill_tag,
                qty=-qty,
                side=side_l,
                role="entry",
            )
            _emit_observation(role="entry", signed_qty=-qty)
        else:
            # Long-only engine received a SELL fill we didn't author as an
            # exit. Treat as a force-close (old behaviour) and flag it.
            st.positions[fill_tag] = 0.0
            st.entry_prices[fill_tag] = float("nan")
            st.entry_ts[fill_tag] = 0
            st.pending_exits[fill_tag] = False
            log.warning(
                "untracked_sell_treated_as_close", symbol=symbol, tag=fill_tag, qty=qty
            )

    # ── Position state rollback ───────────────────────────────────────────────
    def rollback_entry(self, symbol: str) -> None:
        """
        Called by the engine loop when a BUY order is blocked or fails after
        evaluate() has already written to positions.  Resets state so the
        engine can retry on the next qualifying bar.
        """
        tag = self.strategy_tag
        st = self._state.get(symbol)
        if st and st.is_open(tag):
            log.warning(
                "entry_rollback",
                symbol=symbol,
                tag=tag,
                reason="order_blocked_or_failed",
            )
            st.positions[tag] = 0.0
            st.entry_prices[tag] = float("nan")
            st.entry_ts[tag] = 0

    def reconcile_positions(
        self,
        alpaca_positions: list,
        alpaca_orders: list,
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
            cid = getattr(order, "client_order_id", "") or ""
            side_raw = getattr(order, "side", "")
            side = side_raw.value if hasattr(side_raw, "value") else str(side_raw)
            status_raw = getattr(order, "status", "")
            status = (
                status_raw.value if hasattr(status_raw, "value") else str(status_raw)
            )
            if "buy" in side.lower() and "filled" in status.lower():
                if sym_norm not in cid_by_sym:  # most-recent first
                    cid_by_sym[sym_norm] = cid

        # Build reverse map: "BTCUSD" → "BTC/USD" for our state keys
        norm_to_state = {s.replace("/", ""): s for s in self._state}

        for pos in alpaca_positions:
            alpaca_sym = getattr(pos, "symbol", "")  # e.g. "BTCUSD"
            state_sym = norm_to_state.get(alpaca_sym)
            if state_sym is None:
                continue  # not in our universe

            qty = float(getattr(pos, "qty", 0) or 0)
            avg_entry = float(getattr(pos, "avg_entry_price", 0) or 0)
            if qty <= 0:
                continue

            cid = cid_by_sym.get(alpaca_sym, "")
            parts = cid.split("_", 1)
            cid_tag = (
                parts[0]
                if (len(parts) > 1 and parts[0] in ("taker", "maker"))
                else None
            )

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
            st.positions[tag] = qty
            st.entry_prices[tag] = avg_entry
            st.entry_ts[tag] = int(
                time.time()
            )  # adopt-now: gives full time-stop budget
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
        hl_positions: list[dict],
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
            raw_coin = str(pos.get("coin", ""))
            # Try exact match first (preserves HIP-3 "xyz:MSTR" case),
            # then upper-case fallback for native coins ("btc" → "BTC").
            coin = raw_coin if raw_coin in coin_to_symbol else raw_coin.upper()
            state_sym = coin_to_symbol.get(coin)
            if state_sym is None or state_sym not in self._state:
                continue

            szi = float(pos.get("szi", 0) or 0)
            dust_cap = dust_caps_by_coin.get(coin, 0.0)
            if abs(szi) <= dust_cap:
                if szi != 0.0:
                    log.info(
                        "hl_reconcile_dust_skipped",
                        symbol=state_sym,
                        coin=coin,
                        szi=szi,
                        dust_cap=dust_cap,
                    )
                continue
            entry_px = float(pos.get("entry_px", 0) or 0)

            live_open_syms.add(state_sym)
            st = self._state[state_sym]
            st.positions[self.strategy_tag] = szi  # signed
            st.entry_prices[self.strategy_tag] = entry_px
            st.entry_ts[self.strategy_tag] = int(time.time())  # adopt-now
            log.info(
                "hl_position_reconciled",
                symbol=state_sym,
                coin=coin,
                tag=self.strategy_tag,
                szi=szi,
                entry_px=entry_px,
            )

        # Flat-on-chain sweep: any memory position under any of our tracked
        # tags whose symbol is NOT in live_open_syms is stale (e.g. SHADOW
        # mock entry, missed-fill WebSocket event, or a momentum rollback
        # that left positions[MOMENTUM_TAG] nonzero). Wipe so the flip-guard
        # exit deadlock self-heals on the next bar.
        for tag in (self.strategy_tag, MOMENTUM_TAG):
            for sym, st in self._state.items():
                if sym in live_open_syms:
                    continue
                if st.positions.get(tag, 0.0) != 0.0:
                    stale_qty = st.positions[tag]
                    st.positions[tag] = 0.0
                    st.entry_prices[tag] = float("nan")
                    st.entry_ts[tag] = 0
                    st.pending_exits[tag] = False
                    log.warning(
                        "hl_memory_wiped_stale",
                        symbol=sym,
                        tag=tag,
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
        st = self._state.get(symbol)
        if st and st.pending_exits.get(tag, False):
            log.warning(
                "exit_rollback", symbol=symbol, tag=tag, reason="sell_blocked_or_failed"
            )
            st.pending_exits[tag] = False

    # ── Private: Limit Price ──────────────────────────────────────────────────
    @staticmethod
    def _price_decimals(ref: float) -> int:
        """Dynamic decimal places so sub-penny assets never round to 0.00."""
        return max(2, -int(math.floor(math.log10(ref))) + 2) if ref > 0 else 2

    def _limit_px(self, st: _SymbolState, close: float, side: OrderSide) -> float:
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
                ref = (
                    st.best_bid
                    if not math.isnan(st.best_bid) and st.best_bid > 0
                    else close
                )
                # No spread-crossing adjustment for maker orders
                dec = self._price_decimals(ref)
                return round(ref, dec)
            else:
                ref = (
                    st.best_ask
                    if not math.isnan(st.best_ask) and st.best_ask > 0
                    else close
                )
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
    def _kelly_applies(self, symbol: str) -> bool:
        """True when SIZING_MODE=kelly and symbol passes the allowlist check.

        KELLY_SYMBOLS (strict per-symbol allowlist) takes precedence when set;
        otherwise falls back to KELLY_DEXES (prefix allowlist). Empty both →
        all symbols eligible.
        """
        if SIZING_MODE != "kelly":
            return False
        sym_l = symbol.lower()
        if KELLY_SYMBOLS:
            return sym_l in KELLY_SYMBOLS
        if not KELLY_DEXES:
            return True
        prefix = sym_l.split(":", 1)[0] if ":" in sym_l else ""
        return prefix in KELLY_DEXES

    def _size_order(
        self,
        symbol: str,
        price: float,
        z: float | None = None,
        phi: float | None = None,
        sigma: float | None = None,
    ) -> tuple[float, float]:
        """
        Returns (qty, notional).

        Base cap is the minimum of:
          • NOTIONAL_PER_TRADE (strategy-level cap for small account)
          • SYMBOL_CAPS[symbol] (risk_params.py per-symbol cap)
          • MAX_ORDER_NOTIONAL  (circuit-breaker hard cap)

        When SIZING_MODE=kelly and the symbol's dex is in KELLY_DEXES (or the
        allowlist is empty), the base cap is shrunk by a fractional-Kelly
        multiplier f*·k ∈ [0, KELLY_CAP]. Kelly can only reduce, never grow:
        the min() hierarchy above is still the outer clamp, enforced by the
        downstream risk gates.

        qty is floored to exchange-allowed decimal precision.
        """
        fixed_cap = min(
            self._notional_per_trade,
            SYMBOL_CAPS.get(symbol, self._notional_per_trade),
            MAX_ORDER_NOTIONAL,
        )

        # Always compute the Kelly counterfactual when inputs are valid so
        # downstream attribution can compare Kelly vs fixed ΔPnL on any entry,
        # regardless of SIZING_MODE. Live decision logic below is unchanged.
        kelly_cap: float | None = None
        f_shadow: float | None = None
        theta_shadow: float | None = None
        phi_valid = (
            z is not None and phi is not None and sigma is not None and 0.0 < phi < 1.0
        )
        if phi_valid:
            theta_shadow = -math.log(phi)
            theta_max = math.log(2.0) / max(KELLY_HL_MIN_BARS, 1e-6)
            if theta_shadow > theta_max:
                theta_shadow = theta_max
            f_shadow = kelly_fraction(
                z=z,
                theta=theta_shadow,
                sigma=sigma,
                k=KELLY_K,
                cap=KELLY_CAP,
                sigma_floor=KELLY_SIGMA_FLOOR,
            )
            kelly_cap = fixed_cap * f_shadow

        is_kelly = self._kelly_applies(symbol)

        if is_kelly:
            if phi_valid and kelly_cap is not None:
                cap = kelly_cap
                log.info(
                    "kelly_sizing",
                    symbol=symbol,
                    z=round(z, 4),
                    phi=round(phi, 4),
                    theta=round(theta_shadow, 5),
                    sigma=round(sigma, 6),
                    f=round(f_shadow, 4),
                    base_cap=round(fixed_cap, 2),
                    cap=round(cap, 2),
                )
            else:
                # Kelly active but φ invalid — skip trade (prior behavior).
                log.info(
                    "sizing_shadow",
                    symbol=symbol,
                    mode="kelly",
                    fixed_cap=round(fixed_cap, 2),
                    kelly_cap=None,
                    kelly_f=None,
                    chosen_cap=0.0,
                    skip_reason="bad_phi",
                    z=(round(z, 4) if z is not None else None),
                    phi=(round(phi, 4) if phi is not None else None),
                )
                return 0.0, 0.0
        else:
            cap = fixed_cap

        log.info(
            "sizing_shadow",
            symbol=symbol,
            mode=("kelly" if is_kelly else "fixed"),
            fixed_cap=round(fixed_cap, 2),
            kelly_cap=(round(kelly_cap, 2) if kelly_cap is not None else None),
            kelly_f=(round(f_shadow, 4) if f_shadow is not None else None),
            chosen_cap=round(cap, 2),
            z=(round(z, 4) if z is not None else None),
            phi=(round(phi, 4) if phi is not None else None),
            sigma=(round(sigma, 6) if sigma is not None else None),
        )

        if cap <= 0.0:
            return 0.0, 0.0

        decimals = _QTY_DECIMALS.get(symbol, 6)
        # Floor (not round) so actual notional never exceeds cap.
        # round() can push qty × price above cap, causing circuit breaker rejection.
        qty = math.floor(cap / price * 10**decimals) / 10**decimals

        if qty <= 0.0:
            return 0.0, 0.0

        actual_notional = round(qty * price, 2)
        return qty, actual_notional

    # ── Momentum / Trend-Following Overlay ────────────────────────────────────
    def evaluate_momentum(self, bar: dict) -> dict | None:
        """
        Momentum signal path — called AFTER evaluate() on each bar.

        Enters WITH the trend (inverted trend gate logic):
          Long:  close > SMA_240 AND z_4h > 0.5 AND z > +1.25
          Short: close < SMA_240 AND z_4h < -0.5 AND z < -1.25

        Buffers are already pushed by evaluate(); this method only reads them.
        Returns order dict with tag="momentum" or None.
        """
        sym = bar.get("symbol")
        if sym not in self._state:
            return None

        st = self._state[sym]
        close = float(bar["close"])
        tag = MOMENTUM_TAG

        # z-scores already computed by evaluate()'s push — just read
        z = st.price_buf.zscore(close)
        if z is None:
            return None
        z_4h = st.trend_buf.zscore(close)

        # Need a warm trend buffer for SMA
        if not st.trend_buf.is_full:
            return None
        trend_sma = float(np.mean(st.trend_buf._active()))

        # ── Exit path (check before entry) ────────────────────────────────────
        if st.is_open(tag):
            if st.pending_exits.get(tag, False):
                return None

            cur_qty = st.open_qty(tag)
            is_long = cur_qty > 0
            entry_px = st.entry_prices.get(tag, float("nan"))

            exit_reason: str | None = None

            # 1. Trend break — most important for momentum
            if is_long and close < trend_sma:
                exit_reason = "trend_break"
            elif not is_long and close > trend_sma:
                exit_reason = "trend_break"

            # 2. Momentum exhaustion
            # Default: sign-flip exit (long exits when z_4h<0, short exits when z_4h>0).
            # Per-symbol override: positive z_4h_exit_long defers exit until z_4h is
            # that extended (patient hold). Analogous negative for shorts.
            if z_4h is not None:
                thr_long = st.z_4h_exit_long
                thr_short = st.z_4h_exit_short
                if is_long:
                    if thr_long is not None and thr_long > 0.0:
                        if z_4h >= thr_long:
                            exit_reason = (
                                exit_reason or f"z4h_patient_exit_{thr_long:.1f}"
                            )
                    elif z_4h < 0:
                        exit_reason = exit_reason or "z4h_exhaustion"
                else:
                    if thr_short is not None and thr_short < 0.0:
                        if z_4h <= thr_short:
                            exit_reason = (
                                exit_reason or f"z4h_patient_exit_{thr_short:.1f}"
                            )
                    elif z_4h > 0:
                        exit_reason = exit_reason or "z4h_exhaustion"

            # 3. Stop loss — wider than mean-reversion (3%)
            if not math.isnan(entry_px) and entry_px > 0:
                adverse = (
                    ((entry_px - close) / entry_px)
                    if is_long
                    else ((close - entry_px) / entry_px)
                )
                if adverse >= MOMENTUM_STOP_PCT:
                    exit_reason = exit_reason or f"stop_loss_{adverse:.4f}"

            # 4. Time-stop — reuse RTH/OVN infrastructure
            entry_ts = st.entry_ts.get(tag, 0)
            if entry_ts > 0:
                age_s = int(time.time()) - entry_ts
                now_et = datetime.now(_ET)
                is_rth = (
                    now_et.weekday() < 5
                    and 930 <= now_et.hour * 100 + now_et.minute < 1600
                )
                max_secs = MAX_POSITION_SECS_RTH if is_rth else MAX_POSITION_SECS_OVN
                if age_s >= max_secs:
                    exit_reason = exit_reason or f"time_stop_{age_s}s"

            if not exit_reason:
                return None

            exit_side = OrderSide.SELL if is_long else OrderSide.BUY
            exit_qty = abs(cur_qty)
            exit_px = self._limit_px(st, close, exit_side)
            notional = round(exit_qty * close, 2)
            if not math.isnan(entry_px) and entry_px > 0:
                raw_pnl = (close - entry_px) if is_long else (entry_px - close)
                pnl_est = round(raw_pnl / entry_px * 100, 3)
            else:
                pnl_est = float("nan")
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
                reason=exit_reason,
            )
            st.pending_exits[tag] = True
            return {
                "symbol": sym,
                "side": exit_side,
                "qty": exit_qty,
                "limit_px": exit_px,
                "notional": notional,
                "_exit_pnl_pct": pnl_est,
                "_exit_tag": tag,
                "obi": round(st.obi, 4),
            }

        # ── Entry path ────────────────────────────────────────────────────────
        # Per-symbol overrides
        _z_mom = (
            st.z_momentum_entry if st.z_momentum_entry is not None else Z_MOMENTUM_ENTRY
        )
        _z_mom_short = (
            st.z_momentum_short_entry
            if st.z_momentum_short_entry is not None
            else Z_MOMENTUM_SHORT_ENTRY
        )

        # Momentum LONG: trending up + macro confirmation + short-term strength
        long_entry = (
            close > trend_sma
            and z_4h is not None
            and z_4h > Z_4H_MOMENTUM_THRESHOLD
            and z > _z_mom
        )
        # Momentum SHORT: trending down + macro confirmation + short-term weakness
        short_entry = (
            self._allow_short
            and close < trend_sma
            and z_4h is not None
            and z_4h < -Z_4H_MOMENTUM_THRESHOLD
            and z < _z_mom_short
        )

        if not (long_entry or short_entry):
            return None

        entry_side = OrderSide.BUY if long_entry else OrderSide.SELL
        direction = "long" if long_entry else "short"

        qty, notional = self._size_order(sym, close)
        if qty <= 0.0:
            return None

        limit_px = self._limit_px(st, close, entry_side)

        log.info(
            "entry_signal",
            symbol=sym,
            z=round(z, 4),
            z_4h=round(z_4h, 4) if z_4h is not None else None,
            obi=round(st.obi, 4),
            tag=tag,
            direction=direction,
            qty=qty,
            limit_px=limit_px,
            notional=notional,
            trend_sma=round(trend_sma, 2),
        )
        st.positions[tag] = qty if long_entry else -qty
        st.entry_prices[tag] = close
        st.entry_ts[tag] = int(time.time())

        return {
            "symbol": sym,
            "side": entry_side,
            "qty": qty,
            "limit_px": limit_px,
            "notional": notional,
            "obi": round(st.obi, 4),
        }

    def rollback_momentum_entry(self, symbol: str) -> None:
        """Undo evaluate_momentum() entry state when order fails."""
        tag = MOMENTUM_TAG
        st = self._state.get(symbol)
        if st and st.is_open(tag):
            log.warning(
                "entry_rollback",
                symbol=symbol,
                tag=tag,
                reason="order_blocked_or_failed",
            )
            st.positions[tag] = 0.0
            st.entry_prices[tag] = float("nan")
            st.entry_ts[tag] = 0

    def rollback_momentum_exit(self, symbol: str) -> None:
        """Clear pending_exits flag when momentum exit order fails."""
        tag = MOMENTUM_TAG
        st = self._state.get(symbol)
        if st and st.pending_exits.get(tag, False):
            log.warning(
                "exit_rollback", symbol=symbol, tag=tag, reason="sell_blocked_or_failed"
            )
            st.pending_exits[tag] = False

    def set_symbol_momentum_z(
        self,
        symbol: str,
        z_momentum_entry: float,
        z_momentum_short_entry: float,
    ) -> None:
        """Per-symbol overrides for momentum entry thresholds."""
        st = self._state.get(symbol)
        if st is None:
            return
        st.z_momentum_entry = z_momentum_entry
        st.z_momentum_short_entry = z_momentum_short_entry

    def set_symbol_z4h_exit(
        self,
        symbol: str,
        z_4h_exit_long: float,
        z_4h_exit_short: float,
    ) -> None:
        """Per-symbol patient-hold: defer momentum z_4h-exhaustion exit until
        z_4h reaches an extended level. Positive z_4h_exit_long keeps long
        positions open until z_4h>=that value. Analogous negative for shorts."""
        st = self._state.get(symbol)
        if st is None:
            return
        st.z_4h_exit_long = z_4h_exit_long
        st.z_4h_exit_short = z_4h_exit_short
