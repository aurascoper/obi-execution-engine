#!/usr/bin/env python3
"""
hl_engine.py — Hyperliquid bi-directional Z-score engine (Phase 4).

Orchestration (not wired into launch.py by default — run directly):
  python hl_engine.py

Wiring:
  * Bars      : Alpaca CryptoDataStream (via data.feed.LiveFeed) for 1-min OHLC
                on BTC/USD and ETH/USD. Signal source assumption: Alpaca spot
                is the "truer mean" to reversion-trade against HL perps.
  * Order book: data.hl_feed.HyperliquidFeed for L2 → OBI.
  * Signals   : strategy.signals.SignalEngine(strategy_tag="hl_taker_z",
                                              allow_short=True).
  * Execution : execution.hl_manager.HyperliquidOrderManager(default_leverage=2).

Sign-flip guard:
  Before each submission we pull get_positions() and abort if the live HL
  position is inconsistent with the in-memory direction. evaluate() already
  prevents a single-bar long→short flip (it emits an exit while in-position
  and a fresh entry only after on_fill clears), so this guard protects
  against out-of-band state: manual UI trades, missed fills, restarts before
  reconcile finishes.

Client-order-id parity:
  Every submission is logged with  hl_taker_z_{COIN}_{epoch}  for log-parser
  compatibility with the Phase 3 tagging scheme.

Preconditions before first run:
  * HL_WALLET_ADDRESS and HL_PRIVATE_KEY present in env / .env
  * USDC bridged to the HL account (update_leverage() fails fast otherwise)
  * Alpaca paper API keys present (bars come from the Alpaca stream)
"""

from __future__ import annotations

import asyncio
import logging
import math
import signal as signal_lib
import time
from pathlib import Path

import structlog
from alpaca.trading.enums import OrderSide

from config.settings import load as load_settings
from data.feed                 import LiveFeed
from data.hl_feed              import HyperliquidFeed
from execution.hl_manager      import HyperliquidOrderManager
from strategy.signals          import SignalEngine

# ── Logging ───────────────────────────────────────────────────────────────────
Path("logs").mkdir(exist_ok=True)
structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    logger_factory=structlog.WriteLoggerFactory(
        file=open("logs/hl_engine.jsonl", "a", buffering=1)
    ),
)
log = structlog.get_logger("hl_engine")

# ── Universe mapping ──────────────────────────────────────────────────────────
# State/SignalEngine are keyed by the Alpaca-style symbol ("BTC/USD") so the
# bar stream can drive evaluate() without translation. HL APIs want the coin
# name ("BTC"), so we translate at the order and feed edges only.
COIN_TO_SYMBOL: dict[str, str] = {
    "BTC": "BTC/USD",
    "ETH": "ETH/USD",
}
SYMBOL_TO_COIN: dict[str, str] = {v: k for k, v in COIN_TO_SYMBOL.items()}

HL_COINS    = list(COIN_TO_SYMBOL.keys())
HL_SYMBOLS  = list(COIN_TO_SYMBOL.values())
STRATEGY_TAG = "hl_taker_z"

# ── HL venue price precision ──────────────────────────────────────────────────
# HL rule: price decimals ≤ max(6 - szDecimals, 0) AND significant figures ≤ 5.
# Values from Info.meta() probe — extend when universe grows.
_HL_SZ_DECIMALS: dict[str, int] = {"BTC": 5, "ETH": 4}


def _round_hl_price(px: float, sz_decimals: int) -> float:
    if px <= 0:
        return px
    max_from_sz  = max(0, 6 - sz_decimals)
    int_digits   = max(1, int(math.floor(math.log10(px))) + 1)
    max_from_sig = max(0, 5 - int_digits)
    return round(px, min(max_from_sz, max_from_sig))


def _round_hl_size(qty: float, sz_decimals: int) -> float:
    # Floor to the venue lot size. Truncating (not rounding) guarantees the
    # resulting notional stays ≤ the signal's intended cap — rounding up could
    # breach MAX_ORDER_NOTIONAL or margin.
    if qty <= 0:
        return qty
    factor = 10 ** sz_decimals
    return math.floor(qty * factor) / factor


class HLEngine:
    def __init__(self) -> None:
        self._cfg = load_settings()

        # Fail loud at init if USDC is unbridged / agent is wrong — the leverage
        # pin is the canary.
        self._hl = HyperliquidOrderManager(
            self._cfg,
            strategy_tag     = STRATEGY_TAG,
            default_leverage = 2,
            coins            = HL_COINS,
            is_cross         = True,
        )

        self._signals = SignalEngine(
            symbols      = HL_SYMBOLS,
            strategy_tag = STRATEGY_TAG,
            allow_short  = True,
        )

        # Unified message queue — bars arrive from Alpaca LiveFeed with
        # symbol="BTC/USD", orderbooks arrive from HL pump with symbol rewritten
        # to "BTC/USD" so SignalEngine state matches.
        self._msg_q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._hl_raw_q: asyncio.Queue = asyncio.Queue(maxsize=2000)

        self._bars = LiveFeed(self._cfg, HL_SYMBOLS, self._msg_q)
        self._book = HyperliquidFeed(HL_COINS, self._hl_raw_q)

        self._running = True

    # ── Boot: seed in-memory state from on-chain positions ───────────────────
    async def _reconcile_startup(self) -> None:
        try:
            positions = await self._hl.get_positions()
        except Exception as exc:
            log.warning("hl_reconcile_failed", error=str(exc))
            return
        log.info("hl_reconcile_ok", pos_count=len(positions))
        self._signals.reconcile_hl_positions(positions, COIN_TO_SYMBOL)

    # ── Pump: translate HL feed symbols onto the SignalEngine state keys ─────
    async def _hl_obi_pump(self) -> None:
        while self._running:
            msg = await self._hl_raw_q.get()
            coin = str(msg.get("symbol", "")).upper()
            sym  = COIN_TO_SYMBOL.get(coin)
            if sym is None:
                continue
            msg["symbol"] = sym
            await self._msg_q.put(msg)

    # ── Sign-flip guard: live-state check against in-memory direction ────────
    async def _flip_guard_ok(self, sig: dict) -> bool:
        """
        Returns True if live HL state is consistent with our intent.

        On mismatch: reconcile from on-chain, rollback the optimistic memory
        write, and return False so the engine loop skips this bar. Next
        qualifying bar will re-evaluate against reconciled state.
        """
        sym  = sig["symbol"]
        coin = SYMBOL_TO_COIN[sym]

        try:
            positions = await self._hl.get_positions()
        except Exception as exc:
            log.warning("hl_flip_guard_query_failed",
                        symbol=sym, error=str(exc))
            return False

        live_szi = 0.0
        for p in positions:
            if str(p.get("coin", "")).upper() == coin:
                live_szi = float(p.get("szi", 0) or 0)
                break

        st           = self._signals._state[sym]
        mem_szi      = st.open_qty(STRATEGY_TAG)
        pending_exit = st.pending_exits.get(STRATEGY_TAG, False)

        is_entry = not pending_exit    # exit signals set pending_exits=True

        # Entry: memory should be pre-written with our intended signed qty;
        # live should be flat (the fill hasn't happened yet). Any live size
        # means desync.
        # Exit: memory says non-zero; live should agree in sign. Live-flat
        # means the position was already closed elsewhere — skip.
        mismatch = False
        reason   = ""
        if is_entry:
            if live_szi != 0.0:
                mismatch = True
                reason   = "entry_but_live_nonzero"
        else:
            if live_szi == 0.0:
                mismatch = True
                reason   = "exit_but_live_flat"
            elif (live_szi * mem_szi) < 0:
                mismatch = True
                reason   = "exit_side_sign_mismatch"

        if mismatch:
            log.warning(
                "hl_flip_guard_blocked",
                symbol=sym, reason=reason,
                mem_szi=mem_szi, live_szi=live_szi,
                pending_exit=pending_exit,
            )
            # Reconcile truth-on-chain → memory, then undo the optimistic write
            # for the blocked signal so evaluate() re-fires on the next bar.
            self._signals.reconcile_hl_positions(positions, COIN_TO_SYMBOL)
            if is_entry:
                self._signals.rollback_entry(sym)
            else:
                self._signals.rollback_exit(sym)
            return False

        return True

    # ── Signal → HL order translation + CID logging ──────────────────────────
    async def _submit(self, sig: dict) -> dict | None:
        sym   = sig["symbol"]
        coin  = SYMBOL_TO_COIN[sym]
        side  = "buy" if sig["side"] == OrderSide.BUY else "sell"

        # SignalEngine uses Alpaca-style precision; HL has stricter rules.
        raw_px      = sig["limit_px"]
        sz_dec      = _HL_SZ_DECIMALS.get(coin, 2)
        rounded_px  = _round_hl_price(raw_px, sz_dec)
        raw_qty     = sig["qty"]
        rounded_qty = _round_hl_size(raw_qty, sz_dec)

        if rounded_qty <= 0:
            log.warning(
                "hl_order_qty_floored_to_zero",
                symbol=sym, coin=coin, raw_qty=raw_qty, sz_decimals=sz_dec,
            )
            return None

        cid = f"{STRATEGY_TAG}_{coin}_{int(time.time())}"
        log.info(
            "hl_order_intent",
            client_order_id=cid,
            symbol=sym, coin=coin, side=side,
            qty=rounded_qty, raw_qty=raw_qty,
            limit_px=rounded_px, raw_limit_px=raw_px,
            notional=sig.get("notional"),
        )

        hl_order = {
            "symbol":      coin,
            "side":        side,
            "qty":         rounded_qty,
            "limit_px":    rounded_px,
            # IOC: cross-spread taker behaviour; any unfilled residual cancels
            # instead of resting. Matches the original live_engine taker intent.
            "tif":         "Ioc",
            "reduce_only": False,
        }
        result = await self._hl.submit_order(hl_order)
        log.info("hl_order_result", client_order_id=cid, result=result)

        # HL returns status=ok even when the order is rejected by the validator.
        # The per-order verdict lives in response.data.statuses[i].
        try:
            statuses = (result or {}).get("response", {}).get("data", {}).get("statuses", [])
            for s in statuses:
                if isinstance(s, dict) and s.get("error"):
                    log.warning(
                        "hl_order_inner_rejection",
                        client_order_id=cid, error=s["error"],
                        sent_px=rounded_px, raw_px=raw_px,
                        sent_qty=rounded_qty, raw_qty=raw_qty,
                    )
                    return None  # triggers rollback in _strategy_loop
        except Exception:
            pass

        return result

    # ── Main loop ────────────────────────────────────────────────────────────
    async def _strategy_loop(self) -> None:
        while self._running:
            msg = await self._msg_q.get()

            if msg["type"] == "orderbook":
                # Only accept orderbooks we've mapped to HL_SYMBOLS; the Alpaca
                # LiveFeed also emits orderbook/quote messages for its own
                # venue. We ignore those — HL OBI is authoritative for this
                # engine (HL pump rewrites coin→symbol before enqueueing).
                if msg.get("symbol") in self._signals._state:
                    self._signals.update_orderbook(msg)
                continue

            if msg["type"] != "bar":
                continue

            sig = self._signals.evaluate(msg)
            if sig is None:
                continue

            if not await self._flip_guard_ok(sig):
                continue

            result = await self._submit(sig)
            if result is None:
                # Order rejected by hl_manager (malformed / SDK error). Roll
                # back the optimistic memory write so the next bar retries.
                if sig.get("side") == OrderSide.BUY:
                    # BUY is either a long entry or a short cover. Exits set
                    # pending_exits[tag]=True, so check that to pick the right
                    # rollback path.
                    st = self._signals._state[sig["symbol"]]
                    if st.pending_exits.get(STRATEGY_TAG, False):
                        self._signals.rollback_exit(sig["symbol"])
                    else:
                        self._signals.rollback_entry(sig["symbol"])
                else:
                    st = self._signals._state[sig["symbol"]]
                    if st.pending_exits.get(STRATEGY_TAG, False):
                        self._signals.rollback_exit(sig["symbol"])
                    else:
                        self._signals.rollback_entry(sig["symbol"])

    # ── Lifecycle ────────────────────────────────────────────────────────────
    async def run(self) -> None:
        log.info(
            "hl_engine_start",
            symbols=HL_SYMBOLS, coins=HL_COINS,
            tag=STRATEGY_TAG, leverage=2, mode=self._cfg.execution_mode.value,
        )
        print(
            f"\n[HL-ENGINE] Tag={STRATEGY_TAG}  Coins={HL_COINS}  "
            f"Leverage=2x  Mode={self._cfg.execution_mode.value}\n"
            f"           Logs → logs/hl_engine.jsonl\n"
            f"           Ctrl-C to stop.\n"
        )

        await self._reconcile_startup()

        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._bars.run(),       name="alpaca_bars")
            tg.create_task(self._book.run(),       name="hl_orderbook")
            tg.create_task(self._hl_obi_pump(),    name="hl_obi_pump")
            tg.create_task(self._strategy_loop(),  name="strategy")

    def stop(self) -> None:
        log.info("hl_engine_shutdown")
        self._running = False
        self._book.stop()


async def main() -> None:
    engine = HLEngine()
    loop   = asyncio.get_running_loop()
    for s in (signal_lib.SIGINT, signal_lib.SIGTERM):
        loop.add_signal_handler(s, engine.stop)
    await engine.run()


if __name__ == "__main__":
    asyncio.run(main())
