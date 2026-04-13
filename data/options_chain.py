"""
data/options_chain.py — REST-based options chain cache.

Alpaca options data is REST-only (no WebSocket). This module polls once per
minute for contracts within a DTE window, batch-fetches snapshots (greeks +
NBBO), and exposes best_contract() for the signal engine to select a strike.

Exact SDK field names verified against installed alpaca-py:
  OptionsSnapshot.latest_quote  → Quote  (.bid_price, .ask_price, .bid_size, .ask_size)
  OptionsSnapshot.greeks        → OptionsGreeks  (.delta, .gamma, .theta, .vega)
  OptionsSnapshot.implied_volatility → float | None
  OptionContract.symbol         → OSI string
  OptionContract.expiration_date
  OptionContract.strike_price
  OptionContract.type           → ContractType  ("call" | "put")
  OptionContract.open_interest
  OptionSnapshotRequest.symbol_or_symbols → str | list[str]
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

import structlog
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionSnapshotRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import ContractType
from alpaca.trading.requests import GetOptionContractsRequest

log = structlog.get_logger(__name__)

# Refresh interval (seconds). Keep short enough to catch mid-day IV moves.
CHAIN_REFRESH_S = 60

# Quality filters — tighten or loosen based on liquidity of your universe.
MIN_OPEN_INTEREST     = 50      # skip illiquid contracts
MAX_SPREAD_PCT        = 0.20    # max (ask - bid) / mid; 20% = wide but acceptable
MAX_CONTRACTS_IN_SNAP = 200     # batch ceiling for snapshot API call per symbol


@dataclass(slots=True)
class CachedContract:
    """Single options contract: metadata merged with live snapshot data."""
    symbol:        str           # OSI  e.g. "TSLA250418C00250000"
    underlying:    str           # e.g. "TSLA"
    expiry:        date
    strike:        float
    contract_type: Literal["call", "put"]
    delta:         float         # from greeks; nan if unavailable
    bid:           float
    ask:           float
    mid:           float
    open_interest: int
    iv:            float         # implied volatility; nan if unavailable

    @property
    def dte(self) -> int:
        return max(0, (self.expiry - date.today()).days)

    @property
    def spread_pct(self) -> float:
        """Bid-ask spread as a fraction of mid. 0 when mid = 0."""
        return (self.ask - self.bid) / self.mid if self.mid > 0 else 1.0

    @property
    def total_cost(self) -> float:
        """Mid-market premium for 1 contract (× 100 shares)."""
        return round(self.mid * 100, 2)


class OptionsChainCache:
    """
    Background-refreshed options chain.

    Instantiate once; pass to OptionsSignalEngine.  Call .run() inside the
    engine's TaskGroup so it refreshes every CHAIN_REFRESH_S seconds.

    All state is single-threaded asyncio — no locks needed.
    """

    def __init__(
        self,
        trading_client: TradingClient,
        data_client:    OptionHistoricalDataClient,
        underlyings:    list[str],
        min_dte:        int = 7,
        max_dte:        int = 21,
    ) -> None:
        self._tc          = trading_client
        self._dc          = data_client
        self._underlyings = underlyings
        self._min_dte     = min_dte
        self._max_dte     = max_dte
        self._running     = True
        # underlying → list[CachedContract]
        self._cache: dict[str, list[CachedContract]] = {s: [] for s in underlyings}

    # ── Background refresh loop ───────────────────────────────────────────────

    async def run(self) -> None:
        """Run as a TaskGroup task. Refreshes chain once per minute."""
        # Do one immediate refresh so data is ready for the first bar.
        await self._refresh_all()
        while self._running:
            await asyncio.sleep(CHAIN_REFRESH_S)
            await self._refresh_all()

    def stop(self) -> None:
        self._running = False

    # ── Public query interface ────────────────────────────────────────────────

    def best_contract(
        self,
        underlying:    str,
        contract_type: Literal["call", "put"],
        target_delta:  float = 0.45,
        min_premium:   float = 0.10,
        max_premium:   float = 5.00,
    ) -> CachedContract | None:
        """
        Return the contract closest to |target_delta| that passes quality filters.

        target_delta is an absolute value (0.45 ≈ near-ATM for both calls and puts).
        min/max_premium are per-share bounds (× 100 for total contract cost).
        """
        candidates = [
            c for c in self._cache.get(underlying, [])
            if c.contract_type == contract_type
            and not math.isnan(c.delta)
            and c.open_interest >= MIN_OPEN_INTEREST
            and c.spread_pct    <= MAX_SPREAD_PCT
            and c.bid           >  0
            and min_premium     <= c.mid <= max_premium
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda c: abs(abs(c.delta) - target_delta))

    def spread_short_leg(
        self,
        long_contract: CachedContract,
        spread_width:  float = 5.0,
    ) -> CachedContract | None:
        """
        Find a short-leg contract for a vertical spread.

        For a bull call spread: long_contract is the lower strike (ATM call);
        the short leg is spread_width above it, same expiry.

        For a bear put spread: long_contract is the higher strike (ATM put);
        the short leg is spread_width below it, same expiry.
        """
        ctype = long_contract.contract_type
        exp   = long_contract.expiry

        if ctype == "call":
            target_strike = long_contract.strike + spread_width
        else:
            target_strike = long_contract.strike - spread_width

        candidates = [
            c for c in self._cache.get(long_contract.underlying, [])
            if c.contract_type == ctype
            and c.expiry        == exp
            and c.bid           >  0
            and c.open_interest >= MIN_OPEN_INTEREST
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda c: abs(c.strike - target_strike))

    def get_by_osi(self, osi_symbol: str) -> CachedContract | None:
        """Look up a cached contract by its OSI symbol string."""
        for contracts in self._cache.values():
            for c in contracts:
                if c.symbol == osi_symbol:
                    return c
        return None

    def snapshot(self) -> dict[str, int]:
        """Return count of cached contracts per underlying (for logging)."""
        return {u: len(v) for u, v in self._cache.items()}

    # ── Private ───────────────────────────────────────────────────────────────

    async def _refresh_all(self) -> None:
        try:
            await asyncio.to_thread(self._sync_refresh_all)
        except Exception as exc:
            log.warning(
                "options_chain_refresh_error",
                exc_type=type(exc).__name__,
                exc_msg=str(exc)[:160],
            )

    def _sync_refresh_all(self) -> None:
        today   = date.today()
        exp_min = today + timedelta(days=self._min_dte)
        exp_max = today + timedelta(days=self._max_dte)
        for sym in self._underlyings:
            try:
                self._refresh_symbol(sym, exp_min, exp_max)
            except Exception as exc:
                log.warning(
                    "options_chain_symbol_error",
                    symbol=sym,
                    exc_type=type(exc).__name__,
                    exc_msg=str(exc)[:120],
                )

    def _refresh_symbol(self, sym: str, exp_min: date, exp_max: date) -> None:
        # 1. Fetch all active contracts in the DTE window
        req = GetOptionContractsRequest(
            underlying_symbols=[sym],
            expiration_date_gte=exp_min,
            expiration_date_lte=exp_max,
            status="active",
        )
        result   = self._tc.get_option_contracts(req)
        # Result may be a list or a paginated wrapper — normalize.
        contracts = list(result) if result else []
        if not contracts:
            self._cache[sym] = []
            log.debug("options_chain_empty", symbol=sym, dte_window=f"{exp_min}…{exp_max}")
            return

        # 2. Batch-fetch snapshots (greeks + NBBO) for all OSI symbols.
        #    API allows up to ~1000 symbols per request, but cap for safety.
        osi_symbols = [c.symbol for c in contracts[:MAX_CONTRACTS_IN_SNAP]]
        try:
            snapshots: dict[str, object] = self._dc.get_option_snapshot(
                OptionSnapshotRequest(symbol_or_symbols=osi_symbols)
            )
        except Exception as exc:
            log.warning(
                "options_snapshot_fetch_error",
                symbol=sym,
                n_contracts=len(osi_symbols),
                exc_type=type(exc).__name__,
                exc_msg=str(exc)[:120],
            )
            snapshots = {}

        # 3. Merge contract metadata + snapshot data → CachedContract list.
        cached: list[CachedContract] = []
        for c in contracts[:MAX_CONTRACTS_IN_SNAP]:
            snap = snapshots.get(c.symbol)
            if snap is None:
                continue

            greeks = getattr(snap, "greeks", None)
            delta  = float(greeks.delta) \
                     if greeks is not None and greeks.delta is not None \
                     else float("nan")
            iv     = float(snap.implied_volatility) \
                     if snap.implied_volatility is not None \
                     else float("nan")

            quote  = getattr(snap, "latest_quote", None)
            if quote is None:
                continue
            bid = float(getattr(quote, "bid_price", 0) or 0)
            ask = float(getattr(quote, "ask_price", 0) or 0)
            if bid <= 0 or ask <= 0:
                continue
            mid = (bid + ask) / 2.0

            # expiration_date may be a date object or ISO string
            exp = c.expiration_date
            if isinstance(exp, str):
                from datetime import datetime as _dt
                exp = _dt.strptime(exp[:10], "%Y-%m-%d").date()

            ctype: Literal["call", "put"] = (
                "call"
                if getattr(c, "type", None) == ContractType.CALL
                   or str(getattr(c, "type", "")).lower() == "call"
                else "put"
            )
            oi = int(getattr(c, "open_interest", 0) or 0)

            cached.append(CachedContract(
                symbol        = c.symbol,
                underlying    = sym,
                expiry        = exp,
                strike        = float(c.strike_price),
                contract_type = ctype,
                delta         = delta,
                bid           = bid,
                ask           = ask,
                mid           = mid,
                open_interest = oi,
                iv            = iv,
            ))

        self._cache[sym] = cached
        log.debug(
            "options_chain_refreshed",
            symbol   = sym,
            contracts= len(cached),
            exp_range= f"{exp_min}…{exp_max}",
        )
