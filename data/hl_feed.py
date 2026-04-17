"""
data/hl_feed.py — Hyperliquid WebSocket feed (L2 order book + user fills).

Isolated from the Alpaca path: l2Book payloads are normalized to the shape
SignalEngine.update_orderbook() expects, and userFills payloads are normalized
to a {type: "hl_fill", ...} schema the engine can route to SignalEngine.on_fill.
The signal layer never sees HL-specific types.

Hyperliquid WS protocol (wss://api.hyperliquid.xyz/ws):

  L2 subscribe:
    {"method": "subscribe", "subscription": {"type": "l2Book", "coin": "BTC"}}

  L2 payload:
    {"channel": "l2Book", "data": {"coin":"BTC","time":…,"levels":[[bids],[asks]]}}

  userFills subscribe (address-scoped):
    {"method": "subscribe",
     "subscription": {"type": "userFills", "user": "0x…"}}

  userFills payload:
    {"channel": "userFills",
     "data": {"user":"0x…","isSnapshot":bool,"fills":[
        {"coin":"BTC","px":"…","sz":"…","side":"A"|"B",
         "time":…,"oid":…,"cloid":"0x…"|null,"crossed":bool, …}, …]}}

  Side encoding: "B" = bid-side fill (we bought),
                 "A" = ask-side fill (we sold).

This file intentionally does NOT share any code with data/feed.py — the
Alpaca crypto engine must remain unaffected by Hyperliquid connectivity issues.
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
        coins: Iterable[str],
        msg_queues: asyncio.Queue | list[asyncio.Queue],
        url: str = HL_WS_URL,
        wallet: str | None = None,
        perp_dexs: list[str] | None = None,
    ):
        self._coins = list(coins)
        self._perp_dexs = perp_dexs or []
        self._queues: list[asyncio.Queue] = (
            msg_queues if isinstance(msg_queues, list) else [msg_queues]
        )
        self._url = url
        # userFills subscription is address-scoped. Optional: when absent, the
        # feed runs L2-only — preserves the pre-Spike-A call sites.
        self._wallet = wallet.lower() if wallet else None
        self._stop = asyncio.Event()

        # Debug counters, flushed once per 60 s — mirrors data/feed.py.
        self._msg_counts: dict[str, int] = {"l2Book": 0, "userFills": 0}
        self._count_window_start: float = time.monotonic()

    # ── Subscription ──────────────────────────────────────────────────────────

    async def _subscribe_l2book(self, ws) -> None:
        """Send one l2Book subscription frame per coin."""
        for coin in self._coins:
            frame = {
                "method": "subscribe",
                "subscription": {"type": "l2Book", "coin": coin},
            }
            await ws.send(json.dumps(frame))
            log.info("hl_subscribed", channel="l2Book", coin=coin)

    async def _subscribe_userfills(self, ws) -> None:
        """
        One userFills subscription (address-scoped, not per-coin). Only
        fired when a wallet was provided to __init__. On reconnect, HL
        replays recent fills with isSnapshot=true; we skip those to avoid
        double-counting against state already on disk.
        """
        if not self._wallet:
            return
        frame = {
            "method": "subscribe",
            "subscription": {"type": "userFills", "user": self._wallet},
        }
        await ws.send(json.dumps(frame))
        log.info("hl_subscribed", channel="userFills", user=self._wallet)

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
        except KeyError, TypeError, ValueError:
            return None

        # HL returns asks in ascending-px order and bids in descending-px order,
        # matching what SignalEngine expects (bids[0] = best bid, asks[0] = best ask).
        return {
            "type": "orderbook",
            "symbol": str(coin),
            "bids": bids,
            "asks": asks,
            "timestamp": str(data.get("time", "")),
            "recv_ns": time.perf_counter_ns(),
        }

    def _normalize_userfill(self, fill: dict) -> dict | None:
        """
        Translate one HL userFills entry into the engine-facing schema:
          {type: "hl_fill", symbol: coin, side: "buy"|"sell",
           px: float, sz: float, oid: int|None, cloid: str|None,
           crossed: bool, ts: str, recv_ns: int}

        side decoding: HL uses "B" for bid-side (we're the buyer) and "A"
        for ask-side (we're the seller). `crossed=True` marks taker fills;
        Alo (maker) fills come back with crossed=False.
        """
        try:
            coin = str(fill["coin"])
            px = float(fill["px"])
            sz = float(fill["sz"])
            raw_side = str(fill["side"]).upper()
        except KeyError, TypeError, ValueError:
            return None

        if raw_side == "B":
            side = "buy"
        elif raw_side == "A":
            side = "sell"
        else:
            return None

        return {
            "type": "hl_fill",
            "symbol": coin,
            "side": side,
            "px": px,
            "sz": sz,
            "oid": fill.get("oid"),
            "cloid": fill.get("cloid"),
            "crossed": bool(fill.get("crossed", True)),
            "ts": str(fill.get("time", "")),
            "recv_ns": time.perf_counter_ns(),
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

        if channel == "userFills":
            data = msg.get("data")
            if not isinstance(data, dict):
                return
            # isSnapshot=true is replayed history on each (re)connect. We
            # skip it — the engine's startup reconcile already reads open
            # positions; replaying past fills would double-fire on_fill.
            if data.get("isSnapshot"):
                log.info(
                    "hl_userfills_snapshot_skipped",
                    count=len(data.get("fills", []) or []),
                )
                return
            fills = data.get("fills") or []
            for f in fills:
                norm = self._normalize_userfill(f)
                if norm is None:
                    continue
                self._tick_count("userFills")
                log.info(
                    "hl_fill_received",
                    symbol=norm["symbol"],
                    side=norm["side"],
                    px=norm["px"],
                    sz=norm["sz"],
                    oid=norm["oid"],
                    cloid=norm["cloid"],
                    crossed=norm["crossed"],
                )
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
                    await self._subscribe_userfills(ws)
                    delay = 5  # reset backoff on successful connect

                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        await self._handle_message(raw)

            except asyncio.CancelledError:
                raise
            except ConnectionClosed as exc:
                log.warning(
                    "hl_feed_ws_closed",
                    code=exc.code,
                    reason=str(exc.reason),
                    sleep_s=delay,
                )
            except Exception as exc:
                log.warning("hl_feed_reconnect", error=str(exc), sleep_s=delay)

            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)  # 5→10→20→…→300

    def stop(self) -> None:
        log.info("hl_feed_stopping")
        self._stop.set()
