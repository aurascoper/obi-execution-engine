#!/usr/bin/env python3
"""Gap A — funding-residual regression on next-hour log return.

Tests:
    next_hour_log_return ~ alpha + beta * (funding_rate − expected_funding)

where `expected_funding` is the rolling-mean-8 forecast (validated by
funding_forecast.py as ≈optimal among simple baselines).

Per-symbol OLS plus a pooled regression. Decision rule per the
roadmap: if R² > 0.02 on majors or pooled, signals/funding_basis.py
is justified as a follow-up. Otherwise, funding stays an
accounting/cost-model term, not a signal-drift term.

Pure analysis. No imports from strategy/, risk/, hl_engine.py.

Usage:
    venv/bin/python3 analysis/funding_gap_regression.py
    venv/bin/python3 analysis/funding_gap_regression.py --symbols BTC,ETH,SOL --days 90
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "autoresearch_gated" / "funding_gap_regression.json"
DEFAULT_SYMBOLS = ("BTC", "ETH", "SOL", "DOGE", "AAVE")
ROLLING_N = 8  # window for expected_funding baseline
HOUR_MS = 3_600_000


def fetch_funding(symbol: str, days: int):
    from hyperliquid.info import Info
    from hyperliquid.utils import constants

    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    now_ms = int(dt.datetime.now(tz=dt.timezone.utc).timestamp() * 1000)
    from_ms = now_ms - days * 86_400_000
    raw = info.funding_history(name=symbol, startTime=from_ms, endTime=now_ms) or []
    out = []
    for r in raw:
        try:
            out.append((int(r.get("time", 0)), float(r.get("fundingRate", 0))))
        except (TypeError, ValueError):
            continue
    out.sort()
    return out


def fetch_candles(symbol: str, days: int):
    from hyperliquid.info import Info
    from hyperliquid.utils import constants

    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    now_ms = int(dt.datetime.now(tz=dt.timezone.utc).timestamp() * 1000)
    from_ms = now_ms - days * 86_400_000
    raw = (
        info.candles_snapshot(
            name=symbol, interval="1h", startTime=from_ms, endTime=now_ms
        )
        or []
    )
    out = []
    for r in raw:
        try:
            t_open = int(r.get("t", 0))
            close = float(r.get("c", 0))
            if close > 0:
                out.append((t_open, close))
        except (TypeError, ValueError):
            continue
    out.sort()
    return out


def build_pairs(funding, candles):
    """Per funding event at time t, compute:
        x = funding[t] − rolling_mean_8(prev 8 fundings)
        y = ln(close[t+1h] / close[t])

    HL funding ts is at hour+~55ms (not exactly on the boundary), while
    candle.t is exactly on the hour open. Floor funding ts to hour and
    use the candle whose close coincides with that hour.

    candle [t_open, t_open+1h)  →  closes at t_open+1h
    so close-AT-hour-T  =  candle with t_open = T-1h
       close-AT-hour-T+1h = candle with t_open = T

    Returns list[(t, x, y)].
    """
    cmap = {t: c for t, c in candles}
    pairs = []
    for i in range(ROLLING_N, len(funding)):
        ts, f = funding[i]
        # floor to hour boundary
        t_hour = (ts // HOUR_MS) * HOUR_MS
        prior = [funding[j][1] for j in range(i - ROLLING_N, i)]
        expected = sum(prior) / len(prior)
        residual = f - expected
        c_now = cmap.get(t_hour - HOUR_MS)  # close of candle ending at t_hour
        c_next = cmap.get(t_hour)  # close of candle ending at t_hour+1h
        if c_now is None or c_next is None or c_now <= 0 or c_next <= 0:
            continue
        log_ret = math.log(c_next / c_now)
        pairs.append((t_hour, residual, log_ret))
    return pairs


def ols(xs, ys):
    """Returns (alpha, beta, t_stat, r2, n)."""
    n = len(xs)
    if n < 30:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    beta = sxy / sxx
    alpha = my - beta * mx
    # residual variance
    resid = [y - alpha - beta * x for x, y in zip(xs, ys)]
    rss = sum(r * r for r in resid)
    sigma2 = rss / max(n - 2, 1)
    se_beta = math.sqrt(sigma2 / sxx) if sxx > 0 else float("inf")
    t_stat = beta / se_beta if se_beta > 0 else 0.0
    r2 = 1 - rss / syy
    return {
        "alpha": alpha,
        "beta": beta,
        "t_stat": t_stat,
        "r2": r2,
        "n": n,
        "se_beta": se_beta,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--r2-threshold", type=float, default=0.02)
    args = ap.parse_args()

    syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"# Gap A regression — window {args.days}d, expected = rolling_mean_8")
    print()
    print(
        f"  {'sym':<6s}  {'n':>4s}  {'beta':>10s}  {'t_stat':>8s}  "
        f"{'R²':>8s}  {'direction':>9s}  {'passes_r2_thresh':>16s}"
    )
    print("  " + "-" * 80)
    per_sym = {}
    pooled_xs, pooled_ys = [], []
    for sym in syms:
        try:
            funding = fetch_funding(sym, args.days)
            candles = fetch_candles(sym, args.days)
        except Exception as e:
            print(f"  {sym:<6s}  ERROR: {e}")
            continue
        pairs = build_pairs(funding, candles)
        xs = [x for _, x, _ in pairs]
        ys = [y for _, _, y in pairs]
        pooled_xs.extend(xs)
        pooled_ys.extend(ys)
        r = ols(xs, ys)
        if r is None:
            print(f"  {sym:<6s}  insufficient data (n={len(pairs)})")
            per_sym[sym] = {"n": len(pairs), "error": "insufficient_data"}
            continue
        direction = (
            "positive" if r["beta"] > 0 else "negative" if r["beta"] < 0 else "zero"
        )
        passes = r["r2"] >= args.r2_threshold
        print(
            f"  {sym:<6s}  {r['n']:>4d}  {r['beta']:>+10.4f}  {r['t_stat']:>+8.2f}  "
            f"{r['r2']:>+8.5f}  {direction:>9s}  {'PASS' if passes else 'fail':>16s}"
        )
        per_sym[sym] = {**r, "direction": direction, "passes_r2_thresh": passes}

    pooled_r = ols(pooled_xs, pooled_ys) if pooled_xs else None
    if pooled_r is not None:
        passes = pooled_r["r2"] >= args.r2_threshold
        direction = (
            "positive"
            if pooled_r["beta"] > 0
            else "negative"
            if pooled_r["beta"] < 0
            else "zero"
        )
        print("  " + "-" * 80)
        print(
            f"  {'POOL':<6s}  {pooled_r['n']:>4d}  {pooled_r['beta']:>+10.4f}  "
            f"{pooled_r['t_stat']:>+8.2f}  {pooled_r['r2']:>+8.5f}  "
            f"{direction:>9s}  {'PASS' if passes else 'fail':>16s}"
        )
    else:
        passes = False

    # Verdict
    print()
    print("=== verdict ===")
    if pooled_r is None:
        print("  no pooled regression — insufficient data")
    elif pooled_r["r2"] >= args.r2_threshold:
        sign = "+" if pooled_r["beta"] > 0 else "−"
        print(
            f"  PASS — pooled R² = {pooled_r['r2']:.5f} ≥ {args.r2_threshold}, "
            f"beta {sign} (t = {pooled_r['t_stat']:+.2f})"
        )
        print("  → signals/funding_basis.py JUSTIFIED as next step")
        print(f"  → direction: residual {sign} predicts next-hour return {sign}")
    else:
        print(f"  FAIL — pooled R² = {pooled_r['r2']:.5f} < {args.r2_threshold}")
        print("  → signals/funding_basis.py NOT justified")
        print("  → keep funding as an accounting/cost-model term only")

    out = {
        "window_days": args.days,
        "expected_baseline": "rolling_mean_8",
        "r2_threshold": args.r2_threshold,
        "per_symbol": per_sym,
        "pooled": pooled_r,
        "pooled_passes": passes,
    }
    out_path.write_text(json.dumps(out, indent=2, default=str))
    print(f"\n# wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
