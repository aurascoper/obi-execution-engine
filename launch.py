#!/usr/bin/env python3
"""
launch.py — Side-by-side Taker + Maker deployment.

Runs both crypto engines in a single asyncio event loop, sharing one
WebSocket connection to the Alpaca crypto data feed.

Architecture:
  LiveFeed → [taker_q, maker_q]   (fan-out; one WS connection)
  Engine(taker_q)                  strategy_tag="taker"  logs/engine.jsonl
  MakerEngine(maker_q)             strategy_tag="maker"  logs/maker_engine.jsonl

Usage:
  source env.sh
  export EXECUTION_MODE=PAPER
  caffeinate -i python launch.py
"""

import asyncio
import logging
import signal
from pathlib import Path

import structlog
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus

from config.settings import load as load_settings
from data.feed import LiveFeed
from live_engine import Engine, SYMBOLS

# ── Logging — write to a launch-level log in addition to per-engine logs ──────
Path("logs").mkdir(exist_ok=True)

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
    logger_factory=structlog.WriteLoggerFactory(
        file=open("logs/launch.jsonl", "a", buffering=1)
    ),
)
log = structlog.get_logger("launch")


async def _reconcile(taker: "Engine", maker: "MakerEngine", client) -> None:
    """Seed both engines with any positions Alpaca already holds."""
    import asyncio
    positions = await asyncio.to_thread(client.get_all_positions)
    if not positions:
        log.info("reconcile_complete", open_positions=0)
        return

    orders = await asyncio.to_thread(
        client.get_orders,
        GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=100),
    )
    taker._signals.reconcile_positions(positions, orders)
    maker._signals.reconcile_positions(positions, orders)
    log.info("reconcile_complete", open_positions=len(positions))


async def main() -> None:
    cfg = load_settings()

    # One queue per engine — fan-out delivers each message to both.
    taker_q = asyncio.Queue(maxsize=2000)
    maker_q = asyncio.Queue(maxsize=2000)

    # Single shared crypto WebSocket feed.
    feed = LiveFeed(cfg, SYMBOLS, [taker_q, maker_q])

    # Import here so per-engine structlog sinks are configured first.
    from maker_engine import MakerEngine

    taker = Engine(msg_queue=taker_q)
    maker = MakerEngine(msg_queue=maker_q)

    def _stop(*_):
        log.info("launch_shutdown_requested")
        taker.stop()
        maker.stop()
        feed.stop()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _stop)

    log.info("launch_start", symbols=SYMBOLS, engines=["taker", "maker"])
    print(
        f"\n[LAUNCH] Taker + Maker running on {len(SYMBOLS)} symbols\n"
        f"         One shared WebSocket → fan-out to both strategy loops\n"
        f"         Taker logs → logs/engine.jsonl\n"
        f"         Maker logs → logs/maker_engine.jsonl\n"
        f"         Ctrl-C to stop both cleanly.\n"
    )

    # Initialize both breakers before starting tasks.
    await taker._breaker.initialize_baseline()
    await maker._breaker.initialize_baseline()

    # Reconcile open positions from Alpaca so engines don't re-enter existing
    # holdings after a restart (Bug 2 + Bug 4).
    await _reconcile(taker, maker, taker._client)

    async with asyncio.TaskGroup() as tg:
        # Shared feed — single WebSocket connection.
        tg.create_task(feed.run(),                              name="feed")

        # Taker tasks.
        tg.create_task(taker._strategy_loop(),                  name="taker_strategy")
        tg.create_task(taker._drawdown_watch(),                 name="taker_drawdown")

        # Maker tasks.
        tg.create_task(maker._strategy_loop(),                  name="maker_strategy")
        tg.create_task(maker._drawdown_watch(),                 name="maker_drawdown")
        tg.create_task(maker._order_tracker_loop(),             name="maker_tracker")
        tg.create_task(maker._orders.start_trade_updates(),     name="maker_fills")


if __name__ == "__main__":
    asyncio.run(main())
