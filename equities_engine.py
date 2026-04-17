#!/usr/bin/env python3
"""
equities_engine.py — Parallel equities execution engine.

Runs independently alongside live_engine.py (separate PID, separate asyncio loop).
Logs to logs/equities_engine.jsonl — never shares state with the crypto engine.

Strategy: same dual-gate OBI + mean-reversion as the crypto engine, extended to
support short selling since paper accounts have no PDT constraints.

  Long entry:   z < -1.25  AND  OBI >  0.00  (oversold + buy pressure)
  Long exit:    z > -0.50                     (mean-reversion sufficient)
  Short entry:  z > +1.25  AND  OBI <  0.00  (overbought + sell pressure)
  Short exit:   z < +0.50                     (mean-reversion back toward mean)

Pre-seeding (critical): before the WebSocket starts, StockHistoricalDataClient
fetches the last 60 trading days of daily closes and bulk-loads each symbol's
_RollingBuffer. The engine is mathematically warm on the first live bar — no
60-day wait. Falls back gracefully to live warmup if the fetch fails.

Usage:
  export EXECUTION_MODE=PAPER
  export ALPACA_TRADING_MODE=paper
  source env.sh && nohup /Users/aurascoper/finance/bin/python3 equities_engine.py \\
      >> logs/equities_engine.jsonl 2>&1 &
"""

import asyncio
import logging
import math
import signal
import zoneinfo
from datetime import datetime, time as dtime, timezone, timedelta
from pathlib import Path

import numpy as np
import structlog
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce

from config.settings import ExecutionMode, load as load_settings
from config.risk_params import MAX_ORDERS_PER_MINUTE, MAX_ORDER_NOTIONAL, SYMBOL_CAPS
from config.universe import SECTOR_MAP, SECTOR_CAPS, MAX_SECTOR_EXPOSURE
from risk.circuit_breaker import CircuitBreaker
from risk.sector_tracker import SectorExposureTracker
from data.stock_feed import LiveStockFeed
from execution.order_manager import OrderManager
from strategy.signals import SignalEngine, LIMIT_SLIPPAGE, MOMENTUM_TAG

# ── Logging setup ──────────────────────────────────────────────────────────────
Path("logs").mkdir(exist_ok=True)

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
    logger_factory=structlog.WriteLoggerFactory(
        file=open("logs/equities_engine.jsonl", "a", buffering=1)
    ),
)
log = structlog.get_logger("equities_engine")

# ── Universe ───────────────────────────────────────────────────────────────────
# Screened for z < -1.25 at engine build time (2026-04-09).
# Add/remove symbols here; pre-seed and circuit breaker adapt automatically.
SYMBOLS = [
    # ── Long zone  z < -1.25σ (screened 2026-04-09, S&P500 ∪ NASDAQ100) ──────
    # Trimmed to highest-conviction names; defensive/staples longs dropped to
    # stay within IEX free-tier 100-symbol WebSocket cap.
    # 2026-04-14 fundamentals purge — mean-reversion invalid on regime-break
    # names (z-magnet works on cyclical dislocations, not secular shifts):
    #   NKE   — fundamentals flagged
    #   TTD   — agency-trust moat cracking, exec exits, Publicis delisting
    #   FICO  — Senate pricing probe + FHFA/VantageScore regulatory risk
    #   NOW   — AI-automation moat erosion (UBS)
    #   WDAY  — AI agents cannibalizing HCM/finance SaaS seats
    #   INTU  — AI disruption of TurboTax/QuickBooks
    #   CMCSA — structural cord-cutting value trap
    #   SMCI  — governance/accounting binary gap risk
    "TSLA",
    "PODD",
    "VRSK",
    "ZS",
    "NTAP",
    "CRM",
    "DLTR",
    "DG",
    "DDOG",
    "CTAS",
    "ISRG",
    "PLTR",
    "GPN",
    "GD",
    "ULTA",
    "ORCL",
    "TEAM",
    "CPRT",
    "SNOW",
    "CRWD",
    # ── Short zone  z > +1.25σ (screened 2026-04-09, S&P500 ∪ NASDAQ100) ─────
    # 20 low-signal names dropped to stay under IEX 80-symbol bars+quotes cap.
    # Kept: all 5 currently active short signals + highest-conviction names.
    # INTC — moved to momentum universe (2026-04-17: +3.21σ, 99% above SMA-240)
    "MRVL",
    "KLAC",
    "MPWR",
    "LRCX",
    "WDC",
    "ETN",
    "COST",
    "HUBB",
    "RL",
    "GLW",
    "TER",
    "Q",
    "HLT",
    "WAB",
    "LITE",
    # HPE — moved to momentum universe (2026-04-17: +2.18σ, 18% above SMA-240)
    "DELL",
    "SRE",
    "DLR",
    "TGT",
    "KEYS",
    "CMI",
    "NFLX",
    "ODFL",
    "WEC",
    "MAR",
    "NTRS",
    "EME",
    "VRT",
    "EQIX",
    "GWW",
    "FE",
    "LYV",
    "SLB",
    # CSCO — moved to momentum universe (2026-04-17: +1.72σ, 17% above SMA-240)
    "DTE",
    "STZ",
    "FCX",
    "EIX",
    "ED",
    "TSN",
    "CSX",
    "DUK",
    # ── Dow additions ──────────────────────────────────────────────────────────
    "CAT",
    # ── Precious metals (commodity ETFs, price > $20, ADV > 1M) ──────────────
    "GLD",
    "SLV",
    # ── Energy commodities ────────────────────────────────────────────────────
    # ⚠️  MACRO WARNING (2026-04): Iran war risk → crude oil is a long-side hedge.
    # USO is capped at 1 (sector "Energy ETF"). Do NOT raise cap while Iran
    # tensions are elevated — a short squeeze on oil can be violent and rapid.
    "USO",
    # ── Nuclear / uranium ─────────────────────────────────────────────────────
    # ⚠️  MACRO WARNING: Iran nuclear program makes uranium geopolitically
    # sensitive. URA capped at 1 ("Nuclear Energy" sector).
    "URA",
]

# ── Strategy parameters ────────────────────────────────────────────────────────
WINDOW = 60  # rolling daily bars ≈ 3 months (Avellaneda & Lee)
Z_LONG_ENTRY = -1.25  # long entry: price 1.25σ below rolling mean
Z_LONG_EXIT = -0.50  # long exit:  mean-reversion mostly complete
Z_SHORT_ENTRY = 1.25  # short entry: price 1.25σ above rolling mean
Z_SHORT_EXIT = 0.50  # short exit (cover): mean-reversion back toward mean
OBI_THETA = -0.001  # slightly negative: OBI=0.0 (no quote data) passes
# the gate, so z-score alone fires for symbols outside
# the QUOTE_PRIORITY set in stock_feed.py.
EQUITY_NOTIONAL = (
    15.00
    if __import__("os").environ.get("EXECUTION_MODE", "PAPER").upper() == "LIVE"
    else 1_500.00
)

_ET = zoneinfo.ZoneInfo("America/New_York")
_RTH_OPEN = dtime(9, 30)
_RTH_CLOSE = dtime(16, 0)

# ── Momentum universe (must NOT overlap with SYMBOLS) ─────────────────────────
# Default: screener --momentum results from 2026-04-17
_MOMENTUM_DEFAULT = "NVDA,AMZN,NKE,INTC,HPE,CSCO"
_momentum_raw = __import__("os").environ.get("MOMENTUM_EQUITIES", _MOMENTUM_DEFAULT)
MOMENTUM_EQUITIES: set[str] = {s.strip() for s in _momentum_raw.split(",") if s.strip()}
_overlap = MOMENTUM_EQUITIES & set(SYMBOLS)
if _overlap:
    raise RuntimeError(
        f"MOMENTUM_EQUITIES overlaps mean-reversion SYMBOLS: {_overlap}. "
        "Momentum and mean-reversion must use non-overlapping universes."
    )


# ── Equities Signal Engine ─────────────────────────────────────────────────────
class EquitiesSignalEngine(SignalEngine):
    """
    Extends SignalEngine with:
      1. preseed(symbol, closes)   — bulk-loads historical closes at startup
      2. Short-side entry/exit logic symmetric to the long side
      3. Long exit order emission  — parent logs the exit but doesn't submit a
                                     SELL order; this subclass detects the state
                                     transition and returns a close order dict
      4. _size_order override      — uses 2-decimal fractional share precision
                                     (parent defaults to crypto 6-decimal precision)
    """

    def __init__(self, symbols: list[str], tracker: SectorExposureTracker) -> None:
        super().__init__(
            symbols=symbols,
            window=WINDOW,
            z_entry=Z_LONG_ENTRY,
            z_exit=Z_LONG_EXIT,
            obi_theta=OBI_THETA,
            notional_per_trade=EQUITY_NOTIONAL,
        )
        self._tracker = tracker
        # Short position tracking (parallel to parent's in_position / entry_px)
        self._short: dict[str, bool] = {s: False for s in symbols}
        self._short_px: dict[str, float] = {s: float("nan") for s in symbols}
        self._short_qty: dict[str, float] = {s: 0.0 for s in symbols}
        # Long exit needs the entry qty (stored when long entry fires)
        self._long_qty: dict[str, float] = {s: 0.0 for s in symbols}

    # ── Pre-seed ───────────────────────────────────────────────────────────────

    def preseed(self, symbol: str, closes: list[float]) -> None:
        """
        Bulk-load historical closes into BOTH rolling buffers so zscore() and
        the trend gate / momentum overlay are valid on the very first live bar.
        Idempotent — safe to call multiple times; latest call wins.
        """
        st = self._state.get(symbol)
        if st is None:
            return
        for c in closes:
            st.price_buf.push(float(c))
            st.trend_buf.push(float(c))
        log.info(
            "preseed_complete",
            symbol=symbol,
            bars_loaded=len(closes),
            price_buf_full=st.price_buf.is_full,
            trend_buf_full=st.trend_buf.is_full,
        )

    # ── Core evaluate — bi-directional ────────────────────────────────────────

    def evaluate(self, bar: dict) -> dict | None:
        """
        Evaluates one bar for long AND short signals.

        Execution priority (at most one signal per bar per symbol):
          1. Long entry  — delegates entirely to parent; returns BUY dict
          2. Long exit   — detected via state transition; returns SELL dict
          3. Short exit  — z reverted below Z_SHORT_EXIT; returns BUY (cover) dict
          4. Short entry — overbought + sell-side OBI; returns SELL (short) dict

        All returned dicts carry an "action" key for engine-level routing:
          "enter_long" | "exit_long" | "enter_short" | "cover_short"
        The engine pops "action" before passing the dict to submit_limit.
        """
        sym = bar.get("symbol")
        if sym not in self._state:
            return None

        st = self._state[sym]
        was_long = st.in_position  # snapshot before parent mutates

        # --- Parent handles: buffer push, z-score, long entry/exit ---
        long_signal = super().evaluate(bar)

        # Phase 3 compat: parent now returns SELL dicts on exit instead of
        # silently clearing in_position.  EquitiesSignalEngine builds its own
        # exit order (with action= metadata and equities-specific _sell_limit),
        # so suppress the parent's SELL and restore the pre-Phase-3 contract:
        # zero parent state immediately so block 2's detection still works.
        # (Equities does not use TradingStream fills for position clearing.)
        if long_signal is not None and long_signal.get("side") == OrderSide.SELL:
            tag = self.strategy_tag
            st.pending_exits[tag] = False
            st.positions[tag] = 0.0
            st.entry_prices[tag] = float("nan")
            long_signal = None

        # 1. Long entry fired — check sector cap before committing
        if long_signal is not None:
            if not self._tracker.check(sym):
                sector = self._tracker.sector_of(sym)
                log.warning(
                    "signal_rejected_sector_cap",
                    symbol=sym,
                    sector=sector,
                    side="long",
                    exposure=self._tracker.snapshot().get(sector, 0),
                    cap=SECTOR_CAPS.get(sector, MAX_SECTOR_EXPOSURE),
                )
                self.rollback_entry(sym)  # undo in_position=True set by parent
                return None
            self._long_qty[sym] = long_signal["qty"]
            long_signal["action"] = "enter_long"
            return long_signal

        # 2. Long exit: parent reset in_position without emitting an order
        if was_long and not st.in_position:
            close = float(bar["close"])
            qty = self._long_qty.get(sym, 0.0)
            limit_px = self._sell_limit(close, st)
            if qty > 0.0 and limit_px > 0.0:
                log.info("long_exit_order", symbol=sym, qty=qty, limit_px=limit_px)
                return {
                    "symbol": sym,
                    "side": OrderSide.SELL,
                    "qty": qty,
                    "limit_px": limit_px,
                    "notional": round(qty * close, 2),
                    "action": "exit_long",
                }
            return None

        # No simultaneous long + short
        if st.in_position:
            return None

        close = float(bar["close"])
        z = st.price_buf.zscore(close)  # buffer already updated by parent
        if z is None:
            return None

        log.debug(
            "signal_tick_short_side",
            symbol=sym,
            z=round(z, 4),
            obi=round(st.obi, 4),
            short=self._short[sym],
        )

        # 3. Short exit (cover) — z reverted back below exit threshold
        if self._short[sym]:
            if z < Z_SHORT_EXIT:
                qty = self._short_qty.get(sym, 0.0)
                limit_px = self._buy_limit(close, st)
                log.info(
                    "short_exit_signal",
                    symbol=sym,
                    z=round(z, 4),
                    entry_px=self._short_px[sym],
                    close=close,
                    pnl_est=round(
                        (self._short_px[sym] - close) / self._short_px[sym] * 100, 3
                    ),
                )
                self._short[sym] = False
                self._short_px[sym] = float("nan")
                if qty > 0.0 and limit_px > 0.0:
                    return {
                        "symbol": sym,
                        "side": OrderSide.BUY,
                        "qty": qty,
                        "limit_px": limit_px,
                        "notional": round(qty * close, 2),
                        "action": "cover_short",
                    }
            return None  # short still live — no new entries

        # 4. Short entry — overbought + sell-side OBI pressure
        overbought = z > Z_SHORT_ENTRY
        sell_pressure = st.obi < -OBI_THETA  # theta=0: any net ask imbalance
        if not (overbought and sell_pressure):
            return None

        # Sector cap check before setting short state
        if not self._tracker.check(sym):
            sector = self._tracker.sector_of(sym)
            log.warning(
                "signal_rejected_sector_cap",
                symbol=sym,
                sector=sector,
                side="short",
                exposure=self._tracker.snapshot().get(sector, 0),
                cap=SECTOR_CAPS.get(sector, MAX_SECTOR_EXPOSURE),
            )
            return None

        qty, notional = self._size_order(sym, close)
        if qty <= 0.0:
            log.warning("short_sizing_zero", symbol=sym, close=close)
            return None
        limit_px = self._sell_limit(close, st)
        if limit_px <= 0.0:
            log.warning("short_limit_px_zero", symbol=sym, close=close)
            return None

        log.info(
            "short_entry_signal",
            symbol=sym,
            z=round(z, 4),
            obi=round(st.obi, 4),
            qty=qty,
            limit_px=limit_px,
            notional=notional,
        )
        self._short[sym] = True
        self._short_px[sym] = close
        self._short_qty[sym] = qty
        return {
            "symbol": sym,
            "side": OrderSide.SELL,
            "qty": qty,
            "limit_px": limit_px,
            "notional": notional,
            "action": "enter_short",
        }

    # ── Rollbacks ──────────────────────────────────────────────────────────────

    def rollback_short(self, symbol: str) -> None:
        """Called by engine when a short-entry order is blocked after state was set."""
        if self._short.get(symbol):
            log.warning(
                "short_rollback", symbol=symbol, reason="order_blocked_or_failed"
            )
            self._short[symbol] = False
            self._short_px[symbol] = float("nan")
            self._short_qty[symbol] = 0.0

    # ── Private helpers ────────────────────────────────────────────────────────

    def _size_order(self, symbol: str, price: float) -> tuple[float, float]:
        """
        2-decimal fractional share sizing for equities.
        Parent defaults to 6-decimal crypto precision; equities use 2.
        """
        cap = min(
            self._notional_per_trade,
            SYMBOL_CAPS.get(symbol, self._notional_per_trade),
            MAX_ORDER_NOTIONAL,
        )
        qty = math.floor(cap / price * 100) / 100  # floor → never exceed cap
        if qty <= 0.0:
            return 0.0, 0.0
        return qty, round(qty * price, 2)

    def _sell_limit(self, close: float, st) -> float:
        """
        Aggressive sell limit for short entry and long close.
        Slightly below close so order is competitive without chasing the bid.
        Uses 2 decimal places (equities are never sub-penny).
        """
        return round(close * (1.0 - LIMIT_SLIPPAGE), 2)

    def _buy_limit(self, close: float, st) -> float:
        """
        Aggressive buy limit for long entry and short cover.
        Slightly above close to cross the spread; mirrors parent logic.
        Uses best_ask if available.
        """
        ref = close
        if not np.isnan(st.best_ask) and st.best_ask > 0:
            ref = max(close, st.best_ask)
        return round(ref * (1.0 + LIMIT_SLIPPAGE), 2)


# ── Token bucket ───────────────────────────────────────────────────────────────
class _TokenBucket:
    def __init__(self, rate_per_minute: int):
        self._tokens = float(rate_per_minute)
        self._max = float(rate_per_minute)
        self._interval = 60.0 / rate_per_minute
        self._last_fill = 0.0

    async def acquire(self) -> None:
        loop = asyncio.get_running_loop()
        while self._tokens < 1.0:
            await asyncio.sleep(self._interval)
            now = loop.time()
            refill = (now - self._last_fill) / self._interval
            self._tokens = min(self._max, self._tokens + refill)
            self._last_fill = now
        self._tokens -= 1.0


# ── Engine ─────────────────────────────────────────────────────────────────────
class Engine:
    def __init__(self):
        self._cfg = load_settings()
        self._client = TradingClient(
            self._cfg.api_key,
            self._cfg.api_secret,
            paper=self._cfg.paper,
        )
        self._breaker = CircuitBreaker(self._client)
        self._orders = OrderManager(self._client, self._breaker, self._cfg)
        self._tracker = SectorExposureTracker(
            SECTOR_MAP, SECTOR_CAPS, MAX_SECTOR_EXPOSURE
        )
        self._all_symbols = SYMBOLS + sorted(MOMENTUM_EQUITIES - set(SYMBOLS))
        self._signals = EquitiesSignalEngine(
            symbols=self._all_symbols, tracker=self._tracker
        )
        self._bucket = _TokenBucket(MAX_ORDERS_PER_MINUTE)
        self._msg_q = asyncio.Queue(maxsize=2000)
        self._feed = LiveStockFeed(self._cfg, self._all_symbols, self._msg_q)
        self._running = True

    async def run(self) -> None:
        mode = self._cfg.execution_mode.value
        tag = "*** LIVE ***" if self._cfg.execution_mode == ExecutionMode.LIVE else mode
        log.info(
            "equities_engine_start",
            mode=tag,
            symbols=SYMBOLS,
            momentum_symbols=sorted(MOMENTUM_EQUITIES),
            paper=self._cfg.paper,
            bidirectional=True,
            z_long_entry=Z_LONG_ENTRY,
            z_short_entry=Z_SHORT_ENTRY,
        )
        mom_line = (
            f"  Momentum universe: {sorted(MOMENTUM_EQUITIES)}\n"
            if MOMENTUM_EQUITIES
            else ""
        )
        print(
            f"\n[EQUITIES ENGINE] Mode={tag}  Universe={SYMBOLS}\n"
            f"{mom_line}"
            f"  Long  entry z < {Z_LONG_ENTRY}σ  |  exit z > {Z_LONG_EXIT}σ\n"
            f"  Short entry z > {Z_SHORT_ENTRY}σ  |  cover z < {Z_SHORT_EXIT}σ\n"
            f"  Pre-seeding 240+60 bar buffers from historical daily closes...\n"
            f"  Logs → logs/equities_engine.jsonl\n"
            f"  Ctrl-C to stop cleanly.\n"
        )

        await self._breaker.initialize_baseline()
        await self._preseed_from_history()
        await self._reconcile_positions()

        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._feed.run(), name="stock_feed")
            tg.create_task(self._strategy_loop(), name="equities_strategy")
            tg.create_task(self._drawdown_watch(), name="equities_drawdown")
            tg.create_task(self._heartbeat(), name="equities_heartbeat")

    # ── Startup position reconciliation ───────────────────────────────────────
    async def _reconcile_positions(self) -> None:
        """
        Seed signal state from Alpaca's open positions on startup.
        Handles both Phase-3-tagged orders and pre-Phase-3 UUID orders (Bug 4).

        For equities, also seeds _long_qty / _short / _short_qty / _short_px
        so the strategy loop knows to exit rather than re-enter.
        """
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus

        positions = await asyncio.to_thread(self._client.get_all_positions)
        if not positions:
            log.info("equities_reconcile_complete", open_positions=0)
            return

        orders = await asyncio.to_thread(
            self._client.get_orders,
            GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=100),
        )

        # Seed parent SignalEngine state (positions / entry_prices dicts)
        self._signals.reconcile_positions(positions, orders)

        # Also seed equities-specific long/short tracking dicts
        norm_to_sym = {
            s.replace("/", "").replace("-", ""): s for s in self._signals._state
        }

        for pos in positions:
            alpaca_sym = getattr(pos, "symbol", "")
            state_sym = norm_to_sym.get(alpaca_sym)
            if state_sym is None:
                continue

            qty = float(getattr(pos, "qty", 0) or 0)
            avg_entry = float(getattr(pos, "avg_entry_price", 0) or 0)

            if qty > 0:
                # Long position
                self._signals._long_qty[state_sym] = qty
                log.info(
                    "equities_long_reconciled",
                    symbol=state_sym,
                    qty=qty,
                    avg_entry=avg_entry,
                )
            elif qty < 0:
                # Short position
                self._signals._short[state_sym] = True
                self._signals._short_qty[state_sym] = abs(qty)
                self._signals._short_px[state_sym] = avg_entry
                log.info(
                    "equities_short_reconciled",
                    symbol=state_sym,
                    qty=abs(qty),
                    avg_entry=avg_entry,
                )

        log.info("equities_reconcile_complete", open_positions=len(positions))

    # ── Pre-seed: bulk-load 60 historical daily closes ─────────────────────────
    async def _preseed_from_history(self) -> None:
        """
        Fetches daily closes via StockHistoricalDataClient (IEX feed) and pre-loads
        each symbol's price_buf (60 bars) AND trend_buf (240 bars) so both zscore()
        and the 240-bar trend gate / momentum overlay are valid on the first live bar.

        Fetches 500 calendar days to guarantee 240+ trading days across weekends
        and holidays. Both buffers are RollingBuffers, so overflow is handled
        automatically — just push all available closes.

        Falls back gracefully: if fetch fails (network, auth), logs a warning and
        continues — the engine will warm up from live bars instead.
        """
        hist_client = StockHistoricalDataClient(self._cfg.api_key, self._cfg.api_secret)
        now = datetime.now(timezone.utc)
        lookback_days = 500  # ~350 trading days, enough for 240-bar trend buffer
        start = now - timedelta(days=lookback_days)

        log.info(
            "preseed_fetching",
            symbols=self._all_symbols,
            lookback_days=lookback_days,
            feed="iex",
        )

        try:
            # Batch to avoid Alpaca's 10k row cap: 72 symbols × 350 days = 25k rows.
            all_rows = []
            batch_size = 25
            for i in range(0, len(self._all_symbols), batch_size):
                batch = self._all_symbols[i : i + batch_size]
                req = StockBarsRequest(
                    symbol_or_symbols=batch,
                    timeframe=TimeFrame.Day,
                    start=start,
                    end=now,
                    limit=10_000,
                    feed="iex",
                )
                chunk = await asyncio.to_thread(hist_client.get_stock_bars, req)
                all_rows.append(chunk.df.reset_index())
            import pandas as pd

            df = pd.concat(all_rows, ignore_index=True) if all_rows else pd.DataFrame()
        except Exception as exc:
            log.error("preseed_failed", error=str(exc))
            print(
                f"[EQUITIES ENGINE] WARNING: pre-seed failed ({exc}).\n"
                f"  Engine will warm up from live 1-min bars (~60 bars = ~60 min RTH)."
            )
            return

        for sym in self._all_symbols:
            rows = df[df["symbol"] == sym].sort_values("timestamp")
            closes = rows["close"].values  # all available — buffers handle overflow
            if len(closes) == 0:
                log.warning("preseed_no_data", symbol=sym)
                continue
            self._signals.preseed(sym, closes.tolist())

    # ── Strategy loop ──────────────────────────────────────────────────────────
    async def _strategy_loop(self) -> None:
        while self._running:
            if self._breaker.halted:
                log.critical("equities_engine_halted")
                self._running = False
                self._feed.stop()
                break

            msg = await self._msg_q.get()

            if msg["type"] == "orderbook":
                self._signals.update_orderbook(msg)
                continue

            if msg["type"] == "bar":
                sym = msg.get("symbol", "")

                # Momentum-only symbols skip mean-reversion entirely
                if sym in MOMENTUM_EQUITIES:
                    # evaluate() pushes buffers; ignore its mean-reversion signal
                    self._signals.evaluate(msg)
                    st = self._signals._state.get(sym)
                    was_open = st.is_open(MOMENTUM_TAG) if st else False
                    signal = self._signals.evaluate_momentum(msg)
                    if signal is None:
                        continue
                    # Determine action from position state transition
                    if was_open:
                        # Was in position → this is an exit
                        signal["action"] = (
                            "exit_long"
                            if signal["side"] == OrderSide.SELL
                            else "cover_short"
                        )
                    else:
                        # Was flat → this is an entry
                        signal["action"] = (
                            "enter_long"
                            if signal["side"] == OrderSide.BUY
                            else "enter_short"
                        )
                else:
                    # Daily bars arrive after market close — no RTH guard needed.
                    signal = self._signals.evaluate(msg)
                    if signal is None:
                        continue

                # Pop routing key before forwarding to submit_limit
                action = signal.pop("action", "")
                sym = signal.get("symbol", "")

                # Live Alpaca accounts require shorting privileges to place SELL
                # orders without an existing long position.  Skip short entries
                # on LIVE and roll back the state set by evaluate() so the engine
                # can cleanly re-evaluate on the next bar.
                if (
                    action == "enter_short"
                    and self._cfg.execution_mode == ExecutionMode.LIVE
                ):
                    self._signals.rollback_short(sym)
                    log.warning(
                        "short_skipped_no_privilege",
                        symbol=sym,
                        mode=self._cfg.execution_mode.value,
                    )
                    continue

                await self._bucket.acquire()
                # Alpaca requires DAY (not GTC) for fractional equity orders
                try:
                    result = await self._orders.submit_limit(
                        **signal, tif=TimeInForce.DAY
                    )
                except Exception as exc:
                    # Transient network / Alpaca failure (e.g. ConnectionResetError).
                    # Treat like a blocked order: log, roll back, survive to next bar.
                    log.warning(
                        "equities_order_network_error",
                        action=action,
                        symbol=sym,
                        exc_type=type(exc).__name__,
                        exc_msg=str(exc)[:160],
                    )
                    result = None

                if result:
                    log.info("equities_order_result", action=action, **result)
                    # Update sector exposure on confirmed submission
                    if action in ("enter_long", "enter_short"):
                        self._tracker.open(sym)
                    elif action in ("exit_long", "cover_short"):
                        self._tracker.close(sym)
                    snap = self._tracker.snapshot()
                    if snap:
                        log.debug("sector_exposure_snapshot", exposure=snap)
                else:
                    # Order blocked or errored — rollback position state so engine can retry
                    is_momentum = sym in MOMENTUM_EQUITIES
                    if is_momentum and action in ("enter_long", "enter_short"):
                        self._signals.rollback_momentum_entry(sym)
                    elif is_momentum and action in ("exit_long", "cover_short"):
                        self._signals.rollback_momentum_exit(sym)
                    elif action == "enter_short":
                        self._signals.rollback_short(sym)
                    elif action == "enter_long":
                        self._signals.rollback_entry(sym)
                    # exit_long / cover_short (mean-reversion): state already reset
                    # before order was attempted; no rollback available.

    # ── Drawdown watchdog — independent of strategy ────────────────────────────
    async def _drawdown_watch(self) -> None:
        while self._running:
            await asyncio.sleep(60)
            safe = await self._breaker.check_drawdown()
            if not safe:
                self._running = False
                self._feed.stop()

    async def _heartbeat(self) -> None:
        while self._running:
            await asyncio.sleep(300)
            log.info("equities_heartbeat", halted=not self._running)

    def stop(self) -> None:
        log.info("equities_engine_shutdown")
        self._running = False
        self._feed.stop()


# ── Entry point ────────────────────────────────────────────────────────────────
async def main() -> None:
    engine = Engine()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, engine.stop)
    await engine.run()


if __name__ == "__main__":
    asyncio.run(main())
