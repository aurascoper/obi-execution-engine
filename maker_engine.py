#!/usr/bin/env python3
"""
maker_engine.py — Passive Maker Engine (Phase 3)

Runs alongside live_engine.py (taker) on the same Alpaca paper account.
Both engines share the account balance but track inventory independently
via client_order_id tagging ("maker_" prefix vs "taker_" prefix).

Strategy differences from the taker engine:
  • Limit price = best_bid (BUY) / best_ask (SELL) — no spread-crossing slippage.
    Posts at the inside of the book; earns the spread instead of paying it.
  • Adverse Selection Guard: _order_tracker_loop() cancels any open "maker_"
    orders every 30 seconds. If the market moved away and the order wasn't
    filled, capital is freed immediately and evaluate() reprices on the next bar.

Usage:
  export EXECUTION_MODE=PAPER
  export ALPACA_TRADING_MODE=paper
  python maker_engine.py
"""

import asyncio
import logging
import signal
from pathlib import Path

import structlog
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus

from config.settings import ExecutionMode, load as load_settings
from config.risk_params import MAX_ORDERS_PER_MINUTE
from risk.circuit_breaker import CircuitBreaker
from data.feed import LiveFeed
from execution.order_manager import OrderManager
from strategy.signals import SignalEngine

# ── Logging setup ─────────────────────────────────────────────────────────────
Path("logs").mkdir(exist_ok=True)

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
    logger_factory=structlog.WriteLoggerFactory(
        file=open("logs/maker_engine.jsonl", "a", buffering=1)
    ),
)
log = structlog.get_logger("maker_engine")

# ── Universe — must match live_engine.py (shared feed, same subscription set) ─
from live_engine import SYMBOLS

# Adverse selection timeout: cancel open maker orders older than this.
_CANCEL_INTERVAL_S = 30


# ── Token bucket (rate limiter) ───────────────────────────────────────────────
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


# ── Maker Engine ──────────────────────────────────────────────────────────────
class MakerEngine:
    def __init__(self, msg_queue: asyncio.Queue | None = None):
        self._cfg = load_settings()
        self._client = TradingClient(
            self._cfg.api_key,
            self._cfg.api_secret,
            paper=self._cfg.paper,
        )
        self._breaker = CircuitBreaker(self._client)
        self._orders = OrderManager(
            self._client,
            self._breaker,
            self._cfg,
            strategy_tag="maker",
        )
        self._signals = SignalEngine(
            symbols=SYMBOLS,
            strategy_tag="maker",
        )
        self._bucket = _TokenBucket(MAX_ORDERS_PER_MINUTE)
        self._running = True

        if msg_queue is not None:
            self._msg_q = msg_queue
            self._feed = None
        else:
            self._msg_q = asyncio.Queue(maxsize=2000)
            self._feed = LiveFeed(self._cfg, SYMBOLS, self._msg_q)

        # Wire fill events from TradingStream → SignalEngine position state
        self._orders.register_fill_handler(self._signals.on_fill)

    async def run(self) -> None:
        mode = self._cfg.execution_mode.value
        tag = "*** LIVE ***" if self._cfg.execution_mode == ExecutionMode.LIVE else mode
        log.info(
            "maker_engine_start",
            mode=tag,
            symbols=SYMBOLS,
            paper=self._cfg.paper,
            strategy_tag="maker",
        )
        print(
            f"\n[MAKER ENGINE] Mode={tag}  Universe={len(SYMBOLS)} symbols\n"
            f"               strategy_tag=maker  cancel_interval={_CANCEL_INTERVAL_S}s\n"
            f"               Logs → logs/maker_engine.jsonl\n"
            f"               Ctrl-C to stop cleanly.\n"
        )

        await self._breaker.initialize_baseline()

        async with asyncio.TaskGroup() as tg:
            if self._feed is not None:
                tg.create_task(self._feed.run(), name="feed")
            tg.create_task(self._strategy_loop(), name="strategy")
            tg.create_task(self._drawdown_watch(), name="drawdown")
            tg.create_task(self._order_tracker_loop(), name="order_tracker")
            tg.create_task(self._orders.start_trade_updates(), name="trade_updates")

    # ── Strategy loop ─────────────────────────────────────────────────────────
    async def _strategy_loop(self) -> None:
        while self._running:
            if self._breaker.halted:
                log.critical("maker_engine_halted")
                self._running = False
                if self._feed is not None:
                    self._feed.stop()
                break

            msg = await self._msg_q.get()

            if msg["type"] == "orderbook":
                self._signals.update_orderbook(msg)
                continue

            if msg["type"] == "bar":
                signal = self._signals.evaluate(msg)
                if signal is None:
                    continue

                await self._bucket.acquire()
                result = await self._orders.submit_limit(**signal)
                if result:
                    log.info("order_result", **result)
                else:
                    if signal["side"] == OrderSide.BUY:
                        self._signals.rollback_entry(signal["symbol"])
                    else:
                        self._signals.rollback_exit(signal["symbol"])

    # ── Adverse Selection Guard ───────────────────────────────────────────────
    async def _order_tracker_loop(self) -> None:
        """
        Every 30 seconds: fetch all open orders, cancel any with a "maker_"
        client_order_id.

        Rationale: a maker limit that hasn't filled in 30 seconds means the
        market moved away. Canceling it frees the capital; since the Alpaca
        order never filled, on_fill() was never called, so is_open() is still
        False — evaluate() will cleanly recompute and post a fresh limit on
        the next bar at the current best_bid.
        """
        while self._running:
            await asyncio.sleep(_CANCEL_INTERVAL_S)
            try:
                open_orders = await asyncio.to_thread(
                    self._client.get_orders,
                    GetOrdersRequest(status=QueryOrderStatus.OPEN),
                )
                cancelled = 0
                for order in open_orders:
                    cid = getattr(order, "client_order_id", "") or ""
                    if not cid.startswith("maker_"):
                        continue
                    try:
                        await asyncio.to_thread(
                            self._client.cancel_order_by_id, str(order.id)
                        )
                        log.info(
                            "maker_order_cancelled",
                            order_id=str(order.id),
                            client_order_id=cid,
                            symbol=str(order.symbol),
                            reason="adverse_selection_guard",
                            timeout_s=_CANCEL_INTERVAL_S,
                        )
                        cancelled += 1
                    except Exception:
                        log.exception("cancel_failed", order_id=str(order.id))

                if cancelled:
                    log.info("order_tracker_sweep", cancelled=cancelled)

            except Exception:
                log.exception("order_tracker_error")

    # ── Drawdown watchdog ─────────────────────────────────────────────────────
    async def _drawdown_watch(self) -> None:
        while self._running:
            await asyncio.sleep(60)
            safe = await self._breaker.check_drawdown()
            if not safe:
                self._running = False
                if self._feed is not None:
                    self._feed.stop()

    def stop(self) -> None:
        log.info("maker_engine_shutdown")
        self._running = False
        if self._feed is not None:
            self._feed.stop()


# ── Entry point ───────────────────────────────────────────────────────────────
async def main() -> None:
    engine = MakerEngine()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, engine.stop)
    await engine.run()


if __name__ == "__main__":
    asyncio.run(main())
