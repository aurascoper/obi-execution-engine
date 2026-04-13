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
from strategy.signals import SignalEngine, LIMIT_SLIPPAGE

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
    # CMCSA added 2026-04-13 (screener: z=-1.39σ, $27.94, 1.6M ADV, Communication)
    "CMCSA",
    "NKE",  "TSLA", "NOW",  "PODD", "VRSK", "ZS",   "NTAP", "CRM",
    "DLTR", "DG",   "DDOG", "WDAY", "CTAS", "INTU", "SMCI", "ISRG",
    "PLTR", "GPN",  "GD",   "TTD",  "ULTA", "FICO", "ORCL", "TEAM",
    "CPRT", "SNOW", "CRWD",
    # ── Short zone  z > +1.25σ (screened 2026-04-09, S&P500 ∪ NASDAQ100) ─────
    # 20 low-signal names dropped to stay under IEX 80-symbol bars+quotes cap.
    # Kept: all 5 currently active short signals + highest-conviction names.
    "INTC", "MRVL", "KLAC", "MPWR", "LRCX", "WDC",  "ETN",  "COST",
    "HUBB", "RL",   "GLW",  "TER",  "Q",    "HLT",  "WAB",
    "LITE", "HPE",  "DELL", "SRE",  "DLR",  "TGT",  "KEYS", "CMI",
    "NFLX", "ODFL", "WEC",  "MAR",  "NTRS",
    "EME",  "VRT",  "EQIX", "GWW",  "FE",
    "LYV",  "SLB",  "CSCO", "DTE",  "STZ",  "FCX",  "EIX",  "ED",
    "TSN",  "CSX",  "DUK",
    # ── Dow additions ──────────────────────────────────────────────────────────
    "CAT",
    # ── Precious metals (commodity ETFs, price > $20, ADV > 1M) ──────────────
    "GLD",  "SLV",
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
WINDOW             = 60      # rolling daily bars ≈ 3 months (Avellaneda & Lee)
Z_LONG_ENTRY       = -1.25   # long entry: price 1.25σ below rolling mean
Z_LONG_EXIT        = -0.50   # long exit:  mean-reversion mostly complete
Z_SHORT_ENTRY      =  1.25   # short entry: price 1.25σ above rolling mean
Z_SHORT_EXIT       =  0.50   # short exit (cover): mean-reversion back toward mean
OBI_THETA          = -0.001  # slightly negative: OBI=0.0 (no quote data) passes
                             # the gate, so z-score alone fires for symbols outside
                             # the QUOTE_PRIORITY set in stock_feed.py.
EQUITY_NOTIONAL    = 15.00 if __import__("os").environ.get("EXECUTION_MODE","PAPER").upper()=="LIVE" else 1_500.00

_ET        = zoneinfo.ZoneInfo("America/New_York")
_RTH_OPEN  = dtime(9, 30)
_RTH_CLOSE = dtime(16, 0)


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
            symbols            = symbols,
            window             = WINDOW,
            z_entry            = Z_LONG_ENTRY,
            z_exit             = Z_LONG_EXIT,
            obi_theta          = OBI_THETA,
            notional_per_trade = EQUITY_NOTIONAL,
        )
        self._tracker = tracker
        # Short position tracking (parallel to parent's in_position / entry_px)
        self._short:     dict[str, bool]  = {s: False       for s in symbols}
        self._short_px:  dict[str, float] = {s: float("nan") for s in symbols}
        self._short_qty: dict[str, float] = {s: 0.0         for s in symbols}
        # Long exit needs the entry qty (stored when long entry fires)
        self._long_qty:  dict[str, float] = {s: 0.0         for s in symbols}

    # ── Pre-seed ───────────────────────────────────────────────────────────────

    def preseed(self, symbol: str, closes: list[float]) -> None:
        """
        Bulk-load historical closes into the rolling buffer so zscore() returns
        a valid value on the very first live bar (no warmup period required).
        Idempotent — safe to call multiple times; latest call wins.
        """
        st = self._state.get(symbol)
        if st is None:
            return
        for c in closes:
            st.price_buf.push(float(c))
        log.info(
            "preseed_complete",
            symbol=symbol,
            bars_loaded=len(closes),
            is_full=st.price_buf.is_full,
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

        st       = self._state[sym]
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
            st.pending_exits[tag]  = False
            st.positions[tag]      = 0.0
            st.entry_prices[tag]   = float("nan")
            long_signal            = None

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
                self.rollback_entry(sym)   # undo in_position=True set by parent
                return None
            self._long_qty[sym] = long_signal["qty"]
            long_signal["action"] = "enter_long"
            return long_signal

        # 2. Long exit: parent reset in_position without emitting an order
        if was_long and not st.in_position:
            close    = float(bar["close"])
            qty      = self._long_qty.get(sym, 0.0)
            limit_px = self._sell_limit(close, st)
            if qty > 0.0 and limit_px > 0.0:
                log.info("long_exit_order", symbol=sym, qty=qty, limit_px=limit_px)
                return {
                    "symbol":   sym,
                    "side":     OrderSide.SELL,
                    "qty":      qty,
                    "limit_px": limit_px,
                    "notional": round(qty * close, 2),
                    "action":   "exit_long",
                }
            return None

        # No simultaneous long + short
        if st.in_position:
            return None

        close = float(bar["close"])
        z     = st.price_buf.zscore(close)   # buffer already updated by parent
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
                qty      = self._short_qty.get(sym, 0.0)
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
                self._short[sym]    = False
                self._short_px[sym] = float("nan")
                if qty > 0.0 and limit_px > 0.0:
                    return {
                        "symbol":   sym,
                        "side":     OrderSide.BUY,
                        "qty":      qty,
                        "limit_px": limit_px,
                        "notional": round(qty * close, 2),
                        "action":   "cover_short",
                    }
            return None   # short still live — no new entries

        # 4. Short entry — overbought + sell-side OBI pressure
        overbought    = z > Z_SHORT_ENTRY
        sell_pressure = st.obi < -OBI_THETA   # theta=0: any net ask imbalance
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
        self._short[sym]     = True
        self._short_px[sym]  = close
        self._short_qty[sym] = qty
        return {
            "symbol":   sym,
            "side":     OrderSide.SELL,
            "qty":      qty,
            "limit_px": limit_px,
            "notional": notional,
            "action":   "enter_short",
        }

    # ── Rollbacks ──────────────────────────────────────────────────────────────

    def rollback_short(self, symbol: str) -> None:
        """Called by engine when a short-entry order is blocked after state was set."""
        if self._short.get(symbol):
            log.warning("short_rollback", symbol=symbol,
                        reason="order_blocked_or_failed")
            self._short[symbol]     = False
            self._short_px[symbol]  = float("nan")
            self._short_qty[symbol] = 0.0

    # ── Private helpers ────────────────────────────────────────────────────────

    def _size_order(self, symbol: str, price: float) -> tuple[float, float]:
        """
        2-decimal fractional share sizing for equities.
        Parent defaults to 6-decimal crypto precision; equities use 2.
        """
        cap      = min(
            self._notional_per_trade,
            SYMBOL_CAPS.get(symbol, self._notional_per_trade),
            MAX_ORDER_NOTIONAL,
        )
        qty      = math.floor(cap / price * 100) / 100   # floor → never exceed cap
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
        self._tokens    = float(rate_per_minute)
        self._max       = float(rate_per_minute)
        self._interval  = 60.0 / rate_per_minute
        self._last_fill = 0.0

    async def acquire(self) -> None:
        loop = asyncio.get_running_loop()
        while self._tokens < 1.0:
            await asyncio.sleep(self._interval)
            now             = loop.time()
            refill          = (now - self._last_fill) / self._interval
            self._tokens    = min(self._max, self._tokens + refill)
            self._last_fill = now
        self._tokens -= 1.0


# ── Engine ─────────────────────────────────────────────────────────────────────
class Engine:
    def __init__(self):
        self._cfg     = load_settings()
        self._client  = TradingClient(
            self._cfg.api_key,
            self._cfg.api_secret,
            paper=self._cfg.paper,
        )
        self._breaker  = CircuitBreaker(self._client)
        self._orders   = OrderManager(self._client, self._breaker, self._cfg)
        self._tracker  = SectorExposureTracker(SECTOR_MAP, SECTOR_CAPS, MAX_SECTOR_EXPOSURE)
        self._signals  = EquitiesSignalEngine(symbols=SYMBOLS, tracker=self._tracker)
        self._bucket   = _TokenBucket(MAX_ORDERS_PER_MINUTE)
        self._msg_q    = asyncio.Queue(maxsize=2000)
        self._feed     = LiveStockFeed(self._cfg, SYMBOLS, self._msg_q)
        self._running  = True

    async def run(self) -> None:
        mode = self._cfg.execution_mode.value
        tag  = "*** LIVE ***" if self._cfg.execution_mode == ExecutionMode.LIVE else mode
        log.info(
            "equities_engine_start",
            mode=tag,
            symbols=SYMBOLS,
            paper=self._cfg.paper,
            bidirectional=True,
            z_long_entry=Z_LONG_ENTRY,
            z_short_entry=Z_SHORT_ENTRY,
        )
        print(
            f"\n[EQUITIES ENGINE] Mode={tag}  Universe={SYMBOLS}\n"
            f"  Long  entry z < {Z_LONG_ENTRY}σ  |  exit z > {Z_LONG_EXIT}σ\n"
            f"  Short entry z > {Z_SHORT_ENTRY}σ  |  cover z < {Z_SHORT_EXIT}σ\n"
            f"  Pre-seeding {WINDOW}-bar buffers from historical daily closes...\n"
            f"  Logs → logs/equities_engine.jsonl\n"
            f"  Ctrl-C to stop cleanly.\n"
        )

        await self._breaker.initialize_baseline()
        await self._preseed_from_history()
        await self._reconcile_positions()

        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._feed.run(),       name="stock_feed")
            tg.create_task(self._strategy_loop(),  name="equities_strategy")
            tg.create_task(self._drawdown_watch(), name="equities_drawdown")

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
        norm_to_sym = {s.replace("/", "").replace("-", ""): s
                       for s in self._signals._state}

        for pos in positions:
            alpaca_sym = getattr(pos, "symbol", "")
            state_sym  = norm_to_sym.get(alpaca_sym)
            if state_sym is None:
                continue

            qty       = float(getattr(pos, "qty", 0) or 0)
            avg_entry = float(getattr(pos, "avg_entry_price", 0) or 0)

            if qty > 0:
                # Long position
                self._signals._long_qty[state_sym] = qty
                log.info("equities_long_reconciled",
                         symbol=state_sym, qty=qty, avg_entry=avg_entry)
            elif qty < 0:
                # Short position
                self._signals._short[state_sym]     = True
                self._signals._short_qty[state_sym] = abs(qty)
                self._signals._short_px[state_sym]  = avg_entry
                log.info("equities_short_reconciled",
                         symbol=state_sym, qty=abs(qty), avg_entry=avg_entry)

        log.info("equities_reconcile_complete", open_positions=len(positions))

    # ── Pre-seed: bulk-load 60 historical daily closes ─────────────────────────
    async def _preseed_from_history(self) -> None:
        """
        Fetches WINDOW daily closes via StockHistoricalDataClient (IEX feed — paper
        accounts cannot access SIP historical data) and pre-loads each symbol's
        _RollingBuffer so zscore() is valid on the first live bar.

        Fetches 95 calendar days to guarantee 60+ trading days across weekends
        and holidays. Takes the last WINDOW closes per symbol.

        Falls back gracefully: if fetch fails (network, auth), logs a warning and
        continues — the engine will warm up from live 1-min bars instead (~60 min).
        """
        hist_client = StockHistoricalDataClient(
            self._cfg.api_key, self._cfg.api_secret
        )
        now   = datetime.now(timezone.utc)
        start = now - timedelta(days=95)

        log.info("preseed_fetching", symbols=SYMBOLS, lookback_days=95, feed="iex")

        try:
            req     = StockBarsRequest(
                symbol_or_symbols=SYMBOLS,
                timeframe=TimeFrame.Day,
                start=start,
                end=now,
                limit=10_000,
                feed="iex",
            )
            bars_df = await asyncio.to_thread(hist_client.get_stock_bars, req)
            df      = bars_df.df.reset_index()
        except Exception as exc:
            log.error("preseed_failed", error=str(exc))
            print(
                f"[EQUITIES ENGINE] WARNING: pre-seed failed ({exc}).\n"
                f"  Engine will warm up from live 1-min bars (~60 bars = ~60 min RTH)."
            )
            return

        for sym in SYMBOLS:
            rows   = df[df["symbol"] == sym].sort_values("timestamp")
            closes = rows["close"].values[-WINDOW:]
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
                # Belt-and-suspenders: strategy layer also enforces RTH
                now_et = datetime.now(_ET).time()
                if not (_RTH_OPEN <= now_et < _RTH_CLOSE):
                    continue

                signal = self._signals.evaluate(msg)
                if signal is None:
                    continue

                # Pop routing key before forwarding to submit_limit
                action = signal.pop("action", "")
                sym    = signal.get("symbol", "")

                await self._bucket.acquire()
                # Alpaca requires DAY (not GTC) for fractional equity orders
                result = await self._orders.submit_limit(**signal, tif=TimeInForce.DAY)

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
                    # Order blocked — rollback position state so engine can retry
                    if action == "enter_short":
                        self._signals.rollback_short(sym)
                    elif action == "enter_long":
                        self._signals.rollback_entry(sym)
                    # exit_long / cover_short: state already reset before order was
                    # attempted; no rollback available. Position stays open on Alpaca.

    # ── Drawdown watchdog — independent of strategy ────────────────────────────
    async def _drawdown_watch(self) -> None:
        while self._running:
            await asyncio.sleep(60)
            safe = await self._breaker.check_drawdown()
            if not safe:
                self._running = False
                self._feed.stop()

    def stop(self) -> None:
        log.info("equities_engine_shutdown")
        self._running = False
        self._feed.stop()


# ── Entry point ────────────────────────────────────────────────────────────────
async def main() -> None:
    engine = Engine()
    loop   = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, engine.stop)
    await engine.run()


if __name__ == "__main__":
    asyncio.run(main())
