#!/usr/bin/env python3
"""
live_engine.py — Main async execution engine entry point.

Phase 1 (Shadow Burn-In):
  export EXECUTION_MODE=SHADOW
  export ALPACA_TRADING_MODE=paper
  python live_engine.py

Phase 2 (Paper):
  export EXECUTION_MODE=PAPER
  export ALPACA_TRADING_MODE=paper

Phase 3 (Live — $5 cap active):
  export EXECUTION_MODE=LIVE
  export ALPACA_TRADING_MODE=live
"""

import asyncio
import logging
import signal
from pathlib import Path

import structlog
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide

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
        file=open("logs/engine.jsonl", "a", buffering=1)   # line-buffered
    ),
)
log = structlog.get_logger("engine")

# ── Universe ──────────────────────────────────────────────────────────────────
# Trimmed to 10 highest-liquidity assets: 10 bars + 10 orderbooks = 20 total
# subscriptions — well under Alpaca's crypto free-tier per-connection limit.
# Expand once on a paid tier or Hyperliquid (Phase 4).
SYMBOLS = [
    "BTC/USD",   # highest liquidity, tight spreads
    "ETH/USD",   # second deepest book
    "SOL/USD",   # high-beta L1, strong mean-reversion
    "AVAX/USD",  # correlated L1 alt
    "DOGE/USD",  # high retail volume, mean-reverts well
    "LINK/USD",  # DeFi blue-chip, OBI signal reliable
    "ADA/USD",   # liquid L1
    "XRP/USD",   # high volume, tight spread
    "LTC/USD",   # BTC proxy, deep book
    "BCH/USD",   # BTC fork, predictable correlation
]
# Excluded (subscription limit): DOT, XTZ, AAVE, UNI, CRV, SUSHI, LDO, GRT,
#   YFI, ARB, POL, FIL, RENDER, SHIB, BONK, PEPE, BAT, PAXG
# Expand to full universe on Alpaca paid tier or Phase 4 Hyperliquid pivot.


# ── Token bucket (rate limiter) ───────────────────────────────────────────────
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
            now = loop.time()
            refill = (now - self._last_fill) / self._interval
            self._tokens    = min(self._max, self._tokens + refill)
            self._last_fill = now
        self._tokens -= 1.0


# ── Engine ────────────────────────────────────────────────────────────────────
class Engine:
    def __init__(self, msg_queue: asyncio.Queue | None = None):
        self._cfg     = load_settings()
        self._client  = TradingClient(
            self._cfg.api_key,
            self._cfg.api_secret,
            paper=self._cfg.paper,
        )
        self._breaker = CircuitBreaker(self._client)
        self._orders  = OrderManager(self._client, self._breaker, self._cfg,
                                     strategy_tag="taker")
        self._signals = SignalEngine(symbols=SYMBOLS, strategy_tag="taker")
        self._bucket  = _TokenBucket(MAX_ORDERS_PER_MINUTE)
        self._running = True

        if msg_queue is not None:
            # Shared feed injected by launch.py — don't create our own.
            self._msg_q = msg_queue
            self._feed  = None
        else:
            self._msg_q = asyncio.Queue(maxsize=2000)
            self._feed  = LiveFeed(self._cfg, SYMBOLS, self._msg_q)

    async def run(self) -> None:
        mode = self._cfg.execution_mode.value
        tag  = "*** LIVE ***" if self._cfg.execution_mode == ExecutionMode.LIVE else mode
        log.info("engine_start", mode=tag, symbols=SYMBOLS,
                 paper=self._cfg.paper, max_notional=5.00)
        print(f"\n[ENGINE] Mode={tag}  Universe={SYMBOLS}  MaxNotional=$5.00\n"
              f"         Logs → logs/engine.jsonl\n"
              f"         Ctrl-C to stop cleanly.\n")

        await self._breaker.initialize_baseline()

        async with asyncio.TaskGroup() as tg:
            if self._feed is not None:
                tg.create_task(self._feed.run(),   name="feed")
            tg.create_task(self._strategy_loop(),  name="strategy")
            tg.create_task(self._drawdown_watch(), name="drawdown")

    # ── Strategy loop — routes bar vs. orderbook messages ────────────────────
    async def _strategy_loop(self) -> None:
        while self._running:
            if self._breaker.halted:
                log.critical("engine_halted")
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
                    # Order was blocked (circuit breaker, notional cap, etc.).
                    # Roll back the correct side so the engine can retry next bar.
                    if signal["side"] == OrderSide.BUY:
                        self._signals.rollback_entry(signal["symbol"])
                    else:
                        self._signals.rollback_exit(signal["symbol"])

    # ── Drawdown watchdog — independent of strategy ───────────────────────────
    async def _drawdown_watch(self) -> None:
        while self._running:
            await asyncio.sleep(60)
            safe = await self._breaker.check_drawdown()
            if not safe:
                self._running = False
                if self._feed is not None:
                    self._feed.stop()

    def stop(self) -> None:
        log.info("engine_shutdown")
        self._running = False
        if self._feed is not None:
            self._feed.stop()


# ── Entry point ───────────────────────────────────────────────────────────────
async def main() -> None:
    engine = Engine()
    loop   = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, engine.stop)
    await engine.run()


if __name__ == "__main__":
    asyncio.run(main())
