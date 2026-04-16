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
  * Execution : execution.hl_manager.HyperliquidOrderManager(default_leverage=5).

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
import os
import secrets
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

# ── Universe ──────────────────────────────────────────────────────────────────
# State/SignalEngine are keyed by the Alpaca-style symbol ("BTC/USD") so the
# bar stream can drive evaluate() without translation. HL APIs want the coin
# name ("BTC"), so we translate at the order and feed edges only.
#
# Universe is env-driven (HL_UNIVERSE="BTC,ETH,SOL,..."). The per-coin maps
# (szDecimals, dust caps, coin↔symbol) are built at __init__ time from a live
# Info.meta() probe so adding a new coin never drifts from the venue.
#
# STRATEGY_TAG is retained at module scope — the test harness imports it.
STRATEGY_TAG    = "hl_taker_z"
DEFAULT_UNIVERSE = "BTC,ETH"

# Execution style flag (Spike B). "taker" = existing IOC cross-spread path,
# synchronous response. "maker" = Alo at best bid/ask, rests until filled, fill
# arrives asynchronously via userFills WS. Default stays "taker" so the env
# change alone flips behavior — no code path divergence at rest.
EXECUTION_STYLE = os.environ.get("EXECUTION_STYLE", "taker").lower().strip()

# ── Maker watchdog tuning (Spike C) ──────────────────────────────────────────
# How often the watchdog wakes to check resting quotes. 1 s is well under
# HL's 200-action-per-min wallet limit even with 2 symbols × worst-case churn.
MAKER_WATCHDOG_INTERVAL_S = 1.0
# Hard ceiling on how long a single maker intent can remain live (resting +
# reprices combined). Past this we cancel and roll back the optimistic memory
# write — the signal has to re-qualify on a later bar. Spike E will replace
# the rollback here with a taker escalation.
MAKER_MAX_LIFETIME_S = 30.0
# Max times one intent can be repriced before we give up. Bounds API churn
# when the book is moving faster than our quote can chase.
MAKER_MAX_REPRICES = 5

# ── HL venue price precision ──────────────────────────────────────────────────
# HL rule: price decimals ≤ max(6 - szDecimals, 0) AND significant figures ≤ 5.
# szDecimals is queried at boot via Info.meta(); dust caps derived from it.


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

        # ── Universe: env-driven, CSV, upper-cased, dedup, order-preserving ──
        raw_universe = os.environ.get("HL_UNIVERSE", DEFAULT_UNIVERSE)
        seen: set[str] = set()
        coins: list[str] = []
        for token in raw_universe.split(","):
            c = token.strip().upper()
            if c and c not in seen:
                seen.add(c)
                coins.append(c)
        if not coins:
            raise RuntimeError(
                f"HL_UNIVERSE produced empty coin list (raw={raw_universe!r})"
            )
        self._hl_coins: list[str] = coins
        self._coin_to_symbol: dict[str, str] = {c: f"{c}/USD" for c in coins}
        self._symbol_to_coin: dict[str, str] = {
            v: k for k, v in self._coin_to_symbol.items()
        }
        self._hl_symbols: list[str] = list(self._coin_to_symbol.values())

        # Fail loud at init if USDC is unbridged / agent is wrong — the leverage
        # pin is the canary. default_leverage=5 matches the prior "5-for-5" live
        # cadence on BTC and stays below every candidate coin's maxLeverage
        # floor (screener ≥ 3; BTC/ETH/SOL all support ≥ 20).
        self._default_leverage = 5
        self._hl = HyperliquidOrderManager(
            self._cfg,
            strategy_tag     = STRATEGY_TAG,
            default_leverage = self._default_leverage,
            coins            = self._hl_coins,
            is_cross         = True,
        )

        # Populate per-coin szDecimals from a live meta() probe. Any requested
        # coin missing from the venue universe is a hard failure — better to
        # refuse to start than trade a coin with unknown lot size.
        meta = self._hl._info.meta()
        venue_universe = meta.get("universe", [])
        venue_sz_dec: dict[str, int] = {}
        for asset in venue_universe:
            name = str(asset.get("name", "")).upper()
            if not name:
                continue
            try:
                venue_sz_dec[name] = int(asset.get("szDecimals", 0))
            except (TypeError, ValueError):
                continue
        missing = [c for c in self._hl_coins if c not in venue_sz_dec]
        if missing:
            raise RuntimeError(
                f"HL meta() missing szDecimals for requested coins: {missing}"
            )
        self._sz_decimals: dict[str, int] = {
            c: venue_sz_dec[c] for c in self._hl_coins
        }
        # Sub-lot dust tolerance used by flip-guard AND reconciler. 1.5 × lot
        # size — below this a residual is treated as flat. Keeps stale 1-lot
        # leftovers (observed: −0.0001 ETH from partial fills / manual UI)
        # from re-seeding the exit signal every bar after restart.
        self._dust_caps: dict[str, float] = {
            c: 1.5 * (10 ** -dec) for c, dec in self._sz_decimals.items()
        }

        # Per-pair notional split. Baseline: NOTIONAL_PER_TRADE was sized for
        # the 2-coin (BTC/ETH) universe, so an N-coin expansion divides that
        # same dollar budget across N pairs. For N ≤ 2 we keep the historical
        # sizing untouched; for N > 2 we scale down to protect MAX_ORDER_NOTIONAL
        # and daily-loss headroom.
        from strategy.signals import NOTIONAL_PER_TRADE as _BASE_NTL
        n = len(self._hl_coins)
        if n <= 2:
            per_pair_notional = _BASE_NTL
        else:
            per_pair_notional = _BASE_NTL * 2.0 / n
        self._per_pair_notional = per_pair_notional

        self._signals = SignalEngine(
            symbols            = self._hl_symbols,
            strategy_tag       = STRATEGY_TAG,
            allow_short        = True,
            notional_per_trade = per_pair_notional,
        )

        # Unified message queue — bars arrive from Alpaca LiveFeed with
        # symbol="BTC/USD", orderbooks arrive from HL pump with symbol rewritten
        # to "BTC/USD" so SignalEngine state matches.
        self._msg_q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._hl_raw_q: asyncio.Queue = asyncio.Queue(maxsize=2000)

        self._bars = LiveFeed(self._cfg, self._hl_symbols, self._msg_q)
        # userFills is address-scoped; passing the wallet turns on the
        # Spike-A subscription. Without it the feed runs L2-only.
        self._book = HyperliquidFeed(
            self._hl_coins,
            self._hl_raw_q,
            wallet = self._cfg.hl_wallet_address or None,
        )

        self._running = True

        # Spike B: symbol → {cloid, cid, side, qty, submit_ts} for each
        # resting Alo order awaiting a userFills WS confirmation. Strategy
        # loop skips a bar for any symbol with a live entry here, so we
        # don't dogpile signals while waiting for the quote to fill.
        # Spike C will add a cancel/replace deadline; Spike E will escalate
        # stale entries to taker.
        self._pending_resting: dict[str, dict] = {}

    # ── Boot: seed in-memory state from on-chain positions ───────────────────
    async def _reconcile_startup(self) -> None:
        try:
            positions = await self._hl.get_positions()
        except Exception as exc:
            log.warning("hl_reconcile_failed", error=str(exc))
            return
        log.info("hl_reconcile_ok", pos_count=len(positions))
        self._signals.reconcile_hl_positions(
            positions, self._coin_to_symbol, self._dust_caps,
        )

    # ── Pump: translate HL feed symbols onto the SignalEngine state keys ─────
    async def _hl_obi_pump(self) -> None:
        while self._running:
            msg = await self._hl_raw_q.get()
            coin = str(msg.get("symbol", "")).upper()
            sym  = self._coin_to_symbol.get(coin)
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
        coin = self._symbol_to_coin[sym]

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

        # Sub-lot dust tolerance: HL's minimum lot is 10^-szDecimals. A stray
        # residual smaller than ~1.5 lots (rounding / partial-fill leftovers /
        # manual UI leftovers) is "effectively flat" for guard purposes. Without
        # this, one lot of dust (observed: −0.0001 ETH) deadlocks entries
        # indefinitely — each bar blocks, reconciles dust back in, and rolls
        # back. The 1.5× multiplier stays well below a genuine 2-lot position.
        sz_dec     = self._sz_decimals.get(coin, 2)
        dust_cap   = self._dust_caps.get(coin, 1.5 * (10 ** -sz_dec))
        live_flat  = abs(live_szi) < dust_cap

        # Entry: memory should be pre-written with our intended signed qty;
        # live should be flat (the fill hasn't happened yet). Any live size
        # above dust means desync.
        # Exit: memory says non-zero; live should agree in sign. Live at-or-below
        # dust means the position was already closed elsewhere — skip.
        mismatch = False
        reason   = ""
        if is_entry:
            if not live_flat:
                mismatch = True
                reason   = "entry_but_live_nonzero"
        else:
            if live_flat:
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
            self._signals.reconcile_hl_positions(
                positions, self._coin_to_symbol, self._dust_caps,
            )
            if is_entry:
                self._signals.rollback_entry(sym)
            else:
                self._signals.rollback_exit(sym)
            return False

        return True

    # ── Maker (Alo) path ─────────────────────────────────────────────────────
    async def _submit_maker(self, sig: dict) -> dict | None:
        """
        Submit an Alo order at the non-crossing side of the book (best_bid for
        BUY, best_ask for SELL). Fill arrives asynchronously via userFills;
        _strategy_loop matches the cloid to clear self._pending_resting and
        hand the fill to SignalEngine.on_fill.

        HL rejects Alo orders that would cross the spread — if our cached
        best_bid/ask has drifted or is nan, we fall back to the signal's
        `limit_px` (strategy layer already computes a non-crossing limit).
        """
        sym  = sig["symbol"]
        coin = self._symbol_to_coin[sym]
        side = "buy" if sig["side"] == OrderSide.BUY else "sell"

        sz_dec      = self._sz_decimals.get(coin, 2)
        raw_qty     = sig["qty"]
        rounded_qty = _round_hl_size(raw_qty, sz_dec)
        if rounded_qty <= 0:
            log.warning(
                "hl_maker_qty_floored_to_zero",
                symbol=sym, coin=coin, raw_qty=raw_qty, sz_decimals=sz_dec,
            )
            return None

        st = self._signals._state[sym]
        if side == "buy":
            raw_px = st.best_bid
        else:
            raw_px = st.best_ask
        if not math.isfinite(raw_px) or raw_px <= 0:
            raw_px = sig["limit_px"]
        rounded_px = _round_hl_price(raw_px, sz_dec)

        cloid = f"0x{secrets.randbits(128):032x}"
        cid   = f"{STRATEGY_TAG}_{coin}_{int(time.time())}"
        log.info(
            "hl_maker_intent",
            client_order_id=cid, cloid=cloid,
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
            "tif":         "Alo",
            "reduce_only": False,
            "cloid":       cloid,
        }
        result = await self._hl.submit_order(hl_order)
        log.info("hl_maker_result", client_order_id=cid, cloid=cloid,
                 result=result)

        # Detect immediate rejection — HL returns status=ok with an `error`
        # field inside the per-order status when Alo can't rest (e.g. would
        # cross). Treat as a taker-style rejection so the caller rolls back.
        try:
            statuses = (result or {}).get("response", {}).get("data", {}).get("statuses", [])
            for s in statuses:
                if isinstance(s, dict) and s.get("error"):
                    log.warning(
                        "hl_maker_inner_rejection",
                        client_order_id=cid, cloid=cloid, error=s["error"],
                    )
                    return None
        except Exception:
            pass

        # Register resting order: strategy loop will gate new signals on this
        # symbol until the WS fill clears it. Spike-C watchdog uses last_px to
        # detect queue-behind drift, reprice_count to bound API churn, and
        # is_entry to pick the right rollback path on give-up.
        st_now = self._signals._state[sym]
        is_entry = not st_now.pending_exits.get(STRATEGY_TAG, False)
        self._pending_resting[sym] = {
            "cloid":         cloid.lower(),
            "cid":           cid,
            "side":          side,
            "qty":           rounded_qty,
            "last_px":       rounded_px,
            "reprice_count": 0,
            "is_entry":      is_entry,
            "submit_ts":     int(time.time()),
        }
        return result

    # ── Maker watchdog (Spike C): cancel/replace + rollback on stale quotes ──
    @staticmethod
    def _cancel_ok(resp: dict | None) -> bool:
        """
        True iff HL returned a success verdict for a cancel. Failure modes
        (e.g. "Order was already filled") return statuses[i] as a dict with
        "error" — in that case we leave the pending entry alone and let the
        userFills WS event clear it.
        """
        if not resp or resp.get("status") != "ok":
            return False
        statuses = resp.get("response", {}).get("data", {}).get("statuses", [])
        return any(isinstance(s, str) and s == "success" for s in statuses)

    def _rollback_pending(self, sym: str, is_entry: bool) -> None:
        if is_entry:
            self._signals.rollback_entry(sym)
        else:
            self._signals.rollback_exit(sym)

    async def _cancel_pending(self, sym: str, reason: str) -> None:
        """
        Cancel the resting order for `sym`, roll back the optimistic memory
        write, and clear the pending entry. Safe to call when no pending
        exists — no-op.
        """
        pending = self._pending_resting.get(sym)
        if pending is None:
            return
        coin  = self._symbol_to_coin[sym]
        cloid = pending["cloid"]
        age   = round(time.time() - pending["submit_ts"], 2)
        log.info(
            "hl_maker_giveup",
            symbol=sym, reason=reason, cloid=cloid, age_s=age,
            reprice_count=pending.get("reprice_count", 0),
            cid=pending["cid"],
        )
        try:
            resp = await self._hl.cancel_by_cloid(coin, cloid)
        except Exception as exc:
            log.warning("hl_maker_giveup_cancel_exception",
                        symbol=sym, cloid=cloid, error=str(exc))
            resp = None

        if self._cancel_ok(resp):
            # Order was resting and is now gone. Safe to clear pending and
            # roll back memory.
            self._pending_resting.pop(sym, None)
            self._rollback_pending(sym, pending["is_entry"])
            return

        # Cancel reported failure — most commonly because the order just
        # filled. Leave pending alone so the userFills WS event clears it
        # and fires on_fill. If the order is truly gone but we don't know,
        # the lifetime check on the next watchdog tick will pop it.
        log.warning("hl_maker_giveup_cancel_unconfirmed",
                    symbol=sym, cloid=cloid, resp=resp)

    async def _reprice_pending(self, sym: str, new_px: float) -> None:
        """
        Cancel the current resting order and resubmit at `new_px` with a
        fresh cloid. Preserves the signal's `cid` (on_fill still routes to
        the right intent when any attempt fills).
        """
        pending = self._pending_resting.get(sym)
        if pending is None:
            return
        coin   = self._symbol_to_coin[sym]
        old_cl = pending["cloid"]
        try:
            cresp = await self._hl.cancel_by_cloid(coin, old_cl)
        except Exception as exc:
            log.warning("hl_maker_reprice_cancel_exception",
                        symbol=sym, cloid=old_cl, error=str(exc))
            return

        if not self._cancel_ok(cresp):
            # The order filled (or is filling) while we were trying to
            # reprice. Leave pending alone — userFills will clean up.
            log.info("hl_maker_reprice_skipped_cancel_failed",
                     symbol=sym, cloid=old_cl, resp=cresp)
            return

        new_cloid = f"0x{secrets.randbits(128):032x}"
        log.info(
            "hl_maker_reprice",
            symbol=sym, coin=coin, side=pending["side"],
            qty=pending["qty"], old_px=pending["last_px"], new_px=new_px,
            old_cloid=old_cl, new_cloid=new_cloid,
            reprice_count=pending["reprice_count"] + 1, cid=pending["cid"],
        )

        hl_order = {
            "symbol":      coin,
            "side":        pending["side"],
            "qty":         pending["qty"],
            "limit_px":    new_px,
            "tif":         "Alo",
            "reduce_only": False,
            "cloid":       new_cloid,
        }
        result = await self._hl.submit_order(hl_order)

        # Inner-rejection check — if the new quote would cross, HL returns
        # status=ok with an inner error. In that case give up: the market
        # just ran through our level, so a taker is closer to the right
        # response than another reprice. Spike E will replace this rollback
        # with the escalation.
        inner_ok = True
        try:
            statuses = (result or {}).get("response", {}).get(
                "data", {}).get("statuses", [])
            for s in statuses:
                if isinstance(s, dict) and s.get("error"):
                    inner_ok = False
                    log.warning(
                        "hl_maker_reprice_inner_rejection",
                        symbol=sym, new_cloid=new_cloid, error=s["error"],
                    )
                    break
        except Exception:
            pass

        if result is None or not inner_ok:
            # Re-submission failed: roll back and clear pending.
            self._pending_resting.pop(sym, None)
            self._rollback_pending(sym, pending["is_entry"])
            return

        # Swap in the new cloid / px / counters. cid + side + qty + is_entry
        # + submit_ts (age baseline) all carry over unchanged.
        pending["cloid"]        = new_cloid.lower()
        pending["last_px"]      = new_px
        pending["reprice_count"] = pending["reprice_count"] + 1

    async def _maker_watchdog(self) -> None:
        """
        Periodic sweep of _pending_resting. For each live intent:
          1. lifetime > MAKER_MAX_LIFETIME_S → cancel + rollback.
          2. reprice_count ≥ MAKER_MAX_REPRICES → cancel + rollback.
          3. best bid/ask has moved "behind the queue" relative to our
             resting px → cancel + resubmit at the new best.

          "Behind the queue" means the market has moved *away from fill*:
          for a resting BUY, best_bid > our_px (the market is willing to
          pay more than us, so new buy interest stacks above us); for a
          SELL, best_ask < our_px. When the market moves *toward* fill
          (best_bid falls for a buy, best_ask rises for a sell) we stay
          put — we're already at a better price than the new best, and
          moving would widen the spread we captured.
        """
        while self._running:
            await asyncio.sleep(MAKER_WATCHDOG_INTERVAL_S)
            if not self._pending_resting:
                continue

            now_ts = time.time()
            for sym in list(self._pending_resting.keys()):
                pending = self._pending_resting.get(sym)
                if pending is None:
                    continue

                age = now_ts - pending["submit_ts"]
                if age >= MAKER_MAX_LIFETIME_S:
                    await self._cancel_pending(sym, reason="lifetime_exceeded")
                    continue
                if pending["reprice_count"] >= MAKER_MAX_REPRICES:
                    await self._cancel_pending(sym, reason="max_reprices")
                    continue

                coin = self._symbol_to_coin.get(sym)
                if coin is None:
                    continue
                st = self._signals._state.get(sym)
                if st is None:
                    continue
                sz_dec = self._sz_decimals.get(coin, 2)

                side = pending["side"]
                raw  = st.best_bid if side == "buy" else st.best_ask
                if not math.isfinite(raw) or raw <= 0:
                    continue
                new_px = _round_hl_price(raw, sz_dec)
                cur_px = pending["last_px"]

                behind = (side == "buy"  and new_px > cur_px) or \
                         (side == "sell" and new_px < cur_px)
                if behind:
                    await self._reprice_pending(sym, new_px)

    # ── Signal → HL order translation + CID logging ──────────────────────────
    async def _submit(self, sig: dict) -> dict | None:
        # Route by execution style. Env-gated so the taker contract is
        # untouched when EXECUTION_STYLE is unset or "taker".
        if EXECUTION_STYLE == "maker":
            return await self._submit_maker(sig)
        sym   = sig["symbol"]
        coin  = self._symbol_to_coin[sym]
        side  = "buy" if sig["side"] == OrderSide.BUY else "sell"

        # SignalEngine uses Alpaca-style precision; HL has stricter rules.
        raw_px      = sig["limit_px"]
        sz_dec      = self._sz_decimals.get(coin, 2)
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

            # HL has no TradingStream equivalent wired today — the Alpaca path
            # drives signals.on_fill from user_trades WS events, but hl_feed
            # only subscribes to l2Book. For IOC (taker) the synchronous
            # response is terminal, so parse `filled` here and synthesize
            # on_fill() ourselves. Without this, pending_exits[tag] never
            # clears and evaluate() short-circuits forever after the first
            # exit. For future maker/Alo orders that can rest we'll need a
            # userFills WS subscription.
            for s in statuses:
                if isinstance(s, dict) and "filled" in s:
                    try:
                        filled_sz = float(s["filled"].get("totalSz", 0))
                    except (TypeError, ValueError):
                        filled_sz = 0.0
                    if filled_sz > 0:
                        self._signals.on_fill(
                            client_order_id = cid,
                            symbol          = sym,
                            qty             = filled_sz,
                            side            = side,
                        )
        except Exception:
            pass

        return result

    # ── Async fill routing (Spike A + B) ─────────────────────────────────────
    def _handle_hl_fill(self, msg: dict) -> None:
        """
        Match a userFills WS event to a resting Alo order and drive on_fill.
        WS cloid comes as hex; we normalize to lowercase for comparison.

        Fills without a cloid, or whose cloid is not in self._pending_resting,
        are ignored here — those are either taker fills (handled synchronously
        in _submit) or out-of-band UI activity. Both cases are already
        reconciled by the startup + flip-guard reconcile paths.
        """
        sym   = msg.get("symbol")
        cloid = msg.get("cloid")
        if not (sym and cloid):
            return
        cloid_lc = str(cloid).lower()

        pending = self._pending_resting.get(sym)
        if not pending or pending["cloid"] != cloid_lc:
            return

        try:
            filled_sz = float(msg["sz"])
        except (KeyError, TypeError, ValueError):
            filled_sz = 0.0
        if filled_sz <= 0:
            return

        cumulative = pending.get("filled_qty", 0.0) + filled_sz
        remaining  = pending["qty"] - cumulative
        # Floor at half a lot — anything below is dust that HL can't match,
        # so treat as terminal.
        coin       = self._symbol_to_coin.get(sym, "")
        sz_dec     = self._sz_decimals.get(coin, 2)
        dust_half  = 0.5 * (10 ** -sz_dec)
        terminal   = remaining <= dust_half

        log.info(
            "hl_maker_fill_matched",
            symbol=sym, cloid=cloid_lc,
            client_order_id=pending["cid"],
            fill_sz=filled_sz, px=msg.get("px"),
            crossed=msg.get("crossed"),
            cumulative=cumulative, remaining=remaining,
            terminal=terminal,
        )
        # signals.on_fill overwrites (not accumulates) and clears pending_exits
        # on the first call. Fire it only once per order, on the terminal fill,
        # with the cumulative qty — otherwise multi-chunk entries leave mem at
        # the last chunk size (→ under-sized exit → dust residual), and
        # multi-chunk exits resurrect a phantom position after the close.
        if terminal:
            self._signals.on_fill(
                client_order_id = pending["cid"],
                symbol          = sym,
                qty             = cumulative,
                side            = pending["side"],
            )
            del self._pending_resting[sym]
        else:
            pending["filled_qty"] = cumulative

    # ── Main loop ────────────────────────────────────────────────────────────
    async def _strategy_loop(self) -> None:
        while self._running:
            msg = await self._msg_q.get()

            if msg["type"] == "orderbook":
                # Only accept orderbooks we've mapped to self._hl_symbols; the Alpaca
                # LiveFeed also emits orderbook/quote messages for its own
                # venue. We ignore those — HL OBI is authoritative for this
                # engine (HL pump rewrites coin→symbol before enqueueing).
                if msg.get("symbol") in self._signals._state:
                    self._signals.update_orderbook(msg)
                continue

            if msg["type"] == "hl_fill":
                self._handle_hl_fill(msg)
                continue

            if msg["type"] != "bar":
                continue

            # Maker gate: if a resting Alo order for this symbol is still
            # awaiting a fill, don't run evaluate() — the strategy already
            # has an open intent on the book. Spike C enforces a max-wait
            # before cancel/replace; Spike E adds taker escalation.
            if msg.get("symbol") in self._pending_resting:
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
            symbols=self._hl_symbols, coins=self._hl_coins,
            tag=STRATEGY_TAG, leverage=self._default_leverage,
            per_pair_notional=self._per_pair_notional,
            mode=self._cfg.execution_mode.value,
            execution_style=EXECUTION_STYLE,
        )
        print(
            f"\n[HL-ENGINE] Tag={STRATEGY_TAG}  Coins={self._hl_coins}  "
            f"Leverage={self._default_leverage}x  "
            f"PerPair=${self._per_pair_notional:.2f}  "
            f"Mode={self._cfg.execution_mode.value}  "
            f"Style={EXECUTION_STYLE}\n"
            f"           Logs → logs/hl_engine.jsonl\n"
            f"           Ctrl-C to stop.\n"
        )

        await self._reconcile_startup()

        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._bars.run(),       name="alpaca_bars")
            tg.create_task(self._book.run(),       name="hl_orderbook")
            tg.create_task(self._hl_obi_pump(),    name="hl_obi_pump")
            tg.create_task(self._strategy_loop(),  name="strategy")
            # Watchdog only runs in maker mode; taker path never rests.
            if EXECUTION_STYLE == "maker":
                tg.create_task(self._maker_watchdog(), name="maker_watchdog")

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
