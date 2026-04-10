"""
data/stock_feed.py — Alpaca Stock Data Stream for the parallel equities engine.

Uses StockDataStream which connects to:
  wss://stream.data.alpaca.markets/v2/stocks

Subscribes to:
  bars   → emits {"type": "bar",       ...}  consumed by EquitiesSignalEngine.evaluate()
  quotes → emits {"type": "orderbook", ...}  consumed by EquitiesSignalEngine.update_orderbook()
           (synthesized single-level orderbook from NBBO bid/ask to keep OBI fresh
            between bar events — identical contract to crypto feed.py quote handler)

Market Hours Guard: both callbacks silently discard messages received outside
09:30–16:00 Eastern Time. Pre-market and post-market bars are dropped at the
feed layer; the strategy loop applies a second guard as belt-and-suspenders.

Note: StockDataStream does not expose L2 orderbooks (subscribe_orderbooks is
crypto-only). Top-of-book NBBO quotes provide sufficient OBI signal for daily
bar strategies — the quote rate (~1000/min) keeps obi fresh between 1-min bars.
"""

import time
import asyncio
import zoneinfo
from datetime import datetime, time as dtime

import structlog
from alpaca.data.live import StockDataStream
from alpaca.data.models import Bar, Quote
from alpaca.data.enums import DataFeed

from config.settings import Settings

log = structlog.get_logger(__name__)

_ET        = zoneinfo.ZoneInfo("America/New_York")
_RTH_OPEN  = dtime(9, 30)
_RTH_CLOSE = dtime(16, 0)


class LiveStockFeed:
    """
    Real-time equities bar and NBBO quote feed over Alpaca's StockDataStream.

    Emits two message types to msg_queue (identical schema to crypto LiveFeed):
      {"type": "bar",       "symbol": ..., "close": ..., ...}
      {"type": "orderbook", "symbol": ..., "bids": [[px, sz]], "asks": [[px, sz]]}

    The "orderbook" type is synthesized from NBBO quotes (single level). Downstream
    EquitiesSignalEngine.update_orderbook() handles 1-level lists identically to
    the crypto L2 path — the OBI scalar caches in _SymbolState.obi and gates
    entry conditions at the next bar event.
    """

    def __init__(
        self,
        cfg:       Settings,
        symbols:   list[str],
        msg_queue: asyncio.Queue,
    ):
        self._symbols     = set(symbols)
        self._queue       = msg_queue

        self._msg_counts: dict[str, int] = {"bar": 0, "quote": 0}
        self._count_window_start: float  = time.monotonic()

        self._stream = StockDataStream(
            api_key    = cfg.api_key,
            secret_key = cfg.api_secret,
            feed       = DataFeed.IEX,
        )
        self._stream.subscribe_bars(self._on_bar, *symbols)
        self._stream.subscribe_quotes(self._on_quote, *symbols)

        log.info(
            "stock_feed_subscribed",
            n_symbols=len(symbols),
            channels=["bars", "quotes"],
            endpoint="wss://stream.data.alpaca.markets/v2/iex",
        )

    # ── Market hours guard ─────────────────────────────────────────────────────

    @staticmethod
    def _is_rth() -> bool:
        """True only during Regular Trading Hours: 09:30–16:00 ET."""
        now_et = datetime.now(_ET).time()
        return _RTH_OPEN <= now_et < _RTH_CLOSE

    # ── Debug counter ──────────────────────────────────────────────────────────

    def _tick_count(self, msg_type: str) -> None:
        """Increment per-type counter; flush to log every 60 seconds."""
        self._msg_counts[msg_type] += 1
        now     = time.monotonic()
        elapsed = now - self._count_window_start
        if elapsed >= 60.0:
            log.info(
                "stock_feed_msg_rate",
                window_s=round(elapsed, 1),
                bars_per_min=self._msg_counts["bar"],
                quotes_per_min=self._msg_counts["quote"],
            )
            self._msg_counts         = {"bar": 0, "quote": 0}
            self._count_window_start = now

    # ── SDK callbacks ──────────────────────────────────────────────────────────

    async def _on_bar(self, bar: Bar) -> None:
        if bar.symbol not in self._symbols:
            return
        if not self._is_rth():
            return
        self._tick_count("bar")
        await self._queue.put({
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

    async def _on_quote(self, q: Quote) -> None:
        """
        Synthesize a single-level orderbook from NBBO quote so OBI stays current
        between bar events. Guards against missing/zero fields from bad ticks.
        """
        if q.symbol not in self._symbols:
            return
        if not self._is_rth():
            return
        try:
            bp  = float(q.bid_price)
            bs  = float(q.bid_size)
            ap  = float(q.ask_price)
            as_ = float(q.ask_size)
        except (TypeError, AttributeError):
            return
        if bp <= 0 or ap <= 0:
            return
        self._tick_count("quote")
        await self._queue.put({
            "type":      "orderbook",
            "symbol":    q.symbol,
            "bids":      [[bp, bs]],
            "asks":      [[ap, as_]],
            "timestamp": str(q.timestamp),
            "recv_ns":   time.perf_counter_ns(),
        })

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def run(self) -> None:
        log.info("stock_feed_starting")
        await self._stream._run_forever()

    def stop(self) -> None:
        log.info("stock_feed_stopping")
        self._stream.stop()
