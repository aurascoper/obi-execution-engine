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
  * Signals   : strategy.signals.SignalEngine(strategy_tag="hl_z",
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
  Every submission is logged with  hl_z_{COIN}_{epoch}  for log-parser
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
from control.server import ControlPlaneServer
from data.feed import LiveFeed
from data.hl_feed import HyperliquidFeed
from execution.hl_manager import HyperliquidOrderManager
from strategy.signals import SignalEngine

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
STRATEGY_TAG = "hl_z"
DEFAULT_UNIVERSE = "BTC,ETH"

# Coins Alpaca CryptoDataStream actually delivers 1-min bars for. Any coin NOT
# in this set gets HL-native bars synthesized from L2 midprice ticks instead.
ALPACA_BAR_COINS = frozenset({"BTC", "ETH", "SOL", "DOGE", "AVAX", "LINK"})

# Native bar synthesis interval — matches Alpaca's 1-min bar cadence.
NATIVE_BAR_INTERVAL_S = 60

# Execution style flag (Spike B). "taker" = existing IOC cross-spread path,
# synchronous response. "maker" = Alo at best bid/ask, rests until filled, fill
# arrives asynchronously via userFills WS. Default stays "taker" so the env
# change alone flips behavior — no code path divergence at rest.
EXECUTION_STYLE = os.environ.get("EXECUTION_STYLE", "taker").lower().strip()

# ── Maker watchdog tuning (Spike C) ──────────────────────────────────────────
MAKER_WATCHDOG_INTERVAL_S = 1.0
# Native crypto: tight timeouts (deep books, fast takers).
MAKER_MAX_LIFETIME_S = 30.0
MAKER_MAX_REPRICES = 5
# HIP-3 equity perps: wider spreads, sparser taker flow — need more patience.
HIP3_MAKER_MAX_LIFETIME_S = 120.0
HIP3_MAKER_MAX_REPRICES = 15
# Z-score threshold for taker escalation: if |z| exceeds this at signal time,
# skip maker and send IOC taker to guarantee fill on extreme dislocations.
HIP3_TAKER_ESCALATION_Z = 3.0

# ── HL venue price precision ──────────────────────────────────────────────────
# HL rule: price decimals ≤ max(6 - szDecimals, 0) AND significant figures ≤ 5.
# szDecimals is queried at boot via Info.meta(); dust caps derived from it.


def _round_hl_price(px: float, sz_decimals: int) -> float:
    if px <= 0:
        return px
    max_from_sz = max(0, 6 - sz_decimals)
    int_digits = max(1, int(math.floor(math.log10(px))) + 1)
    max_from_sig = max(0, 5 - int_digits)
    return round(px, min(max_from_sz, max_from_sig))


def _round_hl_size(qty: float, sz_decimals: int) -> float:
    # Floor to the venue lot size. Truncating (not rounding) guarantees the
    # resulting notional stays ≤ the signal's intended cap — rounding up could
    # breach MAX_ORDER_NOTIONAL or margin.
    if qty <= 0:
        return qty
    factor = 10**sz_decimals
    return math.floor(qty * factor) / factor


class HLEngine:
    def __init__(self) -> None:
        self._cfg = load_settings()

        # ── Crypto universe: env-driven, CSV, case-preserved, dedup ──────────
        raw_universe = os.environ.get("HL_UNIVERSE", DEFAULT_UNIVERSE)
        seen: set[str] = set()
        crypto_coins: list[str] = []
        for token in raw_universe.split(","):
            c = token.strip()
            if c and c not in seen:
                seen.add(c)
                crypto_coins.append(c)

        # ── HIP-3 equity perp universe ───────────────────────────────────────
        hip3_dexs_raw = os.environ.get("HIP3_DEXS", "").strip()
        self._hip3_dexs: list[str] = (
            [d.strip() for d in hip3_dexs_raw.split(",") if d.strip()]
            if hip3_dexs_raw
            else []
        )
        hip3_universe_raw = os.environ.get("HIP3_UNIVERSE", "").strip()
        hip3_coins_raw = (
            [t.strip() for t in hip3_universe_raw.split(",") if t.strip()]
            if hip3_universe_raw
            else []
        )
        hip3_coins: list[str] = []
        if self._hip3_dexs and hip3_coins_raw:
            dex = self._hip3_dexs[0]
            prefix = f"{dex}:"
            for name in hip3_coins_raw:
                coin = name if name.startswith(prefix) else f"{prefix}{name}"
                if coin not in seen:
                    seen.add(coin)
                    hip3_coins.append(coin)
        self._hip3_coins: list[str] = hip3_coins

        # ── Shadow coins (case-sensitive — dex prefix must match exactly) ────
        shadow_raw = os.environ.get("SHADOW_COINS", "").strip()
        self._shadow_coins: set[str] = (
            {t.strip() for t in shadow_raw.split(",") if t.strip()}
            if shadow_raw
            else set()
        )

        # ── Combined universe ────────────────────────────────────────────────
        coins = crypto_coins + hip3_coins
        if not coins:
            raise RuntimeError(
                f"HL_UNIVERSE + HIP3_UNIVERSE produced empty coin list "
                f"(HL_UNIVERSE={raw_universe!r}, HIP3_UNIVERSE={hip3_universe_raw!r})"
            )
        self._hl_coins: list[str] = coins
        self._coin_to_symbol: dict[str, str] = {c: f"{c}/USD" for c in coins}
        self._symbol_to_coin: dict[str, str] = {
            v: k for k, v in self._coin_to_symbol.items()
        }
        self._hl_symbols: list[str] = list(self._coin_to_symbol.values())

        # ── Per-coin leverage map ────────────────────────────────────────────
        self._default_leverage = 10
        hip3_leverage = int(os.environ.get("HIP3_LEVERAGE", "5"))
        leverage_map: dict[str, int] = {}
        for c in crypto_coins:
            leverage_map[c] = self._default_leverage
        for c in hip3_coins:
            leverage_map[c] = hip3_leverage

        self._hl = HyperliquidOrderManager(
            self._cfg,
            strategy_tag=STRATEGY_TAG,
            default_leverage=self._default_leverage,
            coins=self._hl_coins,
            is_cross=True,
            perp_dexs=self._hip3_dexs or None,
            leverage_map=leverage_map,
        )

        # Populate per-coin szDecimals from live meta() probes. Native coins
        # come from meta(); HIP-3 coins come from meta(dex=...).
        venue_sz_dec: dict[str, int] = {}

        # Native (HyperCore) meta
        meta = self._hl._info.meta()
        for asset in meta.get("universe", []):
            name = str(asset.get("name", ""))
            if name:
                try:
                    venue_sz_dec[name] = int(asset.get("szDecimals", 0))
                except TypeError, ValueError:
                    continue

        # HIP-3 DEX meta probes — names already include dex prefix (e.g. "xyz:TSLA").
        for dex in self._hip3_dexs:
            try:
                dex_meta = self._hl._info.meta(dex=dex)
            except Exception:
                dex_meta = {}
            for asset in dex_meta.get("universe", []):
                coin_name = str(asset.get("name", ""))
                if not coin_name:
                    continue
                try:
                    venue_sz_dec[coin_name] = int(asset.get("szDecimals", 0))
                except TypeError, ValueError:
                    continue

        missing = [c for c in self._hl_coins if c not in venue_sz_dec]
        if missing:
            raise RuntimeError(
                f"HL meta() missing szDecimals for requested coins: {missing}"
            )
        self._sz_decimals: dict[str, int] = {c: venue_sz_dec[c] for c in self._hl_coins}
        self._dust_caps: dict[str, float] = {
            c: 1.5 * (10**-dec) for c, dec in self._sz_decimals.items()
        }

        self._per_pair_notional = 250.0

        self._signals = SignalEngine(
            symbols=self._hl_symbols,
            strategy_tag=STRATEGY_TAG,
            allow_short=True,
            notional_per_trade=self._per_pair_notional,
        )

        # Inject _QTY_DECIMALS for HIP-3 coins from venue meta (dynamic, not hardcoded).
        from strategy.signals import _QTY_DECIMALS

        for c in hip3_coins:
            sym = self._coin_to_symbol[c]
            _QTY_DECIMALS[sym] = self._sz_decimals[c]

        # ── Per-coin z-thresholds ────────────────────────────────────────────
        # Crypto: BTC/ETH tight, PAXG ultra-tight, alts wide.
        _ALTS_WIDE = {"SOL", "AVAX", "LINK", "ZEC", "AAVE"}
        for coin in crypto_coins:
            sym = self._coin_to_symbol[coin]
            if coin in _ALTS_WIDE:
                self._signals.set_symbol_z(sym, -2.50, -0.75, +2.50, +0.75)
            elif coin == "PAXG":
                self._signals.set_symbol_z(sym, -0.25, -0.10, +0.25, +0.10)

        # HIP-3 equity perps: z-tiers assigned by asset class via screener logic.
        from screener_hip3 import classify, assign_z_tier

        for coin in hip3_coins:
            sym = self._coin_to_symbol[coin]
            cat = classify(coin)
            # Default RMSD estimate by category (refined after preseed with real data).
            _DEFAULT_RMSD = {"INDEX": 2.0, "FX": 0.3, "COMMODITY": 2.5, "ETF": 3.0}
            z = assign_z_tier(cat, _DEFAULT_RMSD.get(cat, 5.0))
            self._signals.set_symbol_z(sym, z[0], z[1], z[2], z[3])

        # Unified message queue — bars arrive from Alpaca LiveFeed with
        # symbol="BTC/USD", orderbooks arrive from HL pump with symbol rewritten
        # to "BTC/USD" so SignalEngine state matches.
        self._msg_q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._hl_raw_q: asyncio.Queue = asyncio.Queue(maxsize=2000)

        # Split universe: coins with Alpaca bar coverage vs native-bar coins.
        # All HIP-3 coins use native bars (no Alpaca coverage).
        self._alpaca_coins = [c for c in crypto_coins if c in ALPACA_BAR_COINS]
        self._native_bar_coins = [
            c for c in crypto_coins if c not in ALPACA_BAR_COINS
        ] + hip3_coins

        alpaca_symbols = [self._coin_to_symbol[c] for c in self._alpaca_coins]
        if alpaca_symbols:
            self._bars = LiveFeed(self._cfg, alpaca_symbols, self._msg_q)
        else:
            self._bars = None

        # Native bar state: per-coin OHLC tracker reset every NATIVE_BAR_INTERVAL_S.
        self._native_mid: dict[str, dict] = {
            c: {"o": 0.0, "h": 0.0, "l": 0.0, "c": 0.0, "n": 0}
            for c in self._native_bar_coins
        }
        self._book = HyperliquidFeed(
            self._hl_coins,
            self._hl_raw_q,
            wallet=self._cfg.hl_wallet_address or None,
            perp_dexs=self._hip3_dexs or None,
        )

        self._running = True

        self._pending_resting: dict[str, dict] = {}

        # ── Control plane (Phase 1: read-only) ──────────────────────────────
        self._control = ControlPlaneServer(
            signals=self._signals,
            engine_meta={
                "coins": self._hl_coins,
                "hip3_coins": self._hip3_coins,
                "shadow_coins": sorted(self._shadow_coins),
                "leverage_map": leverage_map,
                "notional": self._per_pair_notional,
                "tag": STRATEGY_TAG,
                "style": EXECUTION_STYLE,
                "mode": self._cfg.execution_mode.value,
            },
        )

    # ── Boot: seed in-memory state from on-chain positions ───────────────────
    async def _reconcile_startup(self) -> None:
        try:
            positions = await self._hl.get_positions()
        except Exception as exc:
            log.warning("hl_reconcile_failed", error=str(exc))
            return
        log.info("hl_reconcile_ok", pos_count=len(positions))
        self._signals.reconcile_hl_positions(
            positions,
            self._coin_to_symbol,
            self._dust_caps,
        )

    # ── Pump: translate HL feed symbols onto the SignalEngine state keys ─────
    async def _hl_obi_pump(self) -> None:
        while self._running:
            msg = await self._hl_raw_q.get()
            raw_coin = str(msg.get("symbol", ""))
            # Native coins are upper-cased ("BTC"); HIP-3 coins preserve dex
            # prefix case ("xyz:SP500"). Try exact match first, then upper-cased.
            coin = raw_coin if raw_coin in self._coin_to_symbol else raw_coin.upper()
            sym = self._coin_to_symbol.get(coin)
            if sym is None:
                continue
            msg["symbol"] = sym

            if msg["type"] == "orderbook" and coin in self._native_mid:
                bids = msg.get("bids", [])
                asks = msg.get("asks", [])
                if bids and asks:
                    mid = (bids[0][0] + asks[0][0]) / 2.0
                    st = self._native_mid[coin]
                    if st["n"] == 0:
                        st["o"] = st["h"] = st["l"] = st["c"] = mid
                    else:
                        st["h"] = max(st["h"], mid)
                        st["l"] = min(st["l"], mid)
                        st["c"] = mid
                    st["n"] += 1

            await self._msg_q.put(msg)

    # ── Native bar synthesis ────────────────────────────────────────────────
    async def _bar_synthesizer(self) -> None:
        """Emit 1-min OHLC bars for coins without Alpaca bar coverage."""
        while self._running:
            await asyncio.sleep(NATIVE_BAR_INTERVAL_S)
            now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            for coin in self._native_bar_coins:
                st = self._native_mid[coin]
                if st["n"] == 0:
                    continue
                sym = self._coin_to_symbol[coin]
                bar = {
                    "type": "bar",
                    "symbol": sym,
                    "open": st["o"],
                    "high": st["h"],
                    "low": st["l"],
                    "close": st["c"],
                    "volume": 0.0,
                    "timestamp": now_iso,
                    "recv_ns": time.perf_counter_ns(),
                }
                await self._msg_q.put(bar)
                st["o"] = st["h"] = st["l"] = st["c"] = st["c"]
                st["n"] = 0

    async def _preseed_native_bars(self) -> None:
        """Load 240 × 1-min candles from HL for ALL coins to warm trend + z-score buffers."""
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - (240 * 60 * 1000)
        info = self._hl._info
        for coin in self._hl_coins:
            sym = self._coin_to_symbol[coin]
            try:
                candles = info.candles_snapshot(coin, "1m", start_ms, end_ms)
            except Exception as exc:
                log.warning("hl_candle_preseed_failed", coin=coin, error=str(exc))
                continue
            st = self._signals._state.get(sym)
            if st is None:
                continue
            count = 0
            for c in candles:
                try:
                    close = float(c["c"])
                except KeyError, TypeError, ValueError:
                    continue
                st.price_buf.push(close)
                st.trend_buf.push(close)
                count += 1
            log.info("hl_candle_preseed", coin=coin, bars=count)

    # Per-class RMSD floor — overnight-flat HIP-3 perp data understates
    # real-session volatility. Floors ensure volatile names don't collapse
    # to the tightest z-tier just because they preseeded during Asian hours.
    _RMSD_FLOOR: dict[str, float] = {
        "EQUITY": 5.0,
        "COMMODITY": 2.5,
        "ETF": 3.0,
        "INDEX": 1.5,
        "FX": 0.5,
    }

    def _recalibrate_hip3_z(self) -> None:
        """Refine HIP-3 z-tiers using actual RMSD from pre-seeded price buffers."""
        import numpy as np
        from screener_hip3 import classify, assign_z_tier

        for coin in self._hip3_coins:
            sym = self._coin_to_symbol[coin]
            st = self._signals._state.get(sym)
            if st is None or st.price_buf._count < 30:
                continue
            prices = st.price_buf._active()
            mean = float(np.mean(prices))
            if mean <= 0:
                continue
            # Raw RMSD is from 1-minute bars; assign_z_tier thresholds are
            # calibrated for 4h RMSD.  Scale by sqrt(240) to normalise.
            rmsd_pct = float(np.std(prices) / mean) * 100 * math.sqrt(240)
            cat = classify(coin)
            rmsd_pct = max(rmsd_pct, self._RMSD_FLOOR.get(cat, 0))
            z = assign_z_tier(cat, rmsd_pct)
            self._signals.set_symbol_z(sym, z[0], z[1], z[2], z[3])
            log.info(
                "hip3_z_recalibrated",
                coin=coin,
                cat=cat,
                rmsd_pct=round(rmsd_pct, 3),
                z_entry=z[0],
                z_exit=z[1],
            )

    def _apply_z_overrides(self) -> None:
        """Apply per-symbol z-tier overrides from env vars.

        Format:  Z_OVERRIDE_<COIN>=z_entry,z_exit,z_short_entry,z_exit_short
        Example: Z_OVERRIDE_xyz:MSTR=-2.0,5.0,2.0,0.5

        Applied after recalibration so overrides are never clobbered.
        """
        prefix = "Z_OVERRIDE_"
        for key, val in os.environ.items():
            if not key.startswith(prefix):
                continue
            coin = key[len(prefix):]
            sym = self._coin_to_symbol.get(coin)
            if sym is None:
                continue
            try:
                parts = [float(x.strip()) for x in val.split(",")]
                if len(parts) != 4:
                    raise ValueError(f"expected 4 values, got {len(parts)}")
            except (ValueError, TypeError) as exc:
                log.warning("z_override_parse_error", coin=coin, raw=val, error=str(exc))
                continue
            self._signals.set_symbol_z(sym, parts[0], parts[1], parts[2], parts[3])
            log.info(
                "z_override_applied",
                coin=coin,
                symbol=sym,
                z_entry=parts[0],
                z_exit=parts[1],
                z_short_entry=parts[2],
                z_exit_short=parts[3],
            )

    # ── Sign-flip guard: live-state check against in-memory direction ────────
    async def _flip_guard_ok(self, sig: dict) -> bool:
        """
        Returns True if live HL state is consistent with our intent.

        On mismatch: reconcile from on-chain, rollback the optimistic memory
        write, and return False so the engine loop skips this bar. Next
        qualifying bar will re-evaluate against reconciled state.
        """
        sym = sig["symbol"]
        coin = self._symbol_to_coin[sym]

        # Shadow coins have no real positions to check against.
        if coin in self._shadow_coins:
            return True

        try:
            positions = await self._hl.get_positions()
        except Exception as exc:
            log.warning("hl_flip_guard_query_failed", symbol=sym, error=str(exc))
            return False

        live_szi = 0.0
        for p in positions:
            if str(p.get("coin", "")) == coin:
                live_szi = float(p.get("szi", 0) or 0)
                break

        st = self._signals._state[sym]
        mem_szi = st.open_qty(STRATEGY_TAG)
        pending_exit = st.pending_exits.get(STRATEGY_TAG, False)

        is_entry = not pending_exit  # exit signals set pending_exits=True

        # Sub-lot dust tolerance: HL's minimum lot is 10^-szDecimals. A stray
        # residual smaller than ~1.5 lots (rounding / partial-fill leftovers /
        # manual UI leftovers) is "effectively flat" for guard purposes. Without
        # this, one lot of dust (observed: −0.0001 ETH) deadlocks entries
        # indefinitely — each bar blocks, reconciles dust back in, and rolls
        # back. The 1.5× multiplier stays well below a genuine 2-lot position.
        sz_dec = self._sz_decimals.get(coin, 2)
        dust_cap = self._dust_caps.get(coin, 1.5 * (10**-sz_dec))
        live_flat = abs(live_szi) < dust_cap

        # Entry: memory should be pre-written with our intended signed qty;
        # live should be flat (the fill hasn't happened yet). Any live size
        # above dust means desync.
        # Exit: memory says non-zero; live should agree in sign. Live at-or-below
        # dust means the position was already closed elsewhere — skip.
        mismatch = False
        reason = ""
        if is_entry:
            if not live_flat:
                mismatch = True
                reason = "entry_but_live_nonzero"
        else:
            if live_flat:
                mismatch = True
                reason = "exit_but_live_flat"
            elif (live_szi * mem_szi) < 0:
                mismatch = True
                reason = "exit_side_sign_mismatch"

        if mismatch:
            log.warning(
                "hl_flip_guard_blocked",
                symbol=sym,
                reason=reason,
                mem_szi=mem_szi,
                live_szi=live_szi,
                pending_exit=pending_exit,
            )
            # Reconcile truth-on-chain → memory, then undo the optimistic write
            # for the blocked signal so evaluate() re-fires on the next bar.
            self._signals.reconcile_hl_positions(
                positions,
                self._coin_to_symbol,
                self._dust_caps,
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
        sym = sig["symbol"]
        coin = self._symbol_to_coin[sym]
        side = "buy" if sig["side"] == OrderSide.BUY else "sell"

        sz_dec = self._sz_decimals.get(coin, 2)
        raw_qty = sig["qty"]
        rounded_qty = _round_hl_size(raw_qty, sz_dec)
        if rounded_qty <= 0:
            log.warning(
                "hl_maker_qty_floored_to_zero",
                symbol=sym,
                coin=coin,
                raw_qty=raw_qty,
                sz_decimals=sz_dec,
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
        cid = f"{STRATEGY_TAG}_{coin}_{int(time.time())}"
        log.info(
            "hl_maker_intent",
            client_order_id=cid,
            cloid=cloid,
            symbol=sym,
            coin=coin,
            side=side,
            qty=rounded_qty,
            raw_qty=raw_qty,
            limit_px=rounded_px,
            raw_limit_px=raw_px,
            notional=sig.get("notional"),
        )

        hl_order = {
            "symbol": coin,
            "side": side,
            "qty": rounded_qty,
            "limit_px": rounded_px,
            "tif": "Alo",
            "reduce_only": False,
            "cloid": cloid,
        }
        result = await self._hl.submit_order(hl_order)
        log.info("hl_maker_result", client_order_id=cid, cloid=cloid, result=result)

        # Detect immediate rejection — HL returns status=ok with an `error`
        # field inside the per-order status when Alo can't rest (e.g. would
        # cross). Treat as a taker-style rejection so the caller rolls back.
        try:
            statuses = (
                (result or {}).get("response", {}).get("data", {}).get("statuses", [])
            )
            for s in statuses:
                if isinstance(s, dict) and s.get("error"):
                    log.warning(
                        "hl_maker_inner_rejection",
                        client_order_id=cid,
                        cloid=cloid,
                        error=s["error"],
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
            "cloid": cloid.lower(),
            "cid": cid,
            "side": side,
            "qty": rounded_qty,
            "last_px": rounded_px,
            "reprice_count": 0,
            "is_entry": is_entry,
            "submit_ts": int(time.time()),
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
        coin = self._symbol_to_coin[sym]
        cloid = pending["cloid"]
        age = round(time.time() - pending["submit_ts"], 2)
        log.info(
            "hl_maker_giveup",
            symbol=sym,
            coin=coin,
            reason=reason,
            cloid=cloid,
            age_s=age,
            reprice_count=pending.get("reprice_count", 0),
            is_entry=pending.get("is_entry"),
            is_hip3=":" in coin,
            side=pending.get("side"),
            last_px=pending.get("last_px"),
            cid=pending["cid"],
        )
        try:
            resp = await self._hl.cancel_by_cloid(coin, cloid)
        except Exception as exc:
            log.warning(
                "hl_maker_giveup_cancel_exception",
                symbol=sym,
                cloid=cloid,
                error=str(exc),
            )
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
        log.warning(
            "hl_maker_giveup_cancel_unconfirmed", symbol=sym, cloid=cloid, resp=resp
        )

    async def _reprice_pending(self, sym: str, new_px: float) -> None:
        """
        Cancel the current resting order and resubmit at `new_px` with a
        fresh cloid. Preserves the signal's `cid` (on_fill still routes to
        the right intent when any attempt fills).
        """
        pending = self._pending_resting.get(sym)
        if pending is None:
            return
        coin = self._symbol_to_coin[sym]
        old_cl = pending["cloid"]
        try:
            cresp = await self._hl.cancel_by_cloid(coin, old_cl)
        except Exception as exc:
            log.warning(
                "hl_maker_reprice_cancel_exception",
                symbol=sym,
                cloid=old_cl,
                error=str(exc),
            )
            return

        if not self._cancel_ok(cresp):
            # The order filled (or is filling) while we were trying to
            # reprice. Leave pending alone — userFills will clean up.
            log.info(
                "hl_maker_reprice_skipped_cancel_failed",
                symbol=sym,
                cloid=old_cl,
                resp=cresp,
            )
            return

        new_cloid = f"0x{secrets.randbits(128):032x}"
        log.info(
            "hl_maker_reprice",
            symbol=sym,
            coin=coin,
            side=pending["side"],
            qty=pending["qty"],
            old_px=pending["last_px"],
            new_px=new_px,
            old_cloid=old_cl,
            new_cloid=new_cloid,
            reprice_count=pending["reprice_count"] + 1,
            cid=pending["cid"],
        )

        hl_order = {
            "symbol": coin,
            "side": pending["side"],
            "qty": pending["qty"],
            "limit_px": new_px,
            "tif": "Alo",
            "reduce_only": False,
            "cloid": new_cloid,
        }
        result = await self._hl.submit_order(hl_order)

        # Inner-rejection check — if the new quote would cross, HL returns
        # status=ok with an inner error. In that case give up: the market
        # just ran through our level, so a taker is closer to the right
        # response than another reprice. Spike E will replace this rollback
        # with the escalation.
        inner_ok = True
        try:
            statuses = (
                (result or {}).get("response", {}).get("data", {}).get("statuses", [])
            )
            for s in statuses:
                if isinstance(s, dict) and s.get("error"):
                    inner_ok = False
                    log.warning(
                        "hl_maker_reprice_inner_rejection",
                        symbol=sym,
                        new_cloid=new_cloid,
                        error=s["error"],
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
        pending["cloid"] = new_cloid.lower()
        pending["last_px"] = new_px
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

                coin = self._symbol_to_coin.get(sym)
                is_hip3 = coin and ":" in coin
                max_life = (
                    HIP3_MAKER_MAX_LIFETIME_S if is_hip3 else MAKER_MAX_LIFETIME_S
                )
                max_rep = HIP3_MAKER_MAX_REPRICES if is_hip3 else MAKER_MAX_REPRICES

                age = now_ts - pending["submit_ts"]
                if age >= max_life:
                    await self._cancel_pending(sym, reason="lifetime_exceeded")
                    continue
                if pending["reprice_count"] >= max_rep:
                    await self._cancel_pending(sym, reason="max_reprices")
                    continue
                if coin is None:
                    continue
                st = self._signals._state.get(sym)
                if st is None:
                    continue
                sz_dec = self._sz_decimals.get(coin, 2)

                side = pending["side"]
                raw = st.best_bid if side == "buy" else st.best_ask
                if not math.isfinite(raw) or raw <= 0:
                    continue
                new_px = _round_hl_price(raw, sz_dec)
                cur_px = pending["last_px"]

                behind = (side == "buy" and new_px > cur_px) or (
                    side == "sell" and new_px < cur_px
                )
                if behind:
                    await self._reprice_pending(sym, new_px)

    # ── Signal → HL order translation + CID logging ──────────────────────────
    async def _submit(self, sig: dict) -> dict | None:
        sym = sig["symbol"]
        coin = self._symbol_to_coin.get(sym, "")

        # SYMBOL_CAPS enforcement — entries only (exits reduce exposure).
        st = self._signals._state.get(sym)
        is_exit = bool(st and st.pending_exits.get(STRATEGY_TAG, False))
        if not is_exit:
            from config.risk_params import SYMBOL_CAPS

            cap = SYMBOL_CAPS.get(sym)
            notional = sig.get("notional", 0)
            if cap is not None and notional > cap:
                log.warning(
                    "hl_order_blocked_symbol_cap",
                    symbol=sym,
                    notional=round(notional, 2),
                    cap=cap,
                )
                self._rollback_pending(sym, is_entry=True)
                return None

        # Shadow filter: log the order intent + fire synthetic on_fill, skip exchange.
        if coin in self._shadow_coins:
            side = "buy" if sig["side"] == OrderSide.BUY else "sell"
            cid = f"{STRATEGY_TAG}_{coin}_{int(time.time())}"
            log.info(
                "shadow_order",
                client_order_id=cid,
                symbol=sym,
                coin=coin,
                side=side,
                qty=sig["qty"],
                limit_px=sig["limit_px"],
                notional=sig.get("notional"),
            )
            self._signals.on_fill(
                client_order_id=cid,
                symbol=sym,
                qty=sig["qty"],
                side=side,
            )
            return {"status": "shadow_filled", "coin": coin}

        # Route by execution style. Env-gated so the taker contract is
        # untouched when EXECUTION_STYLE is unset or "taker".
        if EXECUTION_STYLE == "maker":
            is_hip3 = ":" in coin
            if is_hip3:
                st = self._signals._state.get(sym)
                is_exit = bool(st and st.pending_exits.get(STRATEGY_TAG, False))
                # HIP-3 exits always use IOC taker: mean-reversion exits fire
                # while price snaps back to the mean — a resting Alo sits on
                # the wrong side of the move and the only fills are adverse.
                if is_exit:
                    log.info("hip3_exit_taker", symbol=sym, coin=coin)
                    return await self._submit_taker(sig)
                # HIP-3 entry escalation: extreme z-scores bypass maker.
                z_abs = (
                    abs(sig.get("z", 0))
                    if sig.get("z")
                    else (abs(st.price_buf.zscore(sig["limit_px"]) or 0) if st else 0)
                )
                if z_abs >= HIP3_TAKER_ESCALATION_Z:
                    log.info(
                        "hip3_taker_escalation",
                        symbol=sym,
                        coin=coin,
                        z=round(z_abs, 3),
                        threshold=HIP3_TAKER_ESCALATION_Z,
                    )
                    return await self._submit_taker(sig)
            return await self._submit_maker(sig)
        return await self._submit_taker(sig)

    async def _submit_taker(self, sig: dict) -> dict | None:
        """IOC cross-spread taker path. Used as default when EXECUTION_STYLE != maker,
        and as escalation target for HIP-3 extreme z-score signals."""
        sym = sig["symbol"]
        coin = self._symbol_to_coin[sym]
        side = "buy" if sig["side"] == OrderSide.BUY else "sell"

        raw_px = sig["limit_px"]
        sz_dec = self._sz_decimals.get(coin, 2)
        rounded_px = _round_hl_price(raw_px, sz_dec)
        raw_qty = sig["qty"]
        rounded_qty = _round_hl_size(raw_qty, sz_dec)

        if rounded_qty <= 0:
            log.warning(
                "hl_order_qty_floored_to_zero",
                symbol=sym,
                coin=coin,
                raw_qty=raw_qty,
                sz_decimals=sz_dec,
            )
            return None

        cid = f"{STRATEGY_TAG}_{coin}_{int(time.time())}"
        log.info(
            "hl_order_intent",
            client_order_id=cid,
            symbol=sym,
            coin=coin,
            side=side,
            qty=rounded_qty,
            raw_qty=raw_qty,
            limit_px=rounded_px,
            raw_limit_px=raw_px,
            notional=sig.get("notional"),
            tif="Ioc",
        )

        st = self._signals._state.get(sym)
        is_exit = bool(st and st.pending_exits.get(STRATEGY_TAG, False))
        z_now = sig.get("z") or (st.price_buf.zscore(raw_px) if st else None)

        hl_order = {
            "symbol": coin,
            "side": side,
            "qty": rounded_qty,
            "limit_px": rounded_px,
            "tif": "Ioc",
            "reduce_only": False,
        }
        t0 = time.perf_counter_ns()
        result = await self._hl.submit_order(hl_order)
        lat_ms = (time.perf_counter_ns() - t0) / 1e6
        log.info("hl_order_result", client_order_id=cid, result=result)

        try:
            statuses = (
                (result or {}).get("response", {}).get("data", {}).get("statuses", [])
            )
            for s in statuses:
                if isinstance(s, dict) and s.get("error"):
                    log.warning(
                        "hl_order_inner_rejection",
                        client_order_id=cid,
                        error=s["error"],
                        sent_px=rounded_px,
                        raw_px=raw_px,
                        sent_qty=rounded_qty,
                        raw_qty=raw_qty,
                    )
                    return None

            for s in statuses:
                if isinstance(s, dict) and "filled" in s:
                    try:
                        fill_px = float(s["filled"].get("avgPx", 0) or 0)
                        filled_sz = float(s["filled"].get("totalSz", 0))
                    except TypeError, ValueError:
                        fill_px, filled_sz = 0.0, 0.0
                    if filled_sz > 0:
                        slip_bps = (
                            round(abs(fill_px - rounded_px) / rounded_px * 10000, 2)
                            if rounded_px
                            else 0.0
                        )
                        log.info(
                            "hl_taker_fill",
                            client_order_id=cid,
                            symbol=sym,
                            coin=coin,
                            side=side,
                            is_exit=is_exit,
                            is_hip3=":" in coin,
                            sent_px=rounded_px,
                            fill_px=fill_px,
                            slippage_bps=slip_bps,
                            filled_sz=filled_sz,
                            sent_qty=rounded_qty,
                            z=round(z_now, 3) if z_now else None,
                            latency_ms=round(lat_ms, 1),
                        )
                        self._signals.on_fill(
                            client_order_id=cid,
                            symbol=sym,
                            qty=filled_sz,
                            side=side,
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
        sym = msg.get("symbol")
        cloid = msg.get("cloid")
        if not (sym and cloid):
            return
        cloid_lc = str(cloid).lower()

        pending = self._pending_resting.get(sym)
        if not pending or pending["cloid"] != cloid_lc:
            return

        try:
            filled_sz = float(msg["sz"])
        except KeyError, TypeError, ValueError:
            filled_sz = 0.0
        if filled_sz <= 0:
            return

        cumulative = pending.get("filled_qty", 0.0) + filled_sz
        remaining = pending["qty"] - cumulative
        # Floor at half a lot — anything below is dust that HL can't match,
        # so treat as terminal.
        coin = self._symbol_to_coin.get(sym, "")
        sz_dec = self._sz_decimals.get(coin, 2)
        dust_half = 0.5 * (10**-sz_dec)
        terminal = remaining <= dust_half

        fill_px = float(msg.get("px", 0) or 0)
        sent_px = pending.get("last_px", 0)
        slip_bps = (
            round(abs(fill_px - sent_px) / sent_px * 10000, 2) if sent_px else 0.0
        )
        age_s = round(time.time() - pending["submit_ts"], 2)

        log.info(
            "hl_maker_fill_matched",
            symbol=sym,
            coin=coin,
            cloid=cloid_lc,
            client_order_id=pending["cid"],
            fill_sz=filled_sz,
            fill_px=fill_px,
            sent_px=sent_px,
            slippage_bps=slip_bps,
            crossed=msg.get("crossed"),
            is_entry=pending.get("is_entry"),
            is_hip3=":" in coin,
            side=pending.get("side"),
            age_s=age_s,
            reprice_count=pending.get("reprice_count", 0),
            cumulative=cumulative,
            remaining=remaining,
            terminal=terminal,
        )
        # signals.on_fill overwrites (not accumulates) and clears pending_exits
        # on the first call. Fire it only once per order, on the terminal fill,
        # with the cumulative qty — otherwise multi-chunk entries leave mem at
        # the last chunk size (→ under-sized exit → dust residual), and
        # multi-chunk exits resurrect a phantom position after the close.
        if terminal:
            self._signals.on_fill(
                client_order_id=pending["cid"],
                symbol=sym,
                qty=cumulative,
                side=pending["side"],
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
            symbols=self._hl_symbols,
            coins=self._hl_coins,
            hip3_coins=self._hip3_coins,
            shadow_coins=sorted(self._shadow_coins),
            tag=STRATEGY_TAG,
            leverage=self._default_leverage,
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
            f"           HIP-3 coins: {self._hip3_coins or '(none)'}\n"
            f"           Shadow coins: {sorted(self._shadow_coins) or '(none)'}\n"
            f"           Alpaca bars: {self._alpaca_coins or '(none)'}\n"
            f"           Native bars: {self._native_bar_coins or '(none)'}\n"
            f"           Logs → logs/hl_engine.jsonl\n"
            f"           Ctrl-C to stop.\n"
        )

        await self._reconcile_startup()
        await self._preseed_native_bars()
        if self._hip3_coins:
            self._recalibrate_hip3_z()
        self._apply_z_overrides()

        log.info(
            "hl_bar_sources",
            alpaca_coins=self._alpaca_coins,
            native_bar_coins=self._native_bar_coins,
        )

        async with asyncio.TaskGroup() as tg:
            if self._bars is not None:
                tg.create_task(self._bars.run(), name="alpaca_bars")
            tg.create_task(self._book.run(), name="hl_orderbook")
            tg.create_task(self._hl_obi_pump(), name="hl_obi_pump")
            tg.create_task(self._strategy_loop(), name="strategy")
            if self._native_bar_coins:
                tg.create_task(self._bar_synthesizer(), name="native_bars")
            if EXECUTION_STYLE == "maker":
                tg.create_task(self._maker_watchdog(), name="maker_watchdog")
            tg.create_task(self._control.serve(), name="control_plane")

    def stop(self) -> None:
        log.info("hl_engine_shutdown")
        self._running = False
        self._control.stop()
        self._book.stop()


async def main() -> None:
    engine = HLEngine()
    loop = asyncio.get_running_loop()
    for s in (signal_lib.SIGINT, signal_lib.SIGTERM):
        loop.add_signal_handler(s, engine.stop)
    await engine.run()


if __name__ == "__main__":
    asyncio.run(main())
