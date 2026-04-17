#!/usr/bin/env python3
"""
options_engine.py — OBI-gated options execution engine.

Runs independently alongside equities_engine.py (separate PID, separate asyncio
loop).  Uses the same underlying equity bar + NBBO quote feed to drive the OBI
+ z-score signal, but executes options contracts instead of shares.

Strategy levels (set OPTIONS_LEVEL env var):
  1 — Cash-secured puts on bullish signals  (requires buying power ≈ strike × 100)
  2 — Long calls (bullish) / long puts (bearish)               [DEFAULT]
  3 — Bull call spreads / bear put spreads  (defined risk)

Universe: liquid optionable names with tight bid-ask spreads.
          All must be in the Alpaca options chain with MIN_OPEN_INTEREST > 50.

Pre-seeding: fetches last 60 daily closes at startup (same as equities_engine.py)
             so the z-score window is warm on the first live bar.

Safety guards:
  • No new entries after OPTIONS_NO_ENTRY_HOUR:00 ET (default 15:00 / 3 PM)
  • DTE monitor closes any position with ≤ 2 days to expiry
  • Options budget capped at MAX_OPTIONS_BUDGET per trade
  • Circuit breaker daily loss / drawdown halt shared with other engines
  • Fractional orders never submitted (options qty is always integer)

Usage:
  export EXECUTION_MODE=LIVE
  export ALPACA_TRADING_MODE=live
  export OPTIONS_LEVEL=2          # 1 | 2 | 3
  source env.sh
  caffeinate -i python3 options_engine.py
"""

import asyncio
import logging
import os
import signal
import zoneinfo
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

import structlog
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import TimeInForce

from config.settings import ExecutionMode, load as load_settings
from config.risk_params import MAX_ORDERS_PER_MINUTE
from risk.circuit_breaker import CircuitBreaker
from data.rest_stock_poller import RestStockPoller
from data.options_chain import OptionsChainCache
from execution.order_manager import OrderManager
from strategy.options_signals import OptionsSignalEngine, WINDOW

# ── Logging ────────────────────────────────────────────────────────────────────
Path("logs").mkdir(exist_ok=True)

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
    logger_factory=structlog.WriteLoggerFactory(
        file=open("logs/options_engine.jsonl", "a", buffering=1)
    ),
)
log = structlog.get_logger("options_engine")

# ── Universe ───────────────────────────────────────────────────────────────────
# Chosen for options liquidity: tight bid-ask, high open interest, liquid chains.
# All names have weekly expirations (Friday) available at major brokers.
#
# Account sizing note ($345 equity):
#   A $0.50/share near-ATM call on a $20 stock = $50 total (1 contract).
#   Most mega-cap tech (NVDA, TSLA) will have premiums > $3/share → too expensive
#   unless filtered out by the MAX_OPTIONS_BUDGET cap.
#   The engine will log "options_no_call_found" and skip any symbol where the
#   cheapest qualifying contract exceeds the budget.
SYMBOLS = [
    # Broad market — deepest options liquidity, always within budget on weeklies
    "SPY",  # S&P 500 ETF; tightest spread in the market
    "QQQ",  # Nasdaq 100 ETF; high beta, good for mean-reversion calls
    # Individual names — selected for relatively cheap premiums
    "INTC",  # ~$20-30 range; ATM 7-DTE call ~$0.40-0.80/share
    "CSCO",  # ~$50-60 range; ATM weeklies ~$0.60-1.20/share
    "WDC",  # ~$30-50 range
    "HPE",  # ~$15-20 range; cheapest premiums in the universe
    "SMCI",  # ~$30-50 range; high volatility, good mean-reversion signals
    "PLTR",  # ~$25-40 range; high beta, liquid options
    "CRWD",  # ~$300+ range; will typically exceed budget → skip gracefully
    "AMD",  # ~$100-150 range; will typically exceed budget → skip gracefully
    # ── Short-zone adds from screener 2026-04-14 (puts on overbought) ─────────
    "WULF",  # ~$20 range; cheap weekly puts should fit $110 budget
    "AMZN",  # ~$246; usually budget-skipped, catch cheap OTM on IV spikes
    "NVDA",  # ~$192; usually budget-skipped, catch cheap OTM on IV spikes
]
# NOTE: CRWD, AMD, AMZN, NVDA are included to catch cheap OTM options in
# high-vol regimes, but will usually be skipped by MAX_OPTIONS_BUDGET at
# normal IV levels. Zero financial risk — best_contract() returns None when
# nothing qualifies.

# ── Configuration ──────────────────────────────────────────────────────────────
_ET = zoneinfo.ZoneInfo("America/New_York")
_RTH_OPEN = dtime(9, 30)
_RTH_CLOSE = dtime(16, 0)
_NO_ENTRY_HOUR = int(os.environ.get("OPTIONS_NO_ENTRY_HOUR", "15"))  # 3 PM default
_STRATEGY_LEVEL = int(os.environ.get("OPTIONS_LEVEL", "2"))
_DTE_MONITOR_INTERVAL_S = 1800  # check for near-expiry positions every 30 min


# ── Token bucket ───────────────────────────────────────────────────────────────
class _TokenBucket:
    def __init__(self, rate_per_minute: int) -> None:
        self._tokens = float(rate_per_minute)
        self._max = float(rate_per_minute)
        self._interval = 60.0 / rate_per_minute
        self._last = 0.0

    async def acquire(self) -> None:
        loop = asyncio.get_running_loop()
        while self._tokens < 1.0:
            await asyncio.sleep(self._interval)
            now = loop.time()
            self._tokens = min(
                self._max, self._tokens + (now - self._last) / self._interval
            )
            self._last = now
        self._tokens -= 1.0


# ── Engine ─────────────────────────────────────────────────────────────────────
class OptionsEngine:
    def __init__(self) -> None:
        self._cfg = load_settings()
        self._client = TradingClient(
            self._cfg.api_key,
            self._cfg.api_secret,
            paper=self._cfg.paper,
        )
        self._data_client = StockHistoricalDataClient(
            api_key=self._cfg.api_key,
            secret_key=self._cfg.api_secret,
        )
        self._opt_data_client = OptionHistoricalDataClient(
            api_key=self._cfg.api_key,
            secret_key=self._cfg.api_secret,
        )
        self._breaker = CircuitBreaker(self._client)
        self._orders = OrderManager(
            self._client,
            self._breaker,
            self._cfg,
            strategy_tag="options",
            asset_class="option",
        )
        self._chain = OptionsChainCache(
            trading_client=self._client,
            data_client=self._opt_data_client,
            underlyings=SYMBOLS,
            min_dte=7,
            max_dte=21,
        )
        self._signals = OptionsSignalEngine(
            chain=self._chain,
            symbols=SYMBOLS,
            strategy_level=_STRATEGY_LEVEL,
            strategy_tag="options",
        )
        self._msg_q = asyncio.Queue(maxsize=2000)
        self._feed = RestStockPoller(self._data_client, SYMBOLS, self._msg_q)
        self._bucket = _TokenBucket(MAX_ORDERS_PER_MINUTE)
        self._running = True

    async def run(self) -> None:
        mode = self._cfg.execution_mode.value
        tag = "*** LIVE ***" if self._cfg.execution_mode == ExecutionMode.LIVE else mode
        log.info(
            "options_engine_start",
            mode=tag,
            symbols=SYMBOLS,
            level=_STRATEGY_LEVEL,
            paper=self._cfg.paper,
        )
        print(
            f"\n[OPTIONS ENGINE] Mode={tag}  Level={_STRATEGY_LEVEL}  "
            f"Universe={len(SYMBOLS)} symbols\n"
            f"                 No entries after {_NO_ENTRY_HOUR}:00 ET  "
            f"|  DTE close ≤ 2 days\n"
            f"                 Logs → logs/options_engine.jsonl\n"
            f"                 Ctrl-C to stop cleanly.\n"
        )

        await self._breaker.initialize_baseline()
        await self._preseed_buffers()

        # Initial chain load before strategy loop starts (blocking is OK here —
        # we're not in the event loop yet for bar processing).
        log.info("options_chain_initial_load")
        await self._chain._refresh_all()
        snap = self._chain.snapshot()
        log.info("options_chain_loaded", contracts_per_symbol=snap)

        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._feed.run(), name="feed")
            tg.create_task(self._chain.run(), name="chain_refresh")
            tg.create_task(self._strategy_loop(), name="strategy")
            tg.create_task(self._drawdown_watch(), name="drawdown")
            tg.create_task(self._dte_monitor_loop(), name="dte_monitor")

    # ── Strategy loop ─────────────────────────────────────────────────────────

    async def _strategy_loop(self) -> None:
        while self._running:
            if self._breaker.halted:
                log.critical("options_engine_halted")
                self.stop()
                break

            msg = await self._msg_q.get()

            if msg["type"] == "orderbook":
                self._signals.update_orderbook(msg)
                continue

            if msg["type"] != "bar":
                continue

            # Belt-and-suspenders RTH guard
            now_et = datetime.now(_ET).time()
            if not (_RTH_OPEN <= now_et < _RTH_CLOSE):
                continue

            # No new entries after NO_ENTRY_HOUR (default 3 PM)
            entry_cutoff = dtime(_NO_ENTRY_HOUR, 0)
            allow_entry = now_et < entry_cutoff

            sym = msg.get("symbol", "")
            order_list = self._signals.evaluate(msg)
            if order_list is None:
                continue

            # Determine if this is an entry or exit based on the first order
            first_action = order_list[0].get("action", "")
            is_entry = any(
                first_action.startswith(p)
                for p in ("buy_call", "buy_put", "sell_csp", "bull_", "bear_")
            )
            if is_entry and not allow_entry:
                log.info(
                    "options_entry_skipped_cutoff",
                    symbol=sym,
                    hour=now_et.hour,
                    cutoff=_NO_ENTRY_HOUR,
                )
                # Rollback signal state since we didn't execute
                if sym in self._signals._positions:
                    del self._signals._positions[sym]
                continue

            for order_kwargs in order_list:
                action = order_kwargs.pop("action", "")
                await self._bucket.acquire()
                try:
                    result = await self._orders.submit_limit(
                        **order_kwargs,
                        tif=TimeInForce.DAY,  # options require DAY TIF
                    )
                except Exception as exc:
                    # Transient network / Alpaca failure. Treat like a blocked
                    # leg: log, roll back pre-committed entry state, skip remaining
                    # legs so we don't execute half a spread. Engine survives.
                    log.warning(
                        "options_order_network_error",
                        action=action,
                        symbol=order_kwargs.get("symbol"),
                        exc_type=type(exc).__name__,
                        exc_msg=str(exc)[:160],
                    )
                    result = None

                if result:
                    log.info("options_order_result", action=action, **result)
                else:
                    log.warning(
                        "options_order_blocked",
                        action=action,
                        symbol=order_kwargs.get("symbol"),
                    )
                    # On blocked entry, remove the position we pre-committed
                    if is_entry and sym in self._signals._positions:
                        del self._signals._positions[sym]
                    break  # don't submit remaining legs if first is blocked

    # ── DTE monitor ───────────────────────────────────────────────────────────

    async def _dte_monitor_loop(self) -> None:
        """
        Every 30 minutes: scan open options positions for near-expiry contracts.
        Submits close orders for any position with DTE ≤ 2.
        """
        while self._running:
            await asyncio.sleep(_DTE_MONITOR_INTERVAL_S)
            to_close = self._signals.check_dte_closes()
            for underlying, order_list, _ in to_close:
                log.warning(
                    "dte_close_executing",
                    underlying=underlying,
                    n_orders=len(order_list),
                )
                for order_kwargs in order_list:
                    action = order_kwargs.pop("action", "dte_close")
                    await self._bucket.acquire()
                    try:
                        result = await self._orders.submit_limit(
                            **order_kwargs,
                            tif=TimeInForce.DAY,
                        )
                    except Exception as exc:
                        # Network failure on a DTE close is serious (position
                        # still expiring) — log at error level so it's visible,
                        # but don't crash; retry on next 30-min tick.
                        log.error(
                            "dte_close_network_error",
                            action=action,
                            symbol=order_kwargs.get("symbol"),
                            exc_type=type(exc).__name__,
                            exc_msg=str(exc)[:160],
                            note="will retry next DTE monitor tick",
                        )
                        result = None

                    if result:
                        log.info("dte_close_result", action=action, **result)
                    else:
                        log.error(
                            "dte_close_blocked",
                            action=action,
                            symbol=order_kwargs.get("symbol"),
                            note="manual intervention may be required",
                        )

            # Periodic position snapshot for monitoring
            open_pos = self._signals.open_positions_summary()
            if open_pos:
                log.info("options_positions_snapshot", positions=open_pos)

    # ── Drawdown watchdog ─────────────────────────────────────────────────────

    async def _drawdown_watch(self) -> None:
        while self._running:
            await asyncio.sleep(60)
            safe = await self._breaker.check_drawdown()
            if not safe:
                self.stop()

    # ── Pre-seed rolling buffers ──────────────────────────────────────────────

    async def _preseed_buffers(self) -> None:
        """
        Fetch last WINDOW daily closes per symbol so z-scores are warm on bar 1.
        Same approach as equities_engine.py.  Silently skips on error.
        """
        log.info("options_buffer_preseed_start", symbols=SYMBOLS, window=WINDOW)
        end = datetime.now(_ET).replace(hour=0, minute=0, second=0, microsecond=0)
        start = end - timedelta(days=WINDOW * 2)  # extra headroom for non-trading days
        try:
            req = StockBarsRequest(
                symbol_or_symbols=SYMBOLS,
                timeframe=TimeFrame.Day,
                start=start,
                end=end,
            )
            bars = await asyncio.to_thread(self._data_client.get_stock_bars, req)
            seeded = 0
            for sym in SYMBOLS:
                sym_bars = (bars.data if hasattr(bars, "data") else bars).get(sym, [])
                if not sym_bars:
                    log.warning("options_preseed_no_bars", symbol=sym)
                    continue
                closes = [float(b.close) for b in sym_bars[-WINDOW:]]
                self._signals.seed_price_buffer(sym, closes)
                seeded += 1
            log.info(
                "options_buffer_preseed_complete", seeded=seeded, total=len(SYMBOLS)
            )
        except Exception as exc:
            log.warning(
                "options_preseed_error",
                exc_type=type(exc).__name__,
                exc_msg=str(exc)[:120],
                note="engine will warm up from live bars (~60 bars per symbol)",
            )

    def stop(self) -> None:
        log.info("options_engine_shutdown")
        self._running = False
        try:
            self._feed.stop()
        except Exception:
            pass
        try:
            self._chain.stop()
        except Exception:
            pass


# ── Entry point ────────────────────────────────────────────────────────────────
async def main() -> None:
    engine = OptionsEngine()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, engine.stop)
    await engine.run()


if __name__ == "__main__":
    asyncio.run(main())
