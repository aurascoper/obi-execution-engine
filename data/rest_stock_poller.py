"""
data/rest_stock_poller.py — REST-based daily bar + NBBO poller.

Used by options_engine.py so it does not open a second WebSocket connection
(Alpaca free tier: 1 stock stream connection per API key; equities_engine.py
holds that connection).

Behaviour:
  • Every QUOTE_INTERVAL_S seconds: fetch latest quote per symbol, emit
    {"type": "orderbook", ...} messages to keep OBI fresh.
  • Every BAR_CHECK_INTERVAL_S seconds: fetch latest daily bar per symbol.
    When a bar with today's date appears (market just closed), emit a
    {"type": "bar", ...} message once per symbol per day.

Both fetches use asyncio.to_thread so the event loop is never blocked.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
import zoneinfo

import structlog
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestBarRequest, StockLatestQuoteRequest
from alpaca.data.enums import DataFeed

log = structlog.get_logger(__name__)

_ET = zoneinfo.ZoneInfo("America/New_York")

QUOTE_INTERVAL_S = 30  # NBBO poll cadence
BAR_CHECK_INTERVAL_S = 60  # daily bar poll cadence


class RestStockPoller:
    """
    Emits bar and orderbook messages to msg_queue using REST polling only.
    Drop-in replacement for LiveStockFeed in options_engine.py.
    """

    def __init__(
        self,
        data_client: StockHistoricalDataClient,
        symbols: list[str],
        msg_queue: asyncio.Queue,
    ) -> None:
        self._dc = data_client
        self._symbols = symbols
        self._queue = msg_queue
        self._running = True
        # Track the last bar date emitted per symbol to avoid duplicates
        self._last_bar_date: dict[str, date] = {}

    async def run(self) -> None:
        log.info(
            "rest_stock_poller_start",
            symbols=self._symbols,
            quote_interval_s=QUOTE_INTERVAL_S,
            bar_interval_s=BAR_CHECK_INTERVAL_S,
        )

        # Run quote and bar polling concurrently as separate inner loops
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._quote_loop(), name="rest_quote_loop")
            tg.create_task(self._bar_loop(), name="rest_bar_loop")

    def stop(self) -> None:
        log.info("rest_stock_poller_stop")
        self._running = False

    # ── Quote loop ────────────────────────────────────────────────────────────

    async def _quote_loop(self) -> None:
        while self._running:
            await asyncio.sleep(QUOTE_INTERVAL_S)
            try:
                await asyncio.to_thread(self._fetch_quotes)
            except Exception as exc:
                log.warning(
                    "rest_quote_error",
                    exc_type=type(exc).__name__,
                    exc_msg=str(exc)[:120],
                )

    def _fetch_quotes(self) -> None:
        req = StockLatestQuoteRequest(
            symbol_or_symbols=self._symbols,
            feed=DataFeed.IEX,
        )
        data = self._dc.get_stock_latest_quote(req)
        quotes = data if isinstance(data, dict) else getattr(data, "data", {})
        for sym, q in quotes.items():
            bid_px = float(getattr(q, "bid_price", 0) or 0)
            ask_px = float(getattr(q, "ask_price", 0) or 0)
            bid_sz = float(getattr(q, "bid_size", 0) or 0)
            ask_sz = float(getattr(q, "ask_size", 0) or 0)
            if bid_px <= 0 or ask_px <= 0:
                continue
            msg = {
                "type": "orderbook",
                "symbol": sym,
                "bids": [[bid_px, bid_sz]],
                "asks": [[ask_px, ask_sz]],
            }
            try:
                self._queue.put_nowait(msg)
            except asyncio.QueueFull:
                pass  # drop silently; OBI is best-effort between bars

    # ── Bar loop ──────────────────────────────────────────────────────────────

    async def _bar_loop(self) -> None:
        # Emit immediately on startup — if z-score is already at threshold, fire now.
        try:
            await asyncio.to_thread(self._fetch_bars, force=True)
        except Exception as exc:
            log.warning(
                "rest_bar_error_startup",
                exc_type=type(exc).__name__,
                exc_msg=str(exc)[:120],
            )

        while self._running:
            await asyncio.sleep(BAR_CHECK_INTERVAL_S)
            try:
                await asyncio.to_thread(self._fetch_bars)
            except Exception as exc:
                log.warning(
                    "rest_bar_error",
                    exc_type=type(exc).__name__,
                    exc_msg=str(exc)[:120],
                )

    def _fetch_bars(self, force: bool = False) -> None:
        req = StockLatestBarRequest(
            symbol_or_symbols=self._symbols,
            feed=DataFeed.IEX,
        )
        data = self._dc.get_stock_latest_bar(req)
        bars = data if isinstance(data, dict) else getattr(data, "data", {})
        today = datetime.now(_ET).date()

        for sym, bar in bars.items():
            ts = getattr(bar, "timestamp", None)
            if ts is None:
                continue
            # Normalize to date in ET
            if hasattr(ts, "date"):
                bar_date = ts.astimezone(_ET).date() if ts.tzinfo else ts.date()
            else:
                bar_date = today

            # Only emit once per day per symbol (bypass on startup force-emit)
            if not force and self._last_bar_date.get(sym) == bar_date:
                continue

            self._last_bar_date[sym] = bar_date
            msg = {
                "type": "bar",
                "symbol": sym,
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": float(bar.volume),
                "timestamp": str(ts),
            }
            try:
                self._queue.put_nowait(msg)
                log.debug(
                    "rest_bar_emitted",
                    symbol=sym,
                    date=str(bar_date),
                    close=float(bar.close),
                )
            except asyncio.QueueFull:
                log.warning("rest_bar_queue_full", symbol=sym)
