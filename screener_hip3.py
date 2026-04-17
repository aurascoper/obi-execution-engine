#!/usr/bin/env python3
"""
screener_hip3.py — HIP-3 perp DEX portfolio selector for hl_engine.py.

Scores TradeXYZ (and future HIP-3 DEXs) assets on mean-reversion fitness,
liquidity, and cross-asset diversification. Outputs a ranked universe with
per-coin z-tiers, leverage, and notional caps ready for shadow or LIVE launch.

Pipeline:
  1. Pull meta + asset contexts from each HIP-3 DEX.
  2. Filter: OI >= $1M, 24h volume >= $500K (configurable).
  3. Fetch 4h candles (7 days) → compute RMSD (coefficient of variation).
  4. Compute composite score: RMSD × liquidity × diversification bonus.
  5. Assign z-tiers and leverage per asset class.
  6. Print ranked table + shell export block for hl_engine.py launch.

CLI:
  python3 screener_hip3.py                       # full scan, default thresholds
  python3 screener_hip3.py --top 20              # top 20 by composite score
  python3 screener_hip3.py --min-oi 5            # OI >= $5M
  python3 screener_hip3.py --min-vol 2           # 24h vol >= $2M
  python3 screener_hip3.py --max-leverage 10     # cap leverage at 10x
  python3 screener_hip3.py --apply               # print shell export block
  python3 screener_hip3.py --json                # JSON output for downstream tools
  python3 screener_hip3.py --dex xyz,abc         # scan multiple DEXs
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime, timezone

from hyperliquid.info import Info
from hyperliquid.utils import constants

# ── Asset classification ─────────────────────────────────────────────────────

_INDICES = {"SP500", "XYZ100", "JP225", "KR200", "NDX", "DJI", "FTSE", "DAX"}
_COMMODITIES = {
    "GOLD",
    "SILVER",
    "CL",
    "COPPER",
    "NATGAS",
    "URANIUM",
    "ALUMINIUM",
    "PLATINUM",
    "PALLADIUM",
    "BRENTOIL",
    "CORN",
    "WHEAT",
    "TTF",
}
_FX = {"JPY", "EUR", "DXY", "GBP", "AUD", "CHF", "CNY"}
_ETFS = {"EWY", "EWJ", "XLE", "URNM", "USAR", "SPY", "QQQ", "IWM"}
_MEME_ADJACENT = {"GME", "PURRDAT", "BIRD"}


def classify(coin: str) -> str:
    base = coin.split(":")[1] if ":" in coin else coin
    if base in _INDICES:
        return "INDEX"
    if base in _COMMODITIES:
        return "COMMODITY"
    if base in _FX:
        return "FX"
    if base in _ETFS:
        return "ETF"
    if base in _MEME_ADJACENT:
        return "MEME"
    return "EQUITY"


# ── Z-tier assignment ────────────────────────────────────────────────────────
# Tighter z for low-RMSD assets (mean-reverts slowly); wider z for high-RMSD
# assets (need to catch bigger dislocations). Thresholds calibrated to 4h RMSD.


def assign_z_tier(
    cat: str,
    rmsd_pct: float,
) -> tuple[float, float, float, float]:
    """Returns (z_entry, z_exit, z_short_entry, z_exit_short)."""
    if cat == "INDEX":
        return (-1.50, -0.30, 1.50, 0.30)
    if cat == "FX":
        return (-1.25, -0.25, 1.25, 0.25)
    if cat == "COMMODITY":
        if rmsd_pct < 1.5:
            return (-1.50, -0.30, 1.50, 0.30)
        return (-1.75, -0.40, 1.75, 0.40)
    if cat == "ETF":
        return (-1.75, -0.40, 1.75, 0.40)
    # EQUITY: scale z with volatility
    if rmsd_pct >= 8.0:
        return (-2.50, -0.75, 2.50, 0.75)
    if rmsd_pct >= 5.0:
        return (-2.00, -0.50, 2.00, 0.50)
    if rmsd_pct >= 3.0:
        return (-1.75, -0.40, 1.75, 0.40)
    return (-1.50, -0.30, 1.50, 0.30)


def assign_leverage(cat: str, max_lev: int, user_cap: int) -> int:
    """Conservative leverage assignment per asset class."""
    if cat == "INDEX":
        target = min(10, max_lev)
    elif cat == "FX":
        target = min(20, max_lev)
    elif cat == "COMMODITY":
        target = min(10, max_lev)
    elif cat == "ETF":
        target = min(10, max_lev)
    else:
        target = min(5, max_lev)
    return min(target, user_cap)


# ── Composite scoring ────────────────────────────────────────────────────────

# Diversification bonus: reward categories that are underrepresented in the
# selected set, so the optimizer doesn't fill up on 25 equities.
_CATEGORY_TARGETS = {
    "INDEX": 2,
    "COMMODITY": 5,
    "FX": 2,
    "ETF": 2,
    "EQUITY": 12,
    "MEME": 0,
}


def compute_scores(assets: list[dict]) -> list[dict]:
    """
    Composite score = rmsd_score × liquidity_score × diversification_bonus.

    rmsd_score: log-scaled RMSD% (rewards volatility but diminishing returns).
    liquidity_score: log(OI × vol) normalized — deep markets are safer.
    diversification_bonus: 1.5× for categories below their target count.
    """
    if not assets:
        return []

    # Normalize RMSD: log(1 + rmsd%) so 0.5% and 13% don't differ by 26×.
    max_log_rmsd = max(math.log1p(a["rmsd_pct"]) for a in assets)
    # Normalize liquidity: log(OI_usd * vol_usd + 1)
    max_log_liq = max(math.log1p(a["oi_usd"] * a["vol_usd"]) for a in assets)

    cat_counts: dict[str, int] = {}
    for a in assets:
        cat_counts[a["cat"]] = cat_counts.get(a["cat"], 0) + 1

    for a in assets:
        rmsd_norm = math.log1p(a["rmsd_pct"]) / max_log_rmsd if max_log_rmsd > 0 else 0
        liq_norm = (
            math.log1p(a["oi_usd"] * a["vol_usd"]) / max_log_liq
            if max_log_liq > 0
            else 0
        )
        # Diversification: boost categories that are underweight.
        cat = a["cat"]
        target = _CATEGORY_TARGETS.get(cat, 5)
        current = cat_counts.get(cat, 0)
        div_bonus = 1.5 if current <= target else 1.0
        # Meme penalty
        if cat == "MEME":
            div_bonus = 0.1

        a["rmsd_score"] = rmsd_norm
        a["liq_score"] = liq_norm
        a["div_bonus"] = div_bonus
        a["composite"] = round(rmsd_norm * liq_norm * div_bonus, 6)

    assets.sort(key=lambda x: x["composite"], reverse=True)
    return assets


# ── Data fetching ────────────────────────────────────────────────────────────


def fetch_universe(
    info: Info,
    dexs: list[str],
    min_oi: float,
    min_vol: float,
) -> list[dict]:
    """Fetch meta + contexts, filter by OI and volume, return asset dicts."""
    assets: list[dict] = []
    for dex in dexs:
        try:
            raw = info.post("/info", {"type": "metaAndAssetCtxs", "dex": dex})
        except Exception as exc:
            print(f"  WARN: failed to fetch dex={dex}: {exc}", file=sys.stderr)
            continue
        meta_list = raw[0]["universe"]
        ctx_list = raw[1]
        for asset_meta, ctx in zip(meta_list, ctx_list):
            name = asset_meta.get("name", "")
            if not name:
                continue
            max_lev = int(asset_meta.get("maxLeverage", 1))
            sz_dec = int(asset_meta.get("szDecimals", 0))
            try:
                mark = float(ctx.get("markPx", 0))
                oi_coins = float(ctx.get("openInterest", 0))
                oi_usd = mark * oi_coins
                vol_usd = float(ctx.get("dayNtlVlm", 0))
                funding = float(ctx.get("funding", 0))
            except TypeError, ValueError:
                continue

            if oi_usd < min_oi or vol_usd < min_vol:
                continue

            assets.append(
                {
                    "coin": name,
                    "dex": dex,
                    "cat": classify(name),
                    "mark": mark,
                    "oi_usd": oi_usd,
                    "vol_usd": vol_usd,
                    "max_lev": max_lev,
                    "sz_dec": sz_dec,
                    "funding": funding,
                }
            )
    return assets


def fetch_rmsd(info: Info, assets: list[dict], lookback_days: int = 7) -> list[dict]:
    """Fetch 4h candles and compute RMSD (coefficient of variation %) per asset."""
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - (lookback_days * 24 * 60 * 60 * 1000)

    out: list[dict] = []
    for a in assets:
        coin = a["coin"]
        try:
            candles = info.candles_snapshot(coin, "4h", start_ms, end_ms)
        except Exception as exc:
            print(f"  SKIP {coin}: candle fetch failed: {exc}", file=sys.stderr)
            continue

        closes = []
        for c in candles:
            try:
                closes.append(float(c["c"]))
            except KeyError, TypeError, ValueError:
                continue

        if len(closes) < 10:
            print(f"  SKIP {coin}: only {len(closes)} 4h bars", file=sys.stderr)
            continue

        mean = sum(closes) / len(closes)
        if mean <= 0:
            continue
        sq_diffs = [(x - mean) ** 2 for x in closes]
        rmsd = math.sqrt(sum(sq_diffs) / len(sq_diffs))
        rmsd_pct = (rmsd / mean) * 100.0

        # 4h return stats
        rets = [
            (closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes))
        ]
        avg_ret = sum(rets) / len(rets) if rets else 0
        max_ret = max(rets) if rets else 0
        min_ret = min(rets) if rets else 0

        a["rmsd_pct"] = rmsd_pct
        a["n_bars"] = len(closes)
        a["avg_4h_ret"] = avg_ret
        a["max_4h_ret"] = max_ret
        a["min_4h_ret"] = min_ret
        out.append(a)

    return out


# ── Output formatting ────────────────────────────────────────────────────────


def print_table(ranked: list[dict], max_leverage: int) -> None:
    hdr = (
        f"{'#':>3} {'Coin':<18} {'Cat':<10} {'Price':>10} {'OI($M)':>8} "
        f"{'Vol($M)':>9} {'RMSD%':>7} {'Score':>7} {'Lev':>4} "
        f"{'Z_entry':>8} {'Z_exit':>7} {'Fund%':>8}"
    )
    print(hdr)
    print("-" * len(hdr))
    for i, a in enumerate(ranked, 1):
        lev = assign_leverage(a["cat"], a["max_lev"], max_leverage)
        z = assign_z_tier(a["cat"], a["rmsd_pct"])
        print(
            f"{i:>3} {a['coin']:<18} {a['cat']:<10} {a['mark']:>10.2f} "
            f"{a['oi_usd'] / 1e6:>8.2f} {a['vol_usd'] / 1e6:>9.2f} "
            f"{a['rmsd_pct']:>7.3f} {a['composite']:>7.4f} {lev:>4}x "
            f"{z[0]:>+8.2f} {z[1]:>+7.2f} {a['funding'] * 100:>7.4f}%"
        )


def print_category_summary(ranked: list[dict]) -> None:
    cats: dict[str, list[dict]] = {}
    for a in ranked:
        cats.setdefault(a["cat"], []).append(a)
    print("\nCategory breakdown:")
    for cat in ("INDEX", "EQUITY", "COMMODITY", "ETF", "FX", "MEME", "OTHER"):
        items = cats.get(cat)
        if not items:
            continue
        names = [a["coin"].split(":")[1] for a in items]
        avg_rmsd = sum(a["rmsd_pct"] for a in items) / len(items)
        total_oi = sum(a["oi_usd"] for a in items) / 1e6
        print(
            f"  {cat:<12} n={len(items):>2}  avg_rmsd={avg_rmsd:.2f}%  "
            f"OI=${total_oi:.0f}M  {', '.join(names)}"
        )


def print_apply(ranked: list[dict], max_leverage: int, dex: str) -> None:
    """Print shell export block for hl_engine.py launch."""
    coins = [a["coin"].split(":")[1] for a in ranked]
    shadow = [a["coin"] for a in ranked]
    print("\n# ── Shell exports for hl_engine.py shadow launch ──")
    print(f"export HIP3_DEXS={dex}")
    print(f"export HIP3_UNIVERSE={','.join(coins)}")
    print(f"export SHADOW_COINS={','.join(shadow)}")
    print(f"export HIP3_LEVERAGE={max_leverage}")
    print()
    print("# Launch (shadow — all HIP-3 coins in shadow mode):")
    print("# nohup venv/bin/python3 hl_engine.py >> logs/hl_engine.stdout 2>&1 &")
    print()
    print("# To go LIVE on specific coins, remove them from SHADOW_COINS:")
    print(f"# export SHADOW_COINS={','.join(shadow[:5])}  # keep first 5 in shadow")


def print_json(ranked: list[dict], max_leverage: int) -> None:
    """JSON output for downstream tooling."""
    out = []
    for a in ranked:
        lev = assign_leverage(a["cat"], a["max_lev"], max_leverage)
        z = assign_z_tier(a["cat"], a["rmsd_pct"])
        out.append(
            {
                "coin": a["coin"],
                "cat": a["cat"],
                "mark": a["mark"],
                "oi_usd": round(a["oi_usd"], 2),
                "vol_usd": round(a["vol_usd"], 2),
                "rmsd_pct": round(a["rmsd_pct"], 4),
                "composite_score": a["composite"],
                "leverage": lev,
                "sz_decimals": a["sz_dec"],
                "z_entry": z[0],
                "z_exit": z[1],
                "z_short_entry": z[2],
                "z_exit_short": z[3],
                "funding_rate": round(a["funding"] * 100, 6),
                "n_4h_bars": a["n_bars"],
            }
        )
    print(json.dumps(out, indent=2))


def print_engine_config(ranked: list[dict], max_leverage: int) -> None:
    """Print Python dict snippets for pasting into risk_params.py / hl_engine.py."""
    print("\n# ── SYMBOL_CAPS entries (paste into config/risk_params.py) ──")
    for a in ranked:
        sym = f"{a['coin']}/USD"
        print(f'    "{sym}": 100.0,')

    print("\n# ── Z-tier config (auto-applied at boot by screener scores) ──")
    for a in ranked:
        lev = assign_leverage(a["cat"], a["max_lev"], max_leverage)
        z = assign_z_tier(a["cat"], a["rmsd_pct"])
        base = a["coin"].split(":")[1]
        print(
            f"#  {base:<12} cat={a['cat']:<10} rmsd={a['rmsd_pct']:.2f}%  "
            f"lev={lev}x  z=({z[0]:+.2f}/{z[1]:+.2f}, {z[2]:+.2f}/{z[3]:+.2f})"
        )


# ── Main ─────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="screener_hip3",
        description="HIP-3 perp DEX portfolio selector — scores assets by 4h RMSD, liquidity, and diversification.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dex", default="xyz", help="Comma-separated DEX names (default: xyz)"
    )
    parser.add_argument(
        "--top", type=int, default=0, help="Select top N assets (0 = all passing)"
    )
    parser.add_argument(
        "--min-oi", type=float, default=1.0, help="Min OI in $M (default: 1)"
    )
    parser.add_argument(
        "--min-vol", type=float, default=0.5, help="Min 24h volume in $M (default: 0.5)"
    )
    parser.add_argument(
        "--max-leverage", type=int, default=10, help="Max leverage cap (default: 10)"
    )
    parser.add_argument(
        "--lookback", type=int, default=7, help="RMSD lookback in days (default: 7)"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Print shell export block for engine launch",
    )
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument(
        "--config", action="store_true", help="Print Python config snippets"
    )
    parser.add_argument(
        "--exclude",
        default="",
        help="Comma-separated coins to exclude (e.g. PURRDAT,BIRD)",
    )

    args = parser.parse_args()
    dexs = [d.strip() for d in args.dex.split(",") if d.strip()]
    min_oi = args.min_oi * 1e6
    min_vol = args.min_vol * 1e6
    excludes = {e.strip().upper() for e in args.exclude.split(",") if e.strip()}

    url = getattr(
        constants,
        "MAINNET_API_URL",
        getattr(constants, "MAINNET", "https://api.hyperliquid.xyz"),
    )
    info = Info(url, skip_ws=True, perp_dexs=dexs)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"screener_hip3 — {ts}")
    print(
        f"DEXs: {dexs}  OI>=${args.min_oi}M  Vol>=${args.min_vol}M  MaxLev={args.max_leverage}x  Lookback={args.lookback}d"
    )
    if excludes:
        print(f"Excluding: {', '.join(sorted(excludes))}")
    print()

    # 1. Fetch and filter
    print("Fetching universe...", file=sys.stderr)
    assets = fetch_universe(info, dexs, min_oi, min_vol)

    # Apply exclusions
    if excludes:
        assets = [a for a in assets if a["coin"].split(":")[1] not in excludes]

    print(f"  {len(assets)} assets pass OI/volume filters", file=sys.stderr)

    # 2. Compute RMSD
    print(f"Fetching {args.lookback}d 4h candles...", file=sys.stderr)
    assets = fetch_rmsd(info, assets, args.lookback)
    print(f"  {len(assets)} assets with valid RMSD", file=sys.stderr)

    # 3. Score and rank
    ranked = compute_scores(assets)

    # 4. Apply top-N
    if args.top > 0:
        ranked = ranked[: args.top]

    if not ranked:
        print("No assets passed all filters.", file=sys.stderr)
        return 1

    # 5. Output
    if args.json:
        print_json(ranked, args.max_leverage)
        return 0

    print_table(ranked, args.max_leverage)
    print_category_summary(ranked)

    if args.config:
        print_engine_config(ranked, args.max_leverage)

    if args.apply:
        print_apply(ranked, args.max_leverage, dexs[0])

    print(f"\nTotal: {len(ranked)} assets selected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
