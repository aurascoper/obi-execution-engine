"""
execution/order_manager.py — Order construction, submission, and slippage logging.

Execution mode routing:
  SHADOW → log mock fill, return synthetic dict, never call the API
  PAPER  → submit to Alpaca paper endpoint
  LIVE   → submit to Alpaca live endpoint
"""

import asyncio
import time
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from alpaca.trading.client import TradingClient
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
        client:  TradingClient,
        breaker: CircuitBreaker,
        cfg:     Settings,
    ):
        self._client  = client
        self._breaker = breaker
        self._mode    = cfg.execution_mode

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
        if not self._breaker.validate_order(symbol, qty, notional):
            return None

        if self._mode == ExecutionMode.SHADOW:
            return self._shadow_fill(symbol, side, qty, limit_px, notional)

        # PAPER or LIVE — hit the actual API
        return await self._submit_to_api(symbol, side, qty, limit_px, notional, tif)

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
        symbol:   str,
        side:     OrderSide,
        qty:      float,
        limit_px: float,
        notional: float,
    ) -> dict:
        """
        Simulate a fill without touching the brokerage API.
        The log line is deliberately high-visibility (WARNING level) so it
        cannot be missed in production log streams.
        """
        mock_id = f"shadow-{symbol.replace('/', '')}-{int(time.time())}"
        log.warning(
            "[SHADOW EXECUTION] Mock fill",
            symbol=symbol,
            side=side.value,
            qty=qty,
            limit_px=limit_px,
            notional=notional,
            mock_order_id=mock_id,
        )
        return {
            "id":          mock_id,
            "status":      "shadow_filled",
            "symbol":      symbol,
            "side":        side.value,
            "qty":         qty,
            "limit_px":    limit_px,
            "notional":    notional,
            "latency_ms":  0.0,
            "mode":        "SHADOW",
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
        symbol:   str,
        side:     OrderSide,
        qty:      float,
        limit_px: float,
        notional: float,
        tif:      TimeInForce,
    ) -> dict:
        req = LimitOrderRequest(
            symbol        = symbol,
            qty           = qty,
            side          = side,
            limit_price   = limit_px,
            time_in_force = tif,
        )
        t0    = time.perf_counter_ns()
        order = await asyncio.to_thread(self._client.submit_order, req)
        lat   = (time.perf_counter_ns() - t0) / 1e6    # ms

        log.info(
            "order_submitted",
            id=str(order.id),
            symbol=symbol,
            side=side.value,
            qty=qty,
            limit_px=limit_px,
            notional=notional,
            latency_ms=round(lat, 3),
            mode=self._mode.value,
        )
        return {
            "id":         str(order.id),
            "status":     str(order.status),
            "latency_ms": round(lat, 3),
            "mode":       self._mode.value,
        }
