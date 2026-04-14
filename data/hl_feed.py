"""
data/hl_feed.py — Hyperliquid L2 order book WebSocket feed (Phase 4 scaffold).

Isolated from the Alpaca path: this module produces dicts in the same shape
that SignalEngine.update_orderbook() expects ({type, symbol, bids[[px,sz]…],
asks[[px,sz]…], timestamp, recv_ns}), so the signal layer does not need to
know the venue.

Hyperliquid WS protocol (wss://api.hyperliquid.xyz/ws):

  Subscribe:
    {"method": "subscribe", "subscription": {"type": "l2Book", "coin": "BTC"}}

  Snapshot payload:
    {
      "channel": "l2Book",
      "data": {
        "coin":   "BTC",
        "time":   1_700_000_000_000,      # ms epoch
        "levels": [
          [ {"px": "...", "sz": "...", "n": N}, ... ],   # bids (index 0)
          [ {"px": "...", "sz": "...", "n": N}, ... ],   # asks (index 1)
        ]
      }
    }

This file intentionally does NOT share any code with data/feed.py.  The Alpaca
crypto engine must remain unaffected by Hyperliquid connectivity issues.

Phase 4 state: scaffold. Not wired into any engine yet — kept standalone until
Phase 3 (taker/maker side-by-side) is validated.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Iterable

import structlog
import websockets
from websockets.exceptions import ConnectionClosed

log = structlog.get_logger(__name__)

HL_WS_URL = "wss://api.hyperliquid.xyz/ws"


class HyperliquidFeed:
    """
    Async WebSocket listener for Hyperliquid L2 order book snapshots.

    Usage:
        q = asyncio.Queue()
        feed = HyperliquidFeed(coins=["BTC", "ETH"], msg_queues=q)
        await feed.run()          # runs until cancelled; auto-reconnects
    """

    def __init__(
        self,
        coins:      Iterable[str],
        msg_queues: asyncio.Queue | list[asyncio.Queue],
        url:        str = HL_WS_URL,
    ):
        self._coins = [c.upper() for c in coins]
        self._queues: list[asyncio.Queue] = (
            msg_queues if isinstance(msg_queues, list) else [msg_queues]
        )
        self._url = url
        self._stop = asyncio.Event()

        # Debug counters, flushed once per 60 s — mirrors data/feed.py.
        self._msg_counts: dict[str, int] = {"l2Book": 0}
        self._count_window_start: float  = time.monotonic()

    # ── Subscription ──────────────────────────────────────────────────────────

    async def _subscribe_l2book(self, ws) -> None:
        """Send one l2Book subscription frame per coin."""
        for coin in self._coins:
            frame = {
                "method":       "subscribe",
                "subscription": {"type": "l2Book", "coin": coin},
            }
            await ws.send(json.dumps(frame))
            log.info("hl_subscribed", channel="l2Book", coin=coin)

    # ── Fan-out ───────────────────────────────────────────────────────────────

    async def _put(self, msg: dict) -> None:
        for q in self._queues:
            await q.put(msg)

    # ── Debug counter ────────────────────────────────────────────────────────

    def _tick_count(self, channel: str) -> None:
        self._msg_counts[channel] = self._msg_counts.get(channel, 0) + 1
        now = time.monotonic()
        elapsed = now - self._count_window_start
        if elapsed >= 60.0:
            log.info(
                "hl_feed_msg_rate",
                window_s=round(elapsed, 1),
                **{f"{k}_per_min": v for k, v in self._msg_counts.items()},
            )
            self._msg_counts = {k: 0 for k in self._msg_counts}
            self._count_window_start = now

    # ── Payload normalization ─────────────────────────────────────────────────

    def _normalize_l2book(self, data: dict) -> dict | None:
        """
        Translate Hyperliquid's l2Book payload to the shape consumed by
        SignalEngine.update_orderbook().

        Hyperliquid levels:
          data["levels"] = [bids, asks]
          each level = {"px": str, "sz": str, "n": int}

        Returns None if the payload is malformed, so upstream can skip.
        """
        coin = data.get("coin")
        levels = data.get("levels")
        if not coin or not isinstance(levels, list) or len(levels) != 2:
            return None

        try:
            bids = [[float(lvl["px"]), float(lvl["sz"])] for lvl in levels[0]]
            asks = [[float(lvl["px"]), float(lvl["sz"])] for lvl in levels[1]]
        except (KeyError, TypeError, ValueError):
            return None

        # HL returns asks in ascending-px order and bids in descending-px order,
        # matching what SignalEngine expects (bids[0] = best bid, asks[0] = best ask).
        return {
            "type":      "orderbook",
            "symbol":    coin,
            "bids":      bids,
            "asks":      asks,
            "timestamp": str(data.get("time", "")),
            "recv_ns":   time.perf_counter_ns(),
        }

    # ── Message router ───────────────────────────────────────────────────────

    async def _handle_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("hl_invalid_json", raw=raw[:200])
            return

        channel = msg.get("channel")
        # HL sends subscription acks under channel="subscriptionResponse" and
        # a post-ping frame under channel="pong". Ignore both silently.
        if channel in (None, "subscriptionResponse", "pong"):
            return

        if channel == "l2Book":
            data = msg.get("data")
            if not isinstance(data, dict):
                return
            norm = self._normalize_l2book(data)
            if norm is None:
                return
            self._tick_count("l2Book")
            await self._put(norm)
            return

        # Unknown channel — log once so it's visible but don't spam.
        log.debug("hl_unknown_channel", channel=channel)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """
        Connect, subscribe, and pump messages until cancelled.  Reconnects with
        capped exponential backoff on any disconnect.
        """
        log.info("hl_feed_starting", coins=self._coins, url=self._url)
        delay = 5
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    self._url,
                    ping_interval=30,
                    ping_timeout=10,
                    max_queue=1024,
                ) as ws:
                    await self._subscribe_l2book(ws)
                    delay = 5  # reset backoff on successful connect

                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        await self._handle_message(raw)

            except asyncio.CancelledError:
                raise
            except ConnectionClosed as exc:
                log.warning("hl_feed_ws_closed",
                            code=exc.code, reason=str(exc.reason),
                            sleep_s=delay)
            except Exception as exc:
                log.warning("hl_feed_reconnect",
                            error=str(exc), sleep_s=delay)

            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)  # 5→10→20→…→300

    def stop(self) -> None:
        log.info("hl_feed_stopping")
        self._stop.set()
