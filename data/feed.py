"""
data/feed.py — Alpaca Crypto Data Stream via alpaca-py SDK.

Uses CryptoDataStream which resolves to the correct versioned endpoint:
  wss://stream.data.alpaca.markets/v1beta3/crypto/us

Subscribes to:
  bars       → emits {"type": "bar", ...}       consumed by SignalEngine.evaluate()
  orderbooks → emits {"type": "orderbook", ...} consumed by SignalEngine.update_orderbook()
  quotes     → synthesizes single-level {"type": "orderbook", ...} to keep OBI fresh
               between L2 snapshots (top-of-book bid/ask sizes from "T":"q" messages)

Previous raw-WebSocket implementation used /v2/crypto (404 — endpoint deprecated).
This version delegates URL routing entirely to the SDK.

Debug counters: logs bar/orderbook/quote message counts once per minute so we can
confirm L2 orderbook updates are arriving (OBI stuck → counter stays 0).
"""

import time
import asyncio
import structlog

from alpaca.data.live import CryptoDataStream
from alpaca.data.models import Bar, Orderbook, Quote

from config.settings import Settings

log = structlog.get_logger(__name__)


class LiveFeed:
    def __init__(
        self,
        cfg:        Settings,
        symbols:    list[str],
        msg_queues: asyncio.Queue | list[asyncio.Queue],
    ):
        self._symbols = symbols
        # Normalise to list so callbacks can fan-out to N consumers.
        self._queues: list[asyncio.Queue] = (
            msg_queues if isinstance(msg_queues, list) else [msg_queues]
        )

        # ── Per-type message counters (reset every 60 s) ──────────────────────
        self._msg_counts: dict[str, int] = {"bar": 0, "orderbook": 0, "quote": 0}
        self._count_window_start: float  = time.monotonic()

        # Use paper credentials for the data stream — crypto market data is
        # identical for paper/live, and this preserves the live key's single
        # free-tier WebSocket connection slot for order submission only.
        self._stream  = CryptoDataStream(
            api_key    = cfg.data_key or cfg.api_key,
            secret_key = cfg.data_secret or cfg.api_secret,
        )

        # Patch _start_ws to add backoff on "connection limit exceeded".
        # The SDK's _run_forever retries immediately on any error, hammering
        # Alpaca's server and accumulating zombie connections. A 60s sleep
        # gives the server time to clean them up before the next attempt.
        _orig_start_ws = self._stream._start_ws

        async def _backoff_start_ws() -> None:
            try:
                await _orig_start_ws()
            except Exception as e:
                if "connection limit" in str(e).lower():
                    log.warning(
                        "feed_connection_limit_backoff",
                        sleep_s=60,
                        note="waiting for Alpaca server to clear stale connections",
                    )
                    await asyncio.sleep(60)
                raise

        self._stream._start_ws = _backoff_start_ws

        # Register handlers — SDK calls these as async callbacks.
        # Quotes subscription removed: orderbook snapshots already deliver L2
        # bid/ask depth for OBI.  28 bars + 28 orderbooks = 56 subscriptions,
        # within Alpaca's crypto free-tier per-connection limit.
        self._stream.subscribe_bars(self._on_bar, *symbols)
        self._stream.subscribe_orderbooks(self._on_orderbook, *symbols)

        log.info("feed_subscribed", symbols=symbols,
                 channels=["bars", "orderbooks"],
                 endpoint="wss://stream.data.alpaca.markets/v1beta3/crypto/us")

    # ── Internal: debug counter ────────────────────────────────────────────────

    def _tick_count(self, msg_type: str) -> None:
        """Increment per-type counter; flush to log every 60 seconds."""
        self._msg_counts[msg_type] += 1
        now = time.monotonic()
        elapsed = now - self._count_window_start
        if elapsed >= 60.0:
            log.info(
                "feed_msg_rate",
                window_s=round(elapsed, 1),
                bars_per_min=self._msg_counts["bar"],
                orderbooks_per_min=self._msg_counts["orderbook"],
                quotes_per_min=self._msg_counts["quote"],
            )
            self._msg_counts        = {"bar": 0, "orderbook": 0, "quote": 0}
            self._count_window_start = now

    # ── Internal: fan-out put ─────────────────────────────────────────────────

    async def _put(self, msg: dict) -> None:
        """Deliver one message to every registered consumer queue."""
        for q in self._queues:
            await q.put(msg)

    # ── SDK callbacks ─────────────────────────────────────────────────────────

    async def _on_bar(self, bar: Bar) -> None:
        self._tick_count("bar")
        await self._put({
            "type":      "bar",
            "symbol":    bar.symbol,
            "open":      float(bar.open),
            "high":      float(bar.high),
            "low":       float(bar.low),
            "close":     float(bar.close),
            "volume":    float(bar.volume),
            "timestamp": str(bar.timestamp),
            "recv_ns":   time.perf_counter_ns(),
        })

    async def _on_orderbook(self, ob: Orderbook) -> None:
        """
        Orderbook snapshot from the SDK (T:o messages).
        ob.bids and ob.asks are lists of OrderbookQuote(price, size).
        Converted to [[price, size], ...] to match SignalEngine.update_orderbook() contract.
        """
        self._tick_count("orderbook")
        await self._put({
            "type":      "orderbook",
            "symbol":    ob.symbol,
            "bids":      [[float(q.price), float(q.size)] for q in ob.bids],
            "asks":      [[float(q.price), float(q.size)] for q in ob.asks],
            "timestamp": str(ob.timestamp),
            "recv_ns":   time.perf_counter_ns(),
        })

    async def _on_quote(self, q: Quote) -> None:
        """
        Top-of-book quote (T:q messages).  Arrives far more frequently than L2
        snapshots; synthesize a single-level orderbook so OBI stays fresh between
        full orderbook updates.  Downstream update_orderbook() handles 1-level lists.
        """
        self._tick_count("quote")
        # Guard: skip if price/size fields are missing or zero
        try:
            bp = float(q.bid_price)
            bs = float(q.bid_size)
            ap = float(q.ask_price)
            as_ = float(q.ask_size)
        except (TypeError, AttributeError):
            return
        if bp <= 0 or ap <= 0:
            return
        await self._put({
            "type":      "orderbook",
            "symbol":    q.symbol,
            "bids":      [[bp, bs]],
            "asks":      [[ap, as_]],
            "timestamp": str(q.timestamp),
            "recv_ns":   time.perf_counter_ns(),
        })

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """
        Awaitable coroutine — run inside asyncio.TaskGroup in live_engine.py.
        _run_forever() is the SDK's internal async loop; it handles reconnection.
        """
        log.info("feed_starting")
        await self._stream._run_forever()

    def stop(self) -> None:
        log.info("feed_stopping")
        self._stream.stop()
