"""
execution/order_manager.py — Order construction, submission, and slippage logging.

Execution mode routing:
  SHADOW → log mock fill, return synthetic dict, never call the API
  PAPER  → submit to Alpaca paper endpoint
  LIVE   → submit to Alpaca live endpoint

Phase 3 — Client Order ID tagging:
  Every order carries a client_order_id of the form:
    "{strategy_tag}_{symbol_no_slash}_{epoch_s}"
    e.g. "taker_ETHUSD_1744566000"  (≤ 40 chars, well under Alpaca's 48-char limit)

  TradingStream fill listener:
    start_trade_updates() subscribes to the Alpaca trade-update WebSocket.
    On each fill event the client_order_id is parsed and routed to any
    registered handler (typically SignalEngine.on_fill).

  Usage:
    om = OrderManager(client, breaker, cfg, strategy_tag="taker")
    om.register_fill_handler(signals.on_fill)
    asyncio.create_task(om.start_trade_updates())
"""

import asyncio
import time
from typing import Callable
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.stream import TradingStream
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

from config.settings import ExecutionMode, Settings
from config.risk_params import (
    BACKOFF_BASE_SECONDS,
    BACKOFF_MAX_SECONDS,
    SLIPPAGE_ALERT_PCT,
)
from risk.circuit_breaker import CircuitBreaker

log = structlog.get_logger(__name__)


class OrderManager:
    def __init__(
        self,
        client:       TradingClient,
        breaker:      CircuitBreaker,
        cfg:          Settings,
        strategy_tag: str = "taker",
        asset_class:  str = "equity",   # "equity" | "option" | "crypto"
    ):
        self._client       = client
        self._breaker      = breaker
        self._mode         = cfg.execution_mode
        self._cfg          = cfg
        self.strategy_tag  = strategy_tag
        self._asset_class  = asset_class
        self._fill_handler: Callable | None = None

    # ── Client Order ID ───────────────────────────────────────────────────────

    def _make_client_id(self, symbol: str) -> str:
        """
        Format: "{tag}_{sym_no_slash}_{epoch_s}"
        Max 40 chars — safe under Alpaca's 48-char client_order_id limit.

        Example: "taker_ETHUSD_1744566000"
        """
        sym_clean = symbol.replace("/", "").replace("-", "")
        epoch     = int(time.time())
        return f"{self.strategy_tag}_{sym_clean}_{epoch}"

    # ── Fill handler registration ─────────────────────────────────────────────

    def register_fill_handler(self, handler: Callable) -> None:
        """
        Register a callback invoked on each fill from TradingStream.

        Expected signature:
          handler(client_order_id: str, symbol: str, qty: float, side: str) -> None
        """
        self._fill_handler = handler

    # ── TradingStream trade-update listener ───────────────────────────────────

    async def start_trade_updates(self) -> None:
        """
        Subscribe to Alpaca's TradingStream and route fill events to the
        registered fill handler.  Runs until cancelled.

        Only processes events whose client_order_id prefix matches this
        engine's strategy_tag, so taker/maker streams don't cross-fire.
        """
        stream = TradingStream(
            api_key    = self._cfg.api_key,
            secret_key = self._cfg.api_secret,
            paper      = (self._mode != ExecutionMode.LIVE),
        )

        async def _on_trade_update(data) -> None:
            try:
                # event may be a string or TradeEvent enum
                event_raw = data.event
                event = event_raw.value if hasattr(event_raw, "value") else str(event_raw)
                if event not in ("fill", "partial_fill"):
                    return

                order  = data.order
                cid    = getattr(order, "client_order_id", "") or ""
                symbol = getattr(order, "symbol", "")
                qty    = float(getattr(order, "filled_qty", 0) or 0)

                # Bug 1 fix: order.side is an OrderSide enum, not a plain string.
                # Use .value ("buy"/"sell") when available; fall back to str().
                side_raw = getattr(order, "side", "")
                side     = side_raw.value if hasattr(side_raw, "value") else str(side_raw)
                side     = side.lower()

                # Only handle fills that belong to this tag
                if not cid.startswith(f"{self.strategy_tag}_"):
                    return

                # Bug 3: detect paper-sim ghost fills — maker limit filled at a
                # price that crossed our limit (impossible in real markets).
                fill_px  = float(getattr(order, "filled_avg_price", 0) or 0)
                limit_px = float(getattr(order, "limit_price",      0) or 0)
                if limit_px > 0 and fill_px > 0:
                    if side == "buy"  and fill_px > limit_px * 1.001:
                        log.warning(
                            "paper_sim_fill_suspected",
                            symbol=symbol, side=side,
                            fill_px=fill_px, limit_px=limit_px,
                            note="fill above buy limit — would not fill in live market",
                        )
                    elif side == "sell" and fill_px < limit_px * 0.999:
                        log.warning(
                            "paper_sim_fill_suspected",
                            symbol=symbol, side=side,
                            fill_px=fill_px, limit_px=limit_px,
                            note="fill below sell limit — would not fill in live market",
                        )

                log.info(
                    "trade_update",
                    event=event,
                    client_order_id=cid,
                    symbol=symbol,
                    qty=qty,
                    fill_px=fill_px,
                    side=side,
                    tag=self.strategy_tag,
                )

                if self._fill_handler is not None:
                    self._fill_handler(cid, symbol, qty, side)

            except Exception as _exc:
                log.error(
                    "trade_update_parse_error",
                    raw=str(data),
                    exc_type=type(_exc).__name__,
                    exc_msg=str(_exc),
                )

        stream.subscribe_trade_updates(_on_trade_update)
        log.info("trade_updates_starting", tag=self.strategy_tag)
        await stream._run_forever()

    # ── Public ────────────────────────────────────────────────────────────────
    async def submit_limit(
        self,
        symbol:   str,
        side:     OrderSide,
        qty:      float,
        limit_px: float,
        notional: float,
        tif:      TimeInForce = TimeInForce.GTC,
    ) -> dict | None:
        """
        Route order through the active execution mode.
        Returns a result dict on success, None if blocked by circuit breaker.
        """
        if not self._breaker.validate_order(symbol, qty, notional,
                                             asset_class=self._asset_class,
                                             side=side.value.lower()):
            return None

        cid = self._make_client_id(symbol)

        if self._mode == ExecutionMode.SHADOW:
            return self._shadow_fill(symbol, side, qty, limit_px, notional, cid)

        # PAPER or LIVE — hit the actual API
        try:
            return await self._submit_to_api(symbol, side, qty, limit_px, notional, tif, cid)
        except APIError as exc:
            # Non-retriable broker rejections — log and treat as blocked (return None)
            # so the TaskGroup is not killed by a single order rejection.
            log.warning(
                "order_rejected_by_broker",
                symbol=symbol,
                side=side.value,
                code=getattr(exc, "code", None),
                msg=str(exc)[:200],
                tag=self.strategy_tag,
            )
            return None

    def log_slippage(self, symbol: str, expected_px: float, fill_px: float) -> None:
        slip  = abs(fill_px - expected_px) / expected_px
        level = "warning" if slip > SLIPPAGE_ALERT_PCT else "info"
        getattr(log, level)(
            "slippage",
            symbol=symbol,
            expected=expected_px,
            fill=fill_px,
            pct=f"{slip:.4%}",
        )

    # ── Shadow mode ───────────────────────────────────────────────────────────
    def _shadow_fill(
        self,
        symbol:          str,
        side:            OrderSide,
        qty:             float,
        limit_px:        float,
        notional:        float,
        client_order_id: str = "",
    ) -> dict:
        """
        Simulate a fill without touching the brokerage API.
        The log line is deliberately high-visibility (WARNING level) so it
        cannot be missed in production log streams.
        """
        log.warning(
            "[SHADOW EXECUTION] Mock fill",
            symbol=symbol,
            side=side.value,
            qty=qty,
            limit_px=limit_px,
            notional=notional,
            client_order_id=client_order_id,
            tag=self.strategy_tag,
        )
        return {
            "id":               client_order_id,
            "status":           "shadow_filled",
            "symbol":           symbol,
            "side":             side.value,
            "qty":              qty,
            "limit_px":         limit_px,
            "notional":         notional,
            "latency_ms":       0.0,
            "mode":             "SHADOW",
            "client_order_id":  client_order_id,
        }

    # ── Live / Paper mode ─────────────────────────────────────────────────────
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(
            multiplier=BACKOFF_BASE_SECONDS,
            max=BACKOFF_MAX_SECONDS,
        ),
        reraise=True,
    )
    async def _submit_to_api(
        self,
        symbol:          str,
        side:            OrderSide,
        qty:             float,
        limit_px:        float,
        notional:        float,
        tif:             TimeInForce,
        client_order_id: str = "",
    ) -> dict:
        req = LimitOrderRequest(
            symbol           = symbol,
            qty              = qty,
            side             = side,
            limit_price      = limit_px,
            time_in_force    = tif,
            client_order_id  = client_order_id or None,
        )
        t0    = time.perf_counter_ns()
        order = await asyncio.to_thread(self._client.submit_order, req)
        lat   = (time.perf_counter_ns() - t0) / 1e6    # ms

        log.info(
            "order_submitted",
            id=str(order.id),
            client_order_id=client_order_id,
            symbol=symbol,
            side=side.value,
            qty=qty,
            limit_px=limit_px,
            notional=notional,
            latency_ms=round(lat, 3),
            tag=self.strategy_tag,
            mode=self._mode.value,
        )
        return {
            "id":               str(order.id),
            "status":           str(order.status),
            "latency_ms":       round(lat, 3),
            "mode":             self._mode.value,
            "client_order_id":  client_order_id,
        }
