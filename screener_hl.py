#!/usr/bin/env python3
"""
screener_hl.py — Rank Hyperliquid perps for HL engine universe expansion.

Pipeline:
  1. Pull `info.meta_and_asset_ctxs()` → name, szDecimals, maxLeverage, dayNtlVlm.
  2. Filter: exclude BTC/ETH (already in production), require maxLeverage ≥ 3,
             intersect with Alpaca-supported crypto bar symbols (without Alpaca
             bars the z-score buffer never warms and the coin silently no-ops).
  3. Annotate each survivor with top-of-book spread (bps) via l2_snapshot.
  4. Filter: spread ≤ 5 bps.
  5. Rank by 24h notional volume.
  6. Write top N to config/hl_universe_candidates.json.

CLI:
  python3 screener_hl.py [--top N] [--min-vlm USD] [--apply]

--apply prints a suggested HL_UNIVERSE CSV that pins BTC+ETH as the first two
coins (preserves the production baseline) and appends the top N.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from hyperliquid.info import Info
from hyperliquid.utils import constants


# Alpaca crypto bar coverage — see data/feed.py subscribe_bars + strategy/signals.py
# _QTY_DECIMALS. Any coin NOT in this set has no Alpaca bar source, so the
# SignalEngine rolling buffer would never fill. Pre-filtering here is the only
# honest way to keep the engine universe and the data feed in sync.
ALPACA_SUPPORTED = {"BTC", "ETH", "SOL", "DOGE", "AVAX", "LINK", "SHIB"}
EXCLUDE_CORE = {"BTC", "ETH"}
MAX_SPREAD_BPS = 5.0
MIN_MAX_LEVERAGE = 3
L2_RATE_LIMIT_SEC = 0.22  # ≈5 req/sec


def _mainnet_url() -> str:
    return getattr(
        constants,
        "MAINNET_API_URL",
        getattr(constants, "MAINNET", "https://api.hyperliquid.xyz"),
    )


def _top_spread_bps(info: Info, coin: str) -> float | None:
    l2 = info.l2_snapshot(coin)
    levels = l2.get("levels") or [[], []]
    if len(levels) < 2:
        return None
    bids, asks = levels[0], levels[1]
    if not bids or not asks:
        return None
    try:
        bid = float(bids[0]["px"])
        ask = float(asks[0]["px"])
    except KeyError, ValueError, TypeError:
        return None
    if bid <= 0 or ask <= 0 or ask <= bid:
        return None
    mid = 0.5 * (bid + ask)
    return (ask - bid) / mid * 10_000.0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1].strip())
    ap.add_argument(
        "--top", type=int, default=10, help="max candidates to write (default: 10)"
    )
    ap.add_argument(
        "--min-vlm",
        type=float,
        default=0.0,
        help="min 24h notional volume in USD (default: 0)",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="print suggested HL_UNIVERSE CSV for env.sh",
    )
    args = ap.parse_args()

    info = Info(_mainnet_url(), skip_ws=True)
    meta_ctxs = info.meta_and_asset_ctxs()
    if not isinstance(meta_ctxs, list) or len(meta_ctxs) != 2:
        raise SystemExit(
            f"[screener] unexpected meta_and_asset_ctxs shape: {type(meta_ctxs)}"
        )
    meta, ctxs = meta_ctxs
    universe = meta.get("universe", [])
    if len(universe) != len(ctxs):
        raise SystemExit(
            f"[screener] universe/ctxs length mismatch: {len(universe)} vs {len(ctxs)}"
        )

    # --- metadata filter pass ---
    candidates: list[dict] = []
    for asset, ctx in zip(universe, ctxs):
        name = str(asset.get("name", "")).upper()
        if not name or name in EXCLUDE_CORE or name not in ALPACA_SUPPORTED:
            continue
        try:
            max_lev = int(asset.get("maxLeverage", 0))
            sz_dec = int(asset.get("szDecimals", 0))
            day_vlm = float(ctx.get("dayNtlVlm", 0) or 0)
            mark = float(ctx.get("markPx", 0) or 0)
            oi = float(ctx.get("openInterest", 0) or 0)
        except TypeError, ValueError:
            continue
        if max_lev < MIN_MAX_LEVERAGE:
            continue
        if day_vlm < args.min_vlm:
            continue
        candidates.append(
            {
                "coin": name,
                "sz_decimals": sz_dec,
                "max_leverage": max_lev,
                "day_vlm": day_vlm,
                "open_interest": oi,
                "mark_px": mark,
            }
        )

    if not candidates:
        raise SystemExit(
            "[screener] no candidates passed metadata filter "
            "(Alpaca whitelist ∩ HL perps ∩ maxLeverage ≥ 3) — "
            "refusing to write empty JSON"
        )

    # --- spread filter pass (rate-limited) ---
    for c in candidates:
        c["spread_bps"] = _top_spread_bps(info, c["coin"])
        time.sleep(L2_RATE_LIMIT_SEC)
    surviving = [
        c
        for c in candidates
        if c["spread_bps"] is not None and c["spread_bps"] <= MAX_SPREAD_BPS
    ]
    if not surviving:
        rejected = [(c["coin"], c["spread_bps"]) for c in candidates]
        raise SystemExit(
            f"[screener] no candidates after spread filter (≤{MAX_SPREAD_BPS} bps). "
            f"rejected={rejected}"
        )

    surviving.sort(key=lambda c: c["day_vlm"], reverse=True)
    top = surviving[: args.top]

    # --- display ---
    print(
        f"{'coin':<6} {'szDec':>6} {'maxLev':>7} {'dayVlm USD':>15} "
        f"{'spreadBps':>10} {'mark':>12} {'OI':>14}"
    )
    print("─" * 78)
    for c in top:
        print(
            f"{c['coin']:<6} {c['sz_decimals']:>6} {c['max_leverage']:>7} "
            f"{c['day_vlm']:>15,.0f} {c['spread_bps']:>10.2f} "
            f"{c['mark_px']:>12.4f} {c['open_interest']:>14,.2f}"
        )

    # --- persist ---
    out = {
        "generated_ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "filters": {
            "alpaca_whitelist": sorted(ALPACA_SUPPORTED - EXCLUDE_CORE),
            "max_spread_bps": MAX_SPREAD_BPS,
            "min_max_leverage": MIN_MAX_LEVERAGE,
            "min_day_vlm": args.min_vlm,
        },
        "candidates": top,
    }
    out_path = Path("config/hl_universe_candidates.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2) + "\n")
    print(f"\nwrote {out_path} with {len(top)} candidate(s).")

    if args.apply:
        suggested = ",".join(["BTC", "ETH", *[c["coin"] for c in top]])
        print(f'\nsuggested HL_UNIVERSE CSV:\n  export HL_UNIVERSE="{suggested}"')


if __name__ == "__main__":
    main()
