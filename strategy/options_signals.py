"""
strategy/options_signals.py — OBI-gated options signal engine.

Maps the same dual-gate signal (z-score + OBI) from the equity/crypto engines
to options orders.  Signal interpretation:

  z < Z_ENTRY_LONG  AND  OBI > OBI_THETA_LONG   → buy call  (Level 2)
                                                 → sell CSP  (Level 1, skipped
                                                              on small accounts)
                                                 → bull call spread (Level 3)

  z > Z_ENTRY_SHORT AND  OBI < OBI_THETA_SHORT  → buy put   (Level 2+)
                                                 → bear put spread (Level 3)

Exit conditions (per open position):
  Directional exit: underlying z reverts past Z_EXIT threshold
  DTE guard:        position DTE ≤ DTE_CLOSE_THRESHOLD (avoid expiry pin risk)

Output (what the engine loop submits):
  Each evaluate() call returns a list[dict] or None.
  Single-leg orders: 1-element list.
  Spread orders: 2-element list [long_leg, short_leg].

  Each element matches OrderManager.submit_limit() kwargs:
    symbol, side, qty (int), limit_px (per-share premium), notional (total debit)
  Plus a routing key:
    action: "buy_call" | "buy_put" | "sell_csp" | "close_call" | "close_put"
            | "sell_call_spread_leg" | "sell_put_spread_leg"
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

import numpy as np
import structlog
from alpaca.trading.enums import OrderSide

from config.risk_params import (
    MAX_OPTIONS_BUDGET,
    MAX_OPTIONS_POSITIONS,
    MAX_CONTRACTS_PER_TRADE,
)
from data.options_chain import CachedContract, OptionsChainCache
from strategy.signals import _RollingBuffer

log = structlog.get_logger(__name__)

# ── Strategy parameters ────────────────────────────────────────────────────────
WINDOW            = 60      # rolling bars (daily closes pre-seeded same as equities)
Z_ENTRY_LONG      = -1.25   # z < this → bullish (oversold underlying)
Z_EXIT_LONG       = -0.50   # z > this → close call (mean-reverted)
Z_ENTRY_SHORT     = +1.25   # z > this → bearish (overbought underlying)
Z_EXIT_SHORT      = +0.50   # z < this → close put

OBI_THETA_LONG    = 0.00    # any net buy pressure confirms long entry
OBI_THETA_SHORT   = 0.00    # any net sell pressure confirms short entry

DTE_CLOSE_THRESH  = 2       # close options with ≤ 2 DTE (avoid expiry risk)
NO_ENTRY_AFTER    = (15, 0) # (hour, minute) ET — no new entries after 3 PM

# Delta target and premium budget per option leg
TARGET_DELTA      = 0.45    # near-ATM
SPREAD_WIDTH      = 5.0     # OTM leg offset for spreads ($5 default)


@dataclass
class _OptionPosition:
    """Tracks one open options position (one underlying = one direction at a time)."""
    contract_symbol: str
    underlying:      str
    qty:             int
    entry_px:        float   # premium per share paid
    expiry:          date
    contract_type:   Literal["call", "put"]
    action:          str     # e.g. "buy_call", "bull_call_spread"
    # For spreads: short leg OSI symbol (empty string if single-leg)
    short_leg_symbol: str = field(default="")
    short_entry_px:   float = field(default=float("nan"))


class OptionsSignalEngine:
    """
    Stateful per-symbol options signal engine.

    Each evaluate() call processes one underlying bar and may return a list of
    order dicts (at most 2 for a spread) or None.

    Usage:
        engine = OptionsSignalEngine(chain, symbols, strategy_level=2)
        # Per bar:
        orders = engine.evaluate(bar)     # bar = {"symbol":..., "close":..., ...}
        # Per NBBO quote:
        engine.update_orderbook(ob)
    """

    def __init__(
        self,
        chain:           OptionsChainCache,
        symbols:         list[str],
        strategy_level:  int   = 2,
        window:          int   = WINDOW,
        strategy_tag:    str   = "options",
    ) -> None:
        if strategy_level not in (1, 2, 3):
            raise ValueError(f"strategy_level must be 1, 2, or 3; got {strategy_level}")

        self._chain          = chain
        self._level          = strategy_level
        self._tag            = strategy_tag
        self._symbols        = symbols

        # Per-underlying rolling buffers and OBI cache
        self._buffers: dict[str, _RollingBuffer] = {
            s: _RollingBuffer(window) for s in symbols
        }
        self._obi:  dict[str, float] = {s: 0.0  for s in symbols}
        self._best_bid: dict[str, float] = {s: float("nan") for s in symbols}
        self._best_ask: dict[str, float] = {s: float("nan") for s in symbols}

        # Open options positions: underlying → _OptionPosition
        self._positions: dict[str, _OptionPosition] = {}

    # ── Bar-driven signal evaluation ──────────────────────────────────────────

    def evaluate(self, bar: dict) -> list[dict] | None:
        """
        Process one underlying bar.

        Returns list[dict] of order kwargs (1 element single-leg, 2 for spread),
        or None if no action required.
        """
        sym   = bar.get("symbol", "")
        if sym not in self._buffers:
            return None

        close = float(bar["close"])
        self._buffers[sym].push(close)

        z = self._buffers[sym].zscore(close)
        if z is None:
            return None

        obi = self._obi[sym]

        log.debug(
            "options_signal_tick",
            symbol=sym,
            z=round(z, 4),
            obi=round(obi, 4),
            in_position=sym in self._positions,
            level=self._level,
        )

        # 1. Exit path — check before entry
        if sym in self._positions:
            pos = self._positions[sym]
            if self._should_close(sym, z, pos):
                orders = self._build_close_orders(sym, pos, close)
                if orders:
                    del self._positions[sym]
                    return orders
            return None

        # 2. No new entries if we're at the position cap
        if len(self._positions) >= MAX_OPTIONS_POSITIONS:
            return None

        # 3. Entry path
        bullish  = z < Z_ENTRY_LONG  and obi > OBI_THETA_LONG
        bearish  = z > Z_ENTRY_SHORT and obi < OBI_THETA_SHORT

        if bullish:
            return self._enter_bullish(sym, close)
        if bearish and self._level >= 2:
            # Puts / bear spreads require Level 2+
            return self._enter_bearish(sym, close)

        return None

    # ── Orderbook update (from NBBO quotes) ───────────────────────────────────

    def update_orderbook(self, ob: dict) -> None:
        """Cache latest OBI from a synthesized single-level NBBO orderbook."""
        sym = ob.get("symbol", "")
        if sym not in self._obi:
            return
        bids = ob.get("bids", [])
        asks = ob.get("asks", [])
        if not bids or not asks:
            return
        vb = float(bids[0][1]) if bids else 0.0
        va = float(asks[0][1]) if asks else 0.0
        self._obi[sym]      = (vb - va) / (vb + va + 1e-8)
        self._best_bid[sym] = float(bids[0][0])
        self._best_ask[sym] = float(asks[0][0])

    # ── DTE-driven close (called by engine's dte_monitor_loop) ────────────────

    def check_dte_closes(self) -> list[tuple[str, list[dict], float]]:
        """
        Scan open positions for near-expiry contracts.

        Returns list of (underlying, order_list, close) tuples for positions
        that must be closed.  The engine loop calls this on a schedule and
        submits the returned orders.
        """
        to_close = []
        for sym, pos in list(self._positions.items()):
            dte = (pos.expiry - date.today()).days
            if dte <= DTE_CLOSE_THRESH:
                # Use last known underlying close as reference price for limit
                # The engine passes 0.0 here; build_close_orders handles it.
                contract = self._chain.get_by_osi(pos.contract_symbol)
                close_px = contract.mid if contract else 0.0
                orders   = self._build_close_orders(sym, pos, close_px)
                if orders:
                    to_close.append((sym, orders, close_px))
                    del self._positions[sym]
                log.warning(
                    "dte_close_triggered",
                    symbol=sym,
                    contract=pos.contract_symbol,
                    dte=dte,
                    threshold=DTE_CLOSE_THRESH,
                )
        return to_close

    def seed_price_buffer(self, symbol: str, closes: list[float]) -> None:
        """Pre-seed the rolling buffer from historical data (same as equities engine)."""
        for c in closes[-WINDOW:]:
            self._buffers[symbol].push(c)

    def open_positions_summary(self) -> list[dict]:
        """For logging — returns a snapshot of open options positions."""
        return [
            {
                "underlying":      sym,
                "contract":        pos.contract_symbol,
                "qty":             pos.qty,
                "entry_px":        pos.entry_px,
                "dte":             (pos.expiry - date.today()).days,
                "action":          pos.action,
                "short_leg":       pos.short_leg_symbol or None,
            }
            for sym, pos in self._positions.items()
        ]

    # ── Private: Entry builders ────────────────────────────────────────────────

    def _enter_bullish(self, sym: str, close: float) -> list[dict] | None:
        if self._level == 1:
            return self._enter_csp(sym, close)
        if self._level == 2:
            return self._enter_long_call(sym, close)
        # Level 3 — bull call spread
        return self._enter_bull_call_spread(sym, close)

    def _enter_bearish(self, sym: str, close: float) -> list[dict] | None:
        if self._level == 2:
            return self._enter_long_put(sym, close)
        # Level 3 — bear put spread
        return self._enter_bear_put_spread(sym, close)

    def _enter_long_call(self, sym: str, close: float) -> list[dict] | None:
        max_per_share = MAX_OPTIONS_BUDGET / 100.0   # budget cap → max $/share
        contract = self._chain.best_contract(
            sym, "call",
            target_delta=TARGET_DELTA,
            min_premium=0.05,
            max_premium=max_per_share,
        )
        if contract is None:
            log.warning("options_no_call_found", symbol=sym)
            return None

        qty, limit_px, notional = self._size_single_leg(contract)
        if qty < 1:
            return None

        self._positions[sym] = _OptionPosition(
            contract_symbol=contract.symbol,
            underlying=sym,
            qty=qty,
            entry_px=limit_px,
            expiry=contract.expiry,
            contract_type="call",
            action="buy_call",
        )
        log.info(
            "options_entry",
            symbol=sym,
            action="buy_call",
            contract=contract.symbol,
            strike=contract.strike,
            expiry=str(contract.expiry),
            dte=contract.dte,
            delta=round(contract.delta, 3),
            limit_px=limit_px,
            notional=notional,
            iv=round(contract.iv, 3) if not math.isnan(contract.iv) else None,
        )
        return [{"symbol": contract.symbol, "side": OrderSide.BUY,
                 "qty": qty, "limit_px": limit_px, "notional": notional,
                 "action": "buy_call"}]

    def _enter_long_put(self, sym: str, close: float) -> list[dict] | None:
        max_per_share = MAX_OPTIONS_BUDGET / 100.0
        contract = self._chain.best_contract(
            sym, "put",
            target_delta=TARGET_DELTA,
            min_premium=0.05,
            max_premium=max_per_share,
        )
        if contract is None:
            log.warning("options_no_put_found", symbol=sym)
            return None

        qty, limit_px, notional = self._size_single_leg(contract)
        if qty < 1:
            return None

        self._positions[sym] = _OptionPosition(
            contract_symbol=contract.symbol,
            underlying=sym,
            qty=qty,
            entry_px=limit_px,
            expiry=contract.expiry,
            contract_type="put",
            action="buy_put",
        )
        log.info(
            "options_entry",
            symbol=sym,
            action="buy_put",
            contract=contract.symbol,
            strike=contract.strike,
            expiry=str(contract.expiry),
            dte=contract.dte,
            delta=round(contract.delta, 3),
            limit_px=limit_px,
            notional=notional,
        )
        return [{"symbol": contract.symbol, "side": OrderSide.BUY,
                 "qty": qty, "limit_px": limit_px, "notional": notional,
                 "action": "buy_put"}]

    def _enter_bull_call_spread(self, sym: str, close: float) -> list[dict] | None:
        max_per_share = MAX_OPTIONS_BUDGET / 100.0
        long_leg = self._chain.best_contract(
            sym, "call", target_delta=TARGET_DELTA,
            min_premium=0.05, max_premium=max_per_share,
        )
        if long_leg is None:
            return None
        short_leg = self._chain.spread_short_leg(long_leg, SPREAD_WIDTH)
        if short_leg is None:
            # Fall back to single long call if no short leg found
            return self._enter_long_call(sym, close)

        net_debit = long_leg.ask - short_leg.bid   # worst-case debit
        if net_debit <= 0 or net_debit * 100 > MAX_OPTIONS_BUDGET:
            return self._enter_long_call(sym, close)

        qty = min(MAX_CONTRACTS_PER_TRADE, max(1, int(MAX_OPTIONS_BUDGET / (net_debit * 100))))

        buy_limit  = round(long_leg.ask,  2)
        sell_limit = round(short_leg.bid, 2)
        notional   = round(net_debit * 100 * qty, 2)

        self._positions[sym] = _OptionPosition(
            contract_symbol  = long_leg.symbol,
            underlying       = sym,
            qty              = qty,
            entry_px         = buy_limit,
            expiry           = long_leg.expiry,
            contract_type    = "call",
            action           = "bull_call_spread",
            short_leg_symbol = short_leg.symbol,
            short_entry_px   = sell_limit,
        )
        log.info(
            "options_entry",
            symbol=sym,
            action="bull_call_spread",
            long_contract=long_leg.symbol,
            short_contract=short_leg.symbol,
            long_strike=long_leg.strike,
            short_strike=short_leg.strike,
            net_debit=round(net_debit, 2),
            notional=notional,
            dte=long_leg.dte,
        )
        return [
            {"symbol": long_leg.symbol,  "side": OrderSide.BUY,  "qty": qty,
             "limit_px": buy_limit,  "notional": round(buy_limit  * 100 * qty, 2),
             "action": "buy_call_spread_long"},
            {"symbol": short_leg.symbol, "side": OrderSide.SELL, "qty": qty,
             "limit_px": sell_limit, "notional": round(sell_limit * 100 * qty, 2),
             "action": "sell_call_spread_short"},
        ]

    def _enter_bear_put_spread(self, sym: str, close: float) -> list[dict] | None:
        max_per_share = MAX_OPTIONS_BUDGET / 100.0
        long_leg = self._chain.best_contract(
            sym, "put", target_delta=TARGET_DELTA,
            min_premium=0.05, max_premium=max_per_share,
        )
        if long_leg is None:
            return None
        short_leg = self._chain.spread_short_leg(long_leg, SPREAD_WIDTH)
        if short_leg is None:
            return self._enter_long_put(sym, close)

        net_debit = long_leg.ask - short_leg.bid
        if net_debit <= 0 or net_debit * 100 > MAX_OPTIONS_BUDGET:
            return self._enter_long_put(sym, close)

        qty = min(MAX_CONTRACTS_PER_TRADE, max(1, int(MAX_OPTIONS_BUDGET / (net_debit * 100))))

        buy_limit  = round(long_leg.ask,  2)
        sell_limit = round(short_leg.bid, 2)
        notional   = round(net_debit * 100 * qty, 2)

        self._positions[sym] = _OptionPosition(
            contract_symbol  = long_leg.symbol,
            underlying       = sym,
            qty              = qty,
            entry_px         = buy_limit,
            expiry           = long_leg.expiry,
            contract_type    = "put",
            action           = "bear_put_spread",
            short_leg_symbol = short_leg.symbol,
            short_entry_px   = sell_limit,
        )
        log.info(
            "options_entry",
            symbol=sym,
            action="bear_put_spread",
            long_contract=long_leg.symbol,
            short_contract=short_leg.symbol,
            net_debit=round(net_debit, 2),
            notional=notional,
            dte=long_leg.dte,
        )
        return [
            {"symbol": long_leg.symbol,  "side": OrderSide.BUY,  "qty": qty,
             "limit_px": buy_limit,  "notional": round(buy_limit  * 100 * qty, 2),
             "action": "buy_put_spread_long"},
            {"symbol": short_leg.symbol, "side": OrderSide.SELL, "qty": qty,
             "limit_px": sell_limit, "notional": round(sell_limit * 100 * qty, 2),
             "action": "sell_put_spread_short"},
        ]

    def _enter_csp(self, sym: str, close: float) -> list[dict] | None:
        """
        Level 1: Sell a cash-secured put.

        NOTE: Requires buying power = strike × 100 per contract.
        On a $345 account, this is only feasible for underlyings below ~$3.00
        (strike × 100 ≤ account equity). For most names in the universe this
        will return None after the premium cap filter. Consider using Level 2.
        """
        # CSP strike ≈ 2-3% below current price (slightly OTM)
        target_strike_pct_otm = 0.97
        contract = self._chain.best_contract(
            sym, "put",
            target_delta=0.30,      # slightly OTM for CSP
            min_premium=0.01,
            max_premium=close * 0.05,
        )
        if contract is None:
            log.warning("options_no_csp_found", symbol=sym,
                        note="account may be too small for cash-secured puts on this symbol")
            return None

        # Check buying power: strike × 100 must be < MAX_OPTIONS_BUDGET
        bp_required = contract.strike * 100
        if bp_required > MAX_OPTIONS_BUDGET:
            log.warning(
                "csp_bp_insufficient",
                symbol=sym,
                strike=contract.strike,
                bp_required=bp_required,
                budget=MAX_OPTIONS_BUDGET,
            )
            return None

        qty       = 1
        limit_px  = round(contract.bid, 2)   # sell at bid
        notional  = round(limit_px * 100 * qty, 2)

        self._positions[sym] = _OptionPosition(
            contract_symbol=contract.symbol,
            underlying=sym,
            qty=qty,
            entry_px=limit_px,
            expiry=contract.expiry,
            contract_type="put",
            action="sell_csp",
        )
        log.info(
            "options_entry",
            symbol=sym,
            action="sell_csp",
            contract=contract.symbol,
            strike=contract.strike,
            premium_received=limit_px,
            bp_required=bp_required,
        )
        return [{"symbol": contract.symbol, "side": OrderSide.SELL,
                 "qty": qty, "limit_px": limit_px, "notional": notional,
                 "action": "sell_csp"}]

    # ── Private: Exit builders ─────────────────────────────────────────────────

    def _should_close(self, sym: str, z: float, pos: _OptionPosition) -> bool:
        """True when the underlying z-score signals the trade is over."""
        if pos.action in ("buy_call", "bull_call_spread"):
            return z > Z_EXIT_LONG
        if pos.action in ("buy_put", "bear_put_spread"):
            return z < Z_EXIT_SHORT
        if pos.action == "sell_csp":
            # Close CSP when z reverts (buy-back the short put)
            return z > Z_EXIT_LONG
        return False

    def _build_close_orders(
        self, sym: str, pos: _OptionPosition, underlying_close: float
    ) -> list[dict]:
        """Build sell-to-close (or buy-to-close for short legs) order dicts."""
        orders: list[dict] = []

        # Look up current market price for limit order
        contract = self._chain.get_by_osi(pos.contract_symbol)
        close_px = round(contract.bid if contract else pos.entry_px * 0.80, 2)
        if close_px <= 0:
            close_px = 0.01   # floor to avoid zero-price order rejection

        notional = round(close_px * 100 * pos.qty, 2)

        if pos.action == "sell_csp":
            # CSP was a short put — buy-to-close
            close_side  = OrderSide.BUY
            close_action = "close_csp"
        else:
            # Long calls/puts — sell-to-close
            close_side  = OrderSide.SELL
            close_action = f"close_{pos.contract_type}"

        orders.append({
            "symbol":   pos.contract_symbol,
            "side":     close_side,
            "qty":      pos.qty,
            "limit_px": close_px,
            "notional": notional,
            "action":   close_action,
        })

        # For spreads, also close the short leg (buy-to-close it)
        if pos.short_leg_symbol:
            short_contract = self._chain.get_by_osi(pos.short_leg_symbol)
            short_close_px = round(
                short_contract.ask if short_contract else pos.short_entry_px * 1.20, 2
            )
            if short_close_px <= 0:
                short_close_px = 0.01
            orders.append({
                "symbol":   pos.short_leg_symbol,
                "side":     OrderSide.BUY,   # buy-to-close the short leg
                "qty":      pos.qty,
                "limit_px": short_close_px,
                "notional": round(short_close_px * 100 * pos.qty, 2),
                "action":   "close_spread_short_leg",
            })

        log.info(
            "options_exit",
            symbol=sym,
            action=close_action,
            contract=pos.contract_symbol,
            entry_px=pos.entry_px,
            close_px=close_px,
            pnl_est=round((close_px - pos.entry_px) * 100 * pos.qty, 2)
                    if pos.action != "sell_csp"
                    else round((pos.entry_px - close_px) * 100 * pos.qty, 2),
        )
        return orders

    # ── Private: Sizing ───────────────────────────────────────────────────────

    @staticmethod
    def _size_single_leg(
        contract: CachedContract,
    ) -> tuple[int, float, float]:
        """
        Returns (qty_contracts, limit_px_per_share, total_notional).

        Buys at ask (aggressive limit) for taker fills.
        qty capped by MAX_CONTRACTS_PER_TRADE and MAX_OPTIONS_BUDGET.
        """
        limit_px = round(contract.ask, 2)
        cost_per = limit_px * 100              # cost of 1 contract
        if cost_per <= 0:
            return 0, 0.0, 0.0
        qty = min(
            MAX_CONTRACTS_PER_TRADE,
            max(1, int(MAX_OPTIONS_BUDGET / cost_per)),
        )
        notional = round(limit_px * 100 * qty, 2)
        return qty, limit_px, notional
