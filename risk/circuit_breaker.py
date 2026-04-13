"""
risk/circuit_breaker.py — Independent circuit breaker watchdog.
No imports from strategy/ — intentional isolation.
"""
import structlog
from alpaca.trading.client import TradingClient
from config.risk_params import (
    MAX_DAILY_DRAWDOWN_PCT,
    MAX_DAILY_LOSS_DOLLARS,
    MAX_ORDER_NOTIONAL,
    MAX_CONTRACTS_PER_LEG,
    MAX_SHARES_PER_ORDER,
    SYMBOL_CAPS,
)

log = structlog.get_logger(__name__)


class CircuitBreaker:
    def __init__(self, client: TradingClient):
        self._client       = client
        self._halted       = False
        self._equity_open: float | None = None

    @property
    def halted(self) -> bool:
        return self._halted

    def _halt(self, reason: str) -> None:
        self._halted = True
        log.critical("CIRCUIT_BREAKER_TRIPPED", reason=reason)

    async def initialize_baseline(self) -> None:
        acct = self._client.get_account()
        self._equity_open = float(acct.equity)
        log.info(
            "baseline_equity",
            equity=self._equity_open,
            account=acct.account_number,
        )

    async def check_drawdown(self) -> bool:
        if self._halted:
            return False
        acct         = self._client.get_account()
        equity_now   = float(acct.equity)
        daily_pnl    = equity_now - (self._equity_open or equity_now)
        drawdown_pct = daily_pnl / self._equity_open if self._equity_open else 0.0

        if daily_pnl < -MAX_DAILY_LOSS_DOLLARS:
            self._halt(f"daily loss ${abs(daily_pnl):.2f} > ${MAX_DAILY_LOSS_DOLLARS}")
            return False
        if drawdown_pct < -MAX_DAILY_DRAWDOWN_PCT:
            self._halt(f"drawdown {drawdown_pct:.2%} > {MAX_DAILY_DRAWDOWN_PCT:.2%}")
            return False
        return True

    def validate_order(
        self,
        symbol:     str,
        qty:        float,
        notional:   float,
        asset_class: str = "crypto",    # "equity" | "option" | "crypto"
        side:        str = "buy",       # "buy" | "sell"
    ) -> bool:
        if self._halted:
            log.warning("order_blocked_halted", symbol=symbol)
            return False
        # Notional caps guard entries only — exits reduce exposure, not increase it.
        if side == "buy":
            if notional > MAX_ORDER_NOTIONAL:
                log.warning("order_blocked_notional",
                            symbol=symbol, notional=notional, cap=MAX_ORDER_NOTIONAL)
                return False
            if symbol in SYMBOL_CAPS and notional > SYMBOL_CAPS[symbol]:
                log.warning("order_blocked_symbol_cap",
                            symbol=symbol, notional=notional, cap=SYMBOL_CAPS[symbol])
                return False
        if asset_class == "option" and qty > MAX_CONTRACTS_PER_LEG:
            log.warning("order_blocked_contracts", symbol=symbol, qty=qty)
            return False
        if asset_class == "equity" and qty > MAX_SHARES_PER_ORDER:
            log.warning("order_blocked_shares", symbol=symbol, qty=qty)
            return False
        return True
