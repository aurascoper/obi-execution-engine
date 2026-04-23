#!/usr/bin/env python3
"""
hl_pairs.py — Pairs / statistical arbitrage tier for Hyperliquid.

Trades dollar-neutral spreads on cointegrated HIP-3 equity perps vs native
crypto perps. The math is the same rolling-z-of-residual used by the
z-revert tier, applied to a synthetic spread series instead of a single
price. The whitelist is produced by `hl_pairs_discover.py`.

Pair model:
    spread_t = log(px_A_t) − β · log(px_B_t) − α
    z_t      = (spread_t − μ_W) / σ_W      over W=240 1-min bars (4h)

    Long  spread  (buy A, sell B)   when z ≤ −Z_ENTRY
    Short spread  (sell A, buy B)   when z ≥ +Z_ENTRY
    Exit  flat                      when |z| ≤ Z_EXIT
    Stop  regime-break              when |z| ≥ Z_STOP
                                OR  when pair MTM PnL ≤ −DOLLAR_STOP

Three design choices baked in (per iteration with operator):

    1. Discovered universe — whitelist loaded from config/pairs_whitelist.json;
       never hardcoded. Fails fast if the file is missing or stale (>48h).

    2. Sequential leg execution — leg A (less-liquid, HIP-3 side) submitted
       first. Its actual fill size drives leg B sizing so we never leg out.
       If leg A zeroes, we abort. If leg A partials, leg B is shrunk to match.

    3. Coordinated netting — before opening, query live HL position on both
       legs. If either is non-flat (another tier like hl_z owns it), defer
       the pair until both sides clear. Re-checked every poll tick.

Additional runtime guards:
    * MAX_OPEN_PAIRS         — cap simultaneous open spreads (default 3)
    * PER_PAIR_DOLLAR_STOP   — force-exit on pair MTM PnL ≤ −$DOLLAR_STOP
    * FUNDING_COST_GATE      — skip entries if hourly funding on either leg
                               exceeds FUNDING_MAX_HOURLY (0.05% = ~44% APR,
                               makes spread-revert uneconomic)
    * REENTRY_LOCKOUT_S      — 30min freeze after a stop-out, per pair

Dry-run: default ON (DRY_RUN=1). Signals and would-be orders hit
logs/hl_pairs.jsonl but no submissions. Set DRY_RUN=0 to go live.

Runbook:
    source .env
    venv/bin/python hl_pairs_discover.py         # build whitelist (nightly)
    venv/bin/python hl_pairs.py                  # dry-run (default)
    DRY_RUN=0 venv/bin/python hl_pairs.py        # live
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import structlog

from config.settings import load as load_settings
from config.risk_params import (
    HEDGENET_DRIFT_LIMIT,
    HEDGENET_INTERVAL,
    HEDGENET_WEIGHT_DIR,
    KELLY_CAP,
    KELLY_K,
    KELLY_PAIRS,
    KELLY_SIGMA_FLOOR,
    PAIRS_HEDGE_MODE,
    PAIRS_SHADOW,
)
from execution.hl_manager import HyperliquidOrderManager
from strategy.hedge_shadow import HedgeShadow, auto_fallback
from strategy.sizing import kelly_fraction
from util.platform_compat import install_shutdown_handlers


def _round_hl_price(px: float, sz_decimals: int) -> float:
    if px <= 0:
        return px
    max_from_sz = max(0, 6 - sz_decimals)
    int_digits = max(1, int(math.floor(math.log10(px))) + 1)
    max_from_sig = max(0, 5 - int_digits)
    return round(px, min(max_from_sz, max_from_sig))


def _round_hl_size(qty: float, sz_decimals: int) -> float:
    if qty <= 0:
        return qty
    factor = 10**sz_decimals
    return math.floor(qty * factor) / factor


ROOT = Path(__file__).resolve().parent
WHITELIST_PATH = ROOT / "config" / "pairs_whitelist.json"
MAX_WHITELIST_AGE_S = 48 * 3600  # refuse stale whitelists — they mask regime shifts

# Kill-switch: operator creates this file to halt NEW entries (existing positions
# keep running through exit/stop logic). `touch logs/pairs_halt.flag` to halt,
# `rm logs/pairs_halt.flag` to resume. Checked each tick — no restart needed.
HALT_FLAG_PATH = ROOT / "logs" / "pairs_halt.flag"

# Comma-separated DEX prefixes to drop from the whitelist at load-time. Intended
# for DEXs the unified wallet has not funded (e.g. cash, flx, vntl, hyna, km).
# Pairs where EITHER leg starts with a blacklisted prefix are filtered out.
PAIRS_DEX_BLACKLIST = {
    s.strip() for s in os.environ.get("PAIRS_DEX_BLACKLIST", "").split(",") if s.strip()
}

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
        file=open("logs/hl_pairs.jsonl", "a", buffering=1)
    ),
)
log = structlog.get_logger("hl_pairs")

STRATEGY_TAG = "hl_pairs"

# ── Strategy parameters ───────────────────────────────────────────────────────
WINDOW = 240
WARMUP_BARS = 240
REBETA_INTERVAL_S = 3600
Z_ENTRY = 2.0
Z_EXIT = 0.5
Z_STOP = 4.0
POLL_INTERVAL_S = 60
PAIR_NOTIONAL = float(os.environ.get("PAIR_NOTIONAL", "10"))
MAX_OPEN_PAIRS = int(os.environ.get("HL_PAIRS_MAX_OPEN", "3"))
DOLLAR_STOP = float(os.environ.get("HL_PAIRS_DOLLAR_STOP", "15"))
FUNDING_MAX_HOURLY = float(os.environ.get("HL_PAIRS_FUNDING_MAX", "0.0005"))  # 0.05%/hr
REENTRY_LOCKOUT_S = int(os.environ.get("HL_PAIRS_REENTRY_LOCKOUT_S", "1800"))
POSITION_DUST = 1e-6  # below this, treat as flat
LEG_AGGRESSION_BPS = 30  # IOC slippage budget per leg
DRY_RUN = os.environ.get("DRY_RUN", "1") == "1"

BUILDER_DEXS = ["xyz", "para", "hyna", "flx", "vntl", "km", "cash"]


# ── Whitelist loader ──────────────────────────────────────────────────────────
def _load_whitelist(path: Path = WHITELIST_PATH) -> list[dict]:
    """Load the discovery output. Fails loud on missing/stale files."""
    if not path.exists():
        raise FileNotFoundError(
            f"Whitelist not found: {path}. Run `venv/bin/python hl_pairs_discover.py`."
        )
    data = json.loads(path.read_text())
    gen = data.get("generated_at")
    if gen:
        try:
            ts = datetime.strptime(gen, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            if age > MAX_WHITELIST_AGE_S:
                raise RuntimeError(
                    f"Whitelist is {age / 3600:.1f}h old (>48h). "
                    f"Re-run hl_pairs_discover.py before trading."
                )
        except ValueError:
            log.warning("whitelist_bad_timestamp", generated_at=gen)
    pairs = data.get("pairs", [])
    if not pairs:
        raise RuntimeError(f"Whitelist {path} contains no pairs.")

    if PAIRS_DEX_BLACKLIST:

        def _dex(sym: str) -> str:
            return sym.split(":", 1)[0] if ":" in sym else ""

        before = len(pairs)
        pairs = [
            p
            for p in pairs
            if _dex(p["leg_a"]) not in PAIRS_DEX_BLACKLIST
            and _dex(p["leg_b"]) not in PAIRS_DEX_BLACKLIST
        ]
        dropped = before - len(pairs)
        if dropped:
            log.info(
                "whitelist_dex_blacklist_applied",
                blacklist=sorted(PAIRS_DEX_BLACKLIST),
                dropped=dropped,
                remaining=len(pairs),
            )
        if not pairs:
            raise RuntimeError(
                f"Whitelist empty after DEX blacklist {PAIRS_DEX_BLACKLIST}."
            )
    return pairs


# ── Pair runtime state ────────────────────────────────────────────────────────
@dataclass
class Pair:
    leg_a: str
    leg_b: str
    beta: float
    alpha: float = 0.0
    half_life_min: float = 0.0  # from whitelist; θ = ln(2)/hl (bars=min)
    prices_a: deque = field(default_factory=lambda: deque(maxlen=WINDOW + 1))
    prices_b: deque = field(default_factory=lambda: deque(maxlen=WINDOW + 1))
    last_refit_ts: float = 0.0
    position: int = 0  # +1 long spread, -1 short spread, 0 flat
    entry_z: float | None = None
    entry_ts: float = 0.0
    # Realized leg state — needed for MTM and unwind sizing.
    qty_a: float = 0.0
    qty_b: float = 0.0
    entry_px_a: float = 0.0
    entry_px_b: float = 0.0
    last_stop_ts: float = 0.0  # for re-entry lockout
    # Phase 6d: β_ols is tracked in `beta`; β_nn and the execution-selected β
    # live here so `pair_tick` can log both, and the OU-based exit math can
    # switch between them when PAIRS_HEDGE_MODE=nn (with auto-fallback).
    beta_nn: float | None = None
    beta_source: str = "ols"  # ols | nn | fallback_nan | fallback_drift
    hedge_shadow: HedgeShadow | None = None
    # Phase 6e: rolling history of β_ols refits for σ-based drift guard.
    # Bounded to 32 samples (~32 hours at REBETA_INTERVAL_S=3600) — enough to
    # capture a day of refit variation without dragging in regime-shifted values.
    beta_ols_history: deque = field(default_factory=lambda: deque(maxlen=32))

    @property
    def name(self) -> str:
        return f"{self.leg_a}|{self.leg_b}"

    def push(self, px_a: float, px_b: float) -> None:
        if px_a > 0 and px_b > 0:
            self.prices_a.append(px_a)
            self.prices_b.append(px_b)
            if self.hedge_shadow is not None:
                self.hedge_shadow.push(px_a, px_b)

    @property
    def beta_exec(self) -> float:
        """β actually used by spread_z / sizing. Driven by PAIRS_HEDGE_MODE
        with `auto_fallback` as a sanity layer. `beta_source` records which
        leg won so `pair_tick` can surface it."""
        if PAIRS_HEDGE_MODE == "nn" and self.beta_nn is not None:
            # Prefer σ-based drift cap once we have ≥4 OLS refits to estimate σ.
            sigma: float | None = None
            if len(self.beta_ols_history) >= 4:
                sigma = float(np.std(list(self.beta_ols_history), ddof=0))
            chosen, src = auto_fallback(
                self.beta,
                self.beta_nn,
                drift_limit=HEDGENET_DRIFT_LIMIT,
                beta_ols_sigma=sigma,
            )
            self.beta_source = src
            return chosen
        self.beta_source = "ols"
        return self.beta

    def is_warm(self) -> bool:
        return len(self.prices_a) >= WARMUP_BARS and len(self.prices_b) >= WARMUP_BARS

    def refit_beta(self) -> None:
        """Rolling OLS refit on the 240-bar window.

        Note: this replaces the discovery-time β/α with window-local values.
        Discovery uses 48h; runtime uses 4h — so mid-session regime shifts
        are captured but the discovery-time cointegration evidence isn't
        re-validated. If refit β diverges from discovery β by >50%, log a
        warning — operator should consider re-running discovery."""
        if not self.is_warm():
            return
        la = np.log(np.asarray(self.prices_a, dtype=float))
        lb = np.log(np.asarray(self.prices_b, dtype=float))
        x_mean = float(np.mean(lb))
        y_mean = float(np.mean(la))
        sx2 = float(np.sum((lb - x_mean) ** 2))
        if sx2 <= 1e-12:
            return
        beta_new = float(np.sum((lb - x_mean) * (la - y_mean)) / sx2)
        if not (0.1 <= abs(beta_new) <= 5.0):
            log.warning("pair_refit_out_of_range", pair=self.name, beta_new=beta_new)
            return
        if self.beta and abs(beta_new - self.beta) / abs(self.beta) > 0.5:
            # Reject — whitelist β came from 52d discovery; a >50% swing on a
            # 4h window (or a preseed-polluted one) is more likely noise than
            # a regime shift. Sign flips especially invert the hedge.
            log.warning(
                "pair_beta_drift_rejected",
                pair=self.name,
                beta_old=self.beta,
                beta_new=beta_new,
                sign_flip=(self.beta * beta_new < 0),
            )
            self.last_refit_ts = time.time()
            return
        self.beta = beta_new
        self.alpha = y_mean - beta_new * x_mean
        self.last_refit_ts = time.time()
        # Phase 6e: keep a rolling history of refits so auto_fallback() can use
        # a σ-based drift guard rather than the weaker relative-multiple one.
        self.beta_ols_history.append(beta_new)

    def spread_z(self) -> float | None:
        if not self.is_warm():
            return None
        la = np.log(np.asarray(self.prices_a, dtype=float))
        lb = np.log(np.asarray(self.prices_b, dtype=float))
        # `beta_exec` is `beta` unless PAIRS_HEDGE_MODE=nn and the NN β passed
        # the drift/NaN guards. Alpha is always the OLS intercept — we recenter
        # the NN spread using its own window mean so μ stays zero in-sample.
        b = self.beta_exec
        spread = la - b * lb
        mu = float(np.mean(spread))
        sigma = float(np.std(spread, ddof=0))
        if sigma <= 1e-12:
            return None
        return (spread[-1] - mu) / sigma

    def spread_sigma(self) -> float | None:
        """Rolling residual σ of the log-spread. None if window too short."""
        if not self.is_warm():
            return None
        la = np.log(np.asarray(self.prices_a, dtype=float))
        lb = np.log(np.asarray(self.prices_b, dtype=float))
        spread = la - self.beta_exec * lb
        sigma = float(np.std(spread, ddof=0))
        return sigma if sigma > 1e-12 else None

    def mtm_pnl(self, px_a: float, px_b: float) -> float:
        """Dollar MTM PnL of the open spread position. Zero if flat."""
        if self.position == 0 or self.entry_px_a <= 0 or self.entry_px_b <= 0:
            return 0.0
        pnl_a = (px_a - self.entry_px_a) * self.qty_a
        pnl_b = (px_b - self.entry_px_b) * self.qty_b
        return pnl_a + pnl_b


# ── Engine ────────────────────────────────────────────────────────────────────
class PairsEngine:
    def __init__(self, whitelist: list[dict]):
        cfg = load_settings()
        self._cfg = cfg
        self._wallet = cfg.hl_wallet_address

        self._pairs: list[Pair] = []
        shadow_enabled = PAIRS_SHADOW or PAIRS_HEDGE_MODE == "nn"
        for row in whitelist:
            pair = Pair(
                leg_a=row["leg_a"],
                leg_b=row["leg_b"],
                beta=float(row.get("beta", 1.0)),
                alpha=float(row.get("alpha", 0.0)),
                half_life_min=float(row.get("half_life_min", 0.0)),
            )
            if shadow_enabled:
                pair.hedge_shadow = HedgeShadow(
                    pair.leg_a,
                    pair.leg_b,
                    weights_dir=HEDGENET_WEIGHT_DIR,
                    interval=HEDGENET_INTERVAL,
                )
            self._pairs.append(pair)

        coins = sorted({p.leg_a for p in self._pairs} | {p.leg_b for p in self._pairs})
        used_dexs = sorted(
            {c.split(":")[0] for c in coins if ":" in c} & set(BUILDER_DEXS)
        )

        self._om = HyperliquidOrderManager(
            cfg=cfg,
            strategy_tag=STRATEGY_TAG,
            default_leverage=3,
            coins=coins,
            perp_dexs=used_dexs,
        )
        self._info = self._om._info
        self._active_dexs_list = used_dexs
        self._stop = asyncio.Event()
        self._tick_count = 0

        # Per-coin szDecimals for HL tick-grid rounding. Native coins from
        # meta(); HIP-3 coins from meta(dex=...). Required before submitting
        # any real order — HL rejects prices that aren't on the tick grid.
        venue_sz_dec: dict[str, int] = {}
        try:
            native_meta = self._info.meta()
            for asset in native_meta.get("universe", []):
                name = str(asset.get("name", ""))
                if name:
                    try:
                        venue_sz_dec[name] = int(asset.get("szDecimals", 0))
                    except (TypeError, ValueError):
                        continue
        except Exception as exc:
            log.warning("pairs_meta_native_failed", error=str(exc))
        for dex in used_dexs:
            try:
                dex_meta = self._info.meta(dex=dex)
            except Exception as exc:
                log.warning("pairs_meta_dex_failed", dex=dex, error=str(exc))
                continue
            for asset in dex_meta.get("universe", []):
                coin_name = str(asset.get("name", ""))
                if not coin_name:
                    continue
                try:
                    venue_sz_dec[coin_name] = int(asset.get("szDecimals", 0))
                except (TypeError, ValueError):
                    continue
        missing = [c for c in coins if c not in venue_sz_dec]
        if missing:
            raise RuntimeError(f"pairs meta() missing szDecimals for coins: {missing}")
        self._sz_dec: dict[str, int] = {c: venue_sz_dec[c] for c in coins}

        log.info(
            "pairs_engine_initialized",
            whitelist_size=len(self._pairs),
            pairs=[p.name for p in self._pairs],
            dexs=used_dexs,
            dry_run=DRY_RUN,
            notional_per_leg=PAIR_NOTIONAL,
            max_open=MAX_OPEN_PAIRS,
            dollar_stop=DOLLAR_STOP,
        )

    async def _preseed_prices(self) -> None:
        """Hydrate each Pair.prices_a/b from HL candles_snapshot so WARMUP_BARS
        is satisfied on startup. Mirrors hl_engine._preseed_native_bars.

        Per-coin 1m candles are fetched once, then each pair is populated from
        the timestamp intersection of its two legs — keeps prices_a[i] and
        prices_b[i] aligned to the same minute, which is what push() assumes.
        """
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - ((WARMUP_BARS + 10) * 60 * 1000)
        coins = sorted({p.leg_a for p in self._pairs} | {p.leg_b for p in self._pairs})
        coin_bars: dict[str, dict[int, float]] = {}
        for coin in coins:
            try:
                candles = await asyncio.to_thread(
                    self._info.candles_snapshot, coin, "1m", start_ms, end_ms
                )
            except Exception as exc:
                log.warning("pair_preseed_failed", coin=coin, error=str(exc))
                continue
            bars: dict[int, float] = {}
            for c in candles or []:
                try:
                    ts = int(c["t"])
                    close = float(c["c"])
                except (KeyError, TypeError, ValueError):
                    continue
                bars[ts] = close
            coin_bars[coin] = bars
            log.info("pair_preseed_leg", coin=coin, bars=len(bars))
        now = time.time()
        for pair in self._pairs:
            a = coin_bars.get(pair.leg_a, {})
            b = coin_bars.get(pair.leg_b, {})
            common_ts = sorted(set(a) & set(b))[-(WINDOW + 1) :]
            for ts in common_ts:
                pair.push(a[ts], b[ts])
            # Suppress the first auto-refit (`last_refit_ts == 0.0` gate in
            # run()). A 4h OLS refit on preseed bars is too noisy — it can
            # sign-flip β vs the 52d discovery fit. Keep the whitelist β
            # until the hourly schedule fires 1h from now, by which time the
            # window has real live data mixed in.
            if len(common_ts) >= WARMUP_BARS:
                pair.last_refit_ts = now
            log.info(
                "pair_preseed_hydrated",
                pair=pair.name,
                bars=len(common_ts),
                warm=pair.is_warm(),
            )

    # ── Market data ──────────────────────────────────────────────────────────
    async def _fetch_mids(self) -> dict[str, float]:
        mids: dict[str, float] = {}
        try:
            native = await asyncio.to_thread(self._info.all_mids)
            for k, v in (native or {}).items():
                try:
                    mids[k] = float(v)
                except (TypeError, ValueError):
                    continue
        except Exception as exc:
            log.warning("mids_native_failed", error=str(exc))

        for dex in self._active_dexs_list:
            try:
                dex_mids = await asyncio.to_thread(self._info.all_mids, dex)
            except TypeError:
                continue
            except Exception as exc:
                log.warning("mids_dex_failed", dex=dex, error=str(exc))
                continue
            for k, v in (dex_mids or {}).items():
                # SDK already returns HIP-3 keys in "dex:NAME" form — do not
                # re-prefix or we end up with "xyz:xyz:MSTR" and no mid.
                key = k if ":" in k else f"{dex}:{k}"
                try:
                    mids[key] = float(v)
                except (TypeError, ValueError):
                    continue
        return mids

    async def _fetch_funding(self) -> dict[str, float]:
        """Return {coin: hourly_funding_rate}. Used to gate entries."""
        funding: dict[str, float] = {}
        # Native — marginSummary doesn't give funding; use meta_and_asset_ctxs.
        try:
            meta = await asyncio.to_thread(self._info.meta_and_asset_ctxs)
            universe = meta[0].get("universe", []) if meta else []
            ctxs = meta[1] if meta and len(meta) > 1 else []
            for u, ctx in zip(universe, ctxs):
                name = u.get("name")
                if name:
                    try:
                        funding[name] = float(ctx.get("funding", 0))
                    except (TypeError, ValueError):
                        pass
        except Exception as exc:
            log.warning("funding_native_failed", error=str(exc))

        for dex in self._active_dexs_list:
            try:
                meta = await asyncio.to_thread(
                    self._info.post,
                    "/info",
                    {"type": "metaAndAssetCtxs", "dex": dex},
                )
                universe = meta[0].get("universe", []) if meta else []
                ctxs = meta[1] if meta and len(meta) > 1 else []
                for u, ctx in zip(universe, ctxs):
                    name = u.get("name")
                    if name:
                        key = name if ":" in name else f"{dex}:{name}"
                        try:
                            funding[key] = float(ctx.get("funding", 0))
                        except (TypeError, ValueError):
                            pass
            except Exception as exc:
                log.warning("funding_dex_failed", dex=dex, error=str(exc))
        return funding

    async def _position_on(self, coin: str) -> float:
        """Live HL position size on this coin (signed). Zero if flat."""
        try:
            if ":" in coin:
                dex, name = coin.split(":", 1)
                state = await asyncio.to_thread(
                    self._info.post,
                    "/info",
                    {"type": "clearinghouseState", "user": self._wallet, "dex": dex},
                )
            else:
                state = await asyncio.to_thread(self._info.user_state, self._wallet)
                name = coin
            for pos in (state or {}).get("assetPositions", []):
                p = pos.get("position", {})
                if p.get("coin") == name:
                    return float(p.get("szi", 0))
        except Exception as exc:
            log.warning("position_query_failed", coin=coin, error=str(exc))
        return 0.0

    # ── Execution primitives ─────────────────────────────────────────────────
    @staticmethod
    def _extract_fill(resp: dict | None) -> tuple[float, float, str | None]:
        """Pull (filled_qty, avg_px, error) from an HL exchange.order response.

        SDK shape on success:
          {"status":"ok","response":{"type":"order","data":{"statuses":[
              {"filled":{"totalSz":"0.123","avgPx":"42.10","oid":12345}}]}}}
        On per-order rejection the status entry carries an "error" key instead.
        DRY_RUN and SHADOW produce synthetic responses we treat as full-fill."""
        if not resp:
            return 0.0, 0.0, "no_response"
        if resp.get("mode") == "SHADOW" or resp.get("status") == "shadow_filled":
            return float(resp.get("qty", 0)), float(resp.get("limit_px", 0)), None
        try:
            statuses = resp.get("response", {}).get("data", {}).get("statuses", [])
            if not statuses:
                return 0.0, 0.0, "no_statuses"
            s0 = statuses[0]
            if "error" in s0:
                return 0.0, 0.0, str(s0["error"])
            filled = s0.get("filled", {})
            sz = float(filled.get("totalSz", 0))
            px = float(filled.get("avgPx", 0))
            return sz, px, None
        except (KeyError, TypeError, ValueError) as exc:
            return 0.0, 0.0, f"parse_error: {exc}"

    async def _submit_leg(
        self, coin: str, side: str, qty: float, mid: float
    ) -> tuple[float, float, str | None]:
        """Submit a single IOC leg at mid ± aggression. Returns (fill_qty, fill_px, err)."""
        if qty <= 0 or mid <= 0:
            return 0.0, 0.0, "zero_size_or_price"
        slip = LEG_AGGRESSION_BPS / 1e4
        limit_px = mid * (1 + slip) if side == "buy" else mid * (1 - slip)
        sz_dec = self._sz_dec.get(coin, 0)
        limit_px = _round_hl_price(limit_px, sz_dec)
        qty = _round_hl_size(qty, sz_dec)
        if qty <= 0:
            return 0.0, 0.0, "qty_rounded_to_zero"
        cloid = f"0x{os.urandom(16).hex()}"
        order = {
            "symbol": coin,
            "side": side,
            "qty": qty,
            "limit_px": limit_px,
            "tif": "Ioc",
            "reduce_only": False,
            "cloid": cloid,
        }
        if DRY_RUN:
            log.info("pair_leg_dry_run", **order, tag=STRATEGY_TAG)
            # Pretend full-fill at mid so downstream sizing works in dry-run.
            return qty, mid, None
        resp = await self._om.submit_order(order)
        fq, fp, err = self._extract_fill(resp)
        log.info(
            "pair_leg_submitted",
            order=order,
            fill_qty=fq,
            fill_px=fp,
            error=err,
        )
        return fq, fp, err

    # ── Gates ────────────────────────────────────────────────────────────────
    async def _netting_clear(self, pair: Pair) -> bool:
        """Both legs must be flat (no other tier owns them) before we enter."""
        pa = await self._position_on(pair.leg_a)
        pb = await self._position_on(pair.leg_b)
        clear = abs(pa) < POSITION_DUST and abs(pb) < POSITION_DUST
        if not clear:
            log.info(
                "pair_netting_defer",
                pair=pair.name,
                pos_a=pa,
                pos_b=pb,
            )
        return clear

    def _funding_ok(self, pair: Pair, funding: dict[str, float]) -> bool:
        """Reject if either leg's hourly funding exceeds the tolerance band."""
        fa = abs(funding.get(pair.leg_a, 0.0))
        fb = abs(funding.get(pair.leg_b, 0.0))
        if fa > FUNDING_MAX_HOURLY or fb > FUNDING_MAX_HOURLY:
            log.info(
                "pair_funding_gate_blocked",
                pair=pair.name,
                funding_a=fa,
                funding_b=fb,
                threshold=FUNDING_MAX_HOURLY,
            )
            return False
        return True

    def _in_lockout(self, pair: Pair) -> bool:
        return (time.time() - pair.last_stop_ts) < REENTRY_LOCKOUT_S

    def _open_count(self) -> int:
        return sum(1 for p in self._pairs if p.position != 0)

    # ── Open / close ─────────────────────────────────────────────────────────
    async def _open_spread(self, pair: Pair, direction: int, mids: dict) -> None:
        """Sequential legs: A first, B sized to A's realized fill.

        direction: +1 long-spread (buy A, sell B), −1 short-spread (sell A, buy B).
        """
        px_a = mids.get(pair.leg_a)
        px_b = mids.get(pair.leg_b)
        if not (px_a and px_b):
            log.warning("pair_open_no_mid", pair=pair.name)
            return

        notional = PAIR_NOTIONAL
        if KELLY_PAIRS and pair.half_life_min > 0.0:
            # Lv/Meister multi-asset OU-Kelly collapses to single-asset form on
            # the spread: f* = k · θ · |z| / σ². θ from whitelist half-life,
            # σ from live spread residual std. Kelly can only shrink.
            z_now = pair.spread_z()
            sigma_s = pair.spread_sigma()
            if z_now is not None and sigma_s is not None:
                theta = math.log(2.0) / pair.half_life_min
                f = kelly_fraction(
                    z=z_now,
                    theta=theta,
                    sigma=sigma_s,
                    k=KELLY_K,
                    cap=KELLY_CAP,
                    sigma_floor=KELLY_SIGMA_FLOOR,
                )
                notional = PAIR_NOTIONAL * f
                log.info(
                    "pair_kelly_sizing",
                    pair=pair.name,
                    z=round(z_now, 4),
                    theta=round(theta, 6),
                    sigma=round(sigma_s, 6),
                    f=round(f, 4),
                    notional=round(notional, 2),
                    base=PAIR_NOTIONAL,
                )
                if notional < 10.0:
                    # Kargin 2003 (math/0302104) threshold policy for credit-
                    # constrained OU convergence trading: below the Kelly
                    # threshold, fall back to the base notional rather than
                    # skip — Kelly boosts SHORT-half-life pairs above base, and
                    # LONG-HL pairs trade at base via the threshold rule.
                    log.info(
                        "pair_kelly_threshold_fallback",
                        pair=pair.name,
                        kelly_notional=round(notional, 2),
                        fallback=PAIR_NOTIONAL,
                    )
                    notional = PAIR_NOTIONAL

        qty_a = round(notional / px_a, 6)
        side_a = "buy" if direction > 0 else "sell"
        side_b = "sell" if direction > 0 else "buy"

        # Leg A — less-liquid (HIP-3) side. If ambiguous, whichever we list as A.
        fill_qty_a, fill_px_a, err_a = await self._submit_leg(
            pair.leg_a, side_a, qty_a, px_a
        )
        if fill_qty_a <= 0:
            log.warning(
                "pair_open_leg_a_no_fill",
                pair=pair.name,
                error=err_a,
                intended_qty=qty_a,
            )
            return

        # Leg B sized to realized leg A fill, keeping dollar-neutrality at A's fill px.
        notional_a_realized = fill_qty_a * fill_px_a
        qty_b = round(notional_a_realized / px_b, 6)
        fill_qty_b, fill_px_b, err_b = await self._submit_leg(
            pair.leg_b, side_b, qty_b, px_b
        )
        if fill_qty_b <= 0:
            # Catastrophic — we're legged out. Unwind leg A immediately at market.
            log.error(
                "pair_open_leg_b_no_fill_unwinding",
                pair=pair.name,
                error=err_b,
                leg_a_filled=fill_qty_a,
            )
            unwind_side = "sell" if side_a == "buy" else "buy"
            await self._submit_leg(pair.leg_a, unwind_side, fill_qty_a, px_a)
            return

        # Signed inventory for later MTM and unwind.
        pair.qty_a = fill_qty_a if side_a == "buy" else -fill_qty_a
        pair.qty_b = fill_qty_b if side_b == "buy" else -fill_qty_b
        pair.entry_px_a = fill_px_a
        pair.entry_px_b = fill_px_b
        pair.position = direction
        pair.entry_ts = time.time()
        log.info(
            "pair_open_complete",
            pair=pair.name,
            direction=direction,
            qty_a=pair.qty_a,
            qty_b=pair.qty_b,
            entry_px_a=fill_px_a,
            entry_px_b=fill_px_b,
        )

    async def _close_spread(self, pair: Pair, mids: dict, reason: str) -> None:
        px_a = mids.get(pair.leg_a)
        px_b = mids.get(pair.leg_b)
        if not (px_a and px_b):
            log.warning("pair_close_no_mid", pair=pair.name)
            return

        # Close in reverse order of open (leg B first, then leg A) — the more
        # liquid leg unwinds faster, so the less-liquid residual is the last
        # thing we carry, minimizing time-in-limbo on the harder leg.
        side_b = "sell" if pair.qty_b > 0 else "buy"
        qty_b = abs(pair.qty_b)
        fq_b, fp_b, err_b = await self._submit_leg(pair.leg_b, side_b, qty_b, px_b)
        if fq_b <= 0:
            log.error(
                "pair_close_leg_b_failed",
                pair=pair.name,
                error=err_b,
                reason=reason,
            )
            # Don't close leg A if B can't unwind — we'd be flipping from
            # spread-neutral to outright directional. Retry next tick.
            return

        side_a = "sell" if pair.qty_a > 0 else "buy"
        qty_a = abs(pair.qty_a)
        fq_a, fp_a, err_a = await self._submit_leg(pair.leg_a, side_a, qty_a, px_a)
        if fq_a <= 0:
            log.error(
                "pair_close_leg_a_failed_leg_b_already_closed",
                pair=pair.name,
                error=err_a,
                reason=reason,
            )
            # Leg B is flat but leg A is still on — we've momentarily flipped
            # to directional. Retry leg A next tick; state stays partially open.
            pair.qty_b = 0.0
            return

        log.info(
            "pair_close_complete",
            pair=pair.name,
            reason=reason,
            exit_px_a=fp_a,
            exit_px_b=fp_b,
        )
        pair.position = 0
        pair.entry_z = None
        pair.qty_a = 0.0
        pair.qty_b = 0.0
        pair.entry_px_a = 0.0
        pair.entry_px_b = 0.0
        if reason == "stop":
            pair.last_stop_ts = time.time()

    # ── Main loop ────────────────────────────────────────────────────────────
    async def _tick(self) -> None:
        mids = await self._fetch_mids()
        funding = await self._fetch_funding()

        self._tick_count += 1
        # Warmup heartbeat every 5 minutes (tick = POLL_INTERVAL_S = 60s).
        if self._tick_count % 5 == 0:
            warming = [p for p in self._pairs if not p.is_warm()]
            if warming:
                min_bars = min(min(len(p.prices_a), len(p.prices_b)) for p in warming)
                log.info(
                    "pair_warmup_progress",
                    warming=len(warming),
                    total=len(self._pairs),
                    min_bars=min_bars,
                    need_bars=WARMUP_BARS,
                )

        for pair in self._pairs:
            px_a = mids.get(pair.leg_a)
            px_b = mids.get(pair.leg_b)
            if not (px_a and px_b):
                log.warning("pair_mid_missing", pair=pair.name)
                continue
            pair.push(px_a, px_b)

            if not pair.is_warm():
                continue

            if time.time() - pair.last_refit_ts >= REBETA_INTERVAL_S:
                pair.refit_beta()

            # Phase 6d: refresh β_nn from the shadow adapter (if loaded).
            # This is cheap — the inference pass is a 192-dim MLP forward —
            # so we run it every tick rather than gating on a timer.
            if pair.hedge_shadow is not None:
                pair.beta_nn = pair.hedge_shadow.beta_nn()

            z = pair.spread_z()
            if z is None:
                continue

            mtm = pair.mtm_pnl(px_a, px_b) if pair.position != 0 else 0.0

            log.info(
                "pair_tick",
                pair=pair.name,
                px_a=px_a,
                px_b=px_b,
                beta=round(pair.beta, 4),
                beta_nn=(round(pair.beta_nn, 4) if pair.beta_nn is not None else None),
                beta_source=pair.beta_source,
                hedge_mode=PAIRS_HEDGE_MODE,
                shadow=PAIRS_SHADOW,
                z=round(z, 4),
                position=pair.position,
                mtm_pnl=round(mtm, 3),
            )

            # ── Entry path ────────────────────────────────────────────────
            if pair.position == 0:
                direction = 0
                if z <= -Z_ENTRY:
                    direction = +1
                elif z >= Z_ENTRY:
                    direction = -1
                if direction == 0:
                    continue
                # Entry gates: halt flag, portfolio cap, lockout, funding, netting.
                # Existing positions still run through exit/stop — halt only blocks
                # NEW entries so operator can safely drain the book around a macro event.
                if HALT_FLAG_PATH.exists():
                    log.info(
                        "pair_entry_deferred_halt_flag",
                        pair=pair.name,
                        flag_path=str(HALT_FLAG_PATH),
                    )
                    continue
                if self._open_count() >= MAX_OPEN_PAIRS:
                    log.info("pair_entry_deferred_max_open", pair=pair.name)
                    continue
                if self._in_lockout(pair):
                    log.info("pair_entry_deferred_lockout", pair=pair.name)
                    continue
                if not self._funding_ok(pair, funding):
                    continue
                if not await self._netting_clear(pair):
                    continue
                log.info(
                    "pair_entry", pair=pair.name, z=round(z, 4), direction=direction
                )
                await self._open_spread(pair, direction, mids)
                pair.entry_z = z
                continue

            # ── Exit path ─────────────────────────────────────────────────
            if abs(z) <= Z_EXIT:
                await self._close_spread(pair, mids, reason="mean_revert")
                continue
            if abs(z) >= Z_STOP:
                await self._close_spread(pair, mids, reason="stop")
                continue
            if mtm <= -DOLLAR_STOP:
                log.info("pair_dollar_stop", pair=pair.name, mtm=mtm)
                await self._close_spread(pair, mids, reason="stop")
                continue

    async def run(self) -> None:
        install_shutdown_handlers(self._stop)
        await self._preseed_prices()
        log.info("pairs_loop_start", bars_to_warm=WARMUP_BARS)
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as exc:
                log.error("pair_tick_failed", error=str(exc), type=type(exc).__name__)
            # One-shot initial β refit when the first pair goes warm.
            for p in self._pairs:
                if p.is_warm() and p.last_refit_ts == 0.0:
                    p.refit_beta()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=POLL_INTERVAL_S)
            except asyncio.TimeoutError:
                pass

        log.info("pairs_shutdown_closing_open_positions")
        mids = await self._fetch_mids()
        for p in self._pairs:
            if p.position != 0:
                await self._close_spread(p, mids, reason="shutdown")


async def _main() -> int:
    try:
        whitelist = _load_whitelist()
    except (FileNotFoundError, RuntimeError) as exc:
        log.error("whitelist_load_failed", error=str(exc))
        print(f"ERROR: {exc}")
        return 1

    engine = PairsEngine(whitelist)
    await engine.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
