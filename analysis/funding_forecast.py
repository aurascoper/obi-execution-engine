#!/usr/bin/env python3
"""Funding-rate forecast comparison (Roadmap Commit 2a).

Pulls HL funding_history per symbol over a configurable window, then
walks forward predicting next-hour funding via four baselines:

    no_change         f[t+1] = f[t]
    rolling_mean(N=8) average of last 8 fundings
    EWMA(alpha)       exponentially-weighted moving average
    AR(1)             one-coefficient autoregression on funding diffs

Computes MAE on a held-out tail (default last 30% of the window) and
reports which model wins per symbol.

Pure analysis — no engine touches, no signal generation. Output is a
table to stdout + a JSON dump to autoresearch_gated/funding_forecast.json.

Usage:
    venv/bin/python3 analysis/funding_forecast.py
    venv/bin/python3 analysis/funding_forecast.py --symbols BTC,ETH,SOL --days 90
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "autoresearch_gated" / "funding_forecast.json"
DEFAULT_SYMBOLS = ("BTC", "ETH", "SOL", "DOGE", "AAVE")


def fetch_funding(symbol: str, days: int) -> list[tuple[int, float]]:
    """Returns [(time_ms, fundingRate)] sorted ascending."""
    from hyperliquid.info import Info
    from hyperliquid.utils import constants

    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    now_ms = int(dt.datetime.now(tz=dt.timezone.utc).timestamp() * 1000)
    from_ms = now_ms - days * 86_400_000
    raw = info.funding_history(name=symbol, startTime=from_ms, endTime=now_ms) or []
    out = []
    for r in raw:
        try:
            t = int(r.get("time", 0))
            f = float(r.get("fundingRate", 0))
            out.append((t, f))
        except (TypeError, ValueError):
            continue
    out.sort()
    return out


# ── Forecasters ───────────────────────────────────────────────────────────
def predict_no_change(history: list[float]) -> float:
    return history[-1] if history else 0.0


def predict_rolling_mean(history: list[float], n: int = 8) -> float:
    if not history:
        return 0.0
    tail = history[-n:]
    return sum(tail) / len(tail)


def predict_ewma(history: list[float], alpha: float = 0.3) -> float:
    if not history:
        return 0.0
    s = history[0]
    for x in history[1:]:
        s = alpha * x + (1 - alpha) * s
    return s


def fit_ar1(history: list[float]) -> tuple[float, float]:
    """Fit f[t] = c + phi * f[t-1] + eps via OLS. Returns (c, phi)."""
    if len(history) < 2:
        return 0.0, 0.0
    n = len(history) - 1
    x = history[:-1]
    y = history[1:]
    mx = sum(x) / n
    my = sum(y) / n
    num = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    den = sum((xi - mx) ** 2 for xi in x)
    if den == 0:
        return my, 0.0
    phi = num / den
    c = my - phi * mx
    return c, phi


def predict_ar1(history: list[float]) -> float:
    if len(history) < 2:
        return predict_no_change(history)
    c, phi = fit_ar1(history)
    return c + phi * history[-1]


# ── Walk-forward evaluation ───────────────────────────────────────────────
def evaluate(funding: list[tuple[int, float]], holdout_frac: float = 0.30) -> dict:
    rates = [f for _, f in funding]
    n = len(rates)
    if n < 20:
        return {"n_obs": n, "error": "insufficient history"}
    holdout_start = max(int(n * (1 - holdout_frac)), 10)

    errs_nc, errs_rm, errs_ewma, errs_ar1 = [], [], [], []
    for i in range(holdout_start, n):
        hist = rates[:i]
        actual = rates[i]
        errs_nc.append(abs(predict_no_change(hist) - actual))
        errs_rm.append(abs(predict_rolling_mean(hist, n=8) - actual))
        errs_ewma.append(abs(predict_ewma(hist, alpha=0.3) - actual))
        errs_ar1.append(abs(predict_ar1(hist) - actual))

    def mae(lst): return sum(lst) / len(lst) if lst else float("inf")
    maes = {
        "no_change": mae(errs_nc),
        "rolling_mean_8": mae(errs_rm),
        "ewma_0.3": mae(errs_ewma),
        "ar1": mae(errs_ar1),
    }
    best = min(maes, key=lambda k: maes[k])
    # Also compute simple stats on funding itself
    mean_funding = sum(rates) / n
    abs_mean = sum(abs(r) for r in rates) / n
    return {
        "n_obs": n,
        "holdout_n": n - holdout_start,
        "mean_funding": mean_funding,
        "mean_abs_funding": abs_mean,
        "mae": maes,
        "best_model": best,
        "improvement_vs_no_change_pct": round(
            (1 - maes[best] / maes["no_change"]) * 100, 2
        ) if maes["no_change"] > 0 else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--holdout-frac", type=float, default=0.30)
    args = ap.parse_args()

    syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"# funding_forecast — window {args.days}d, holdout {args.holdout_frac:.0%}")
    print()
    print(f"  {'sym':<6s}  {'n_obs':>5s}  {'mean':>9s}  {'|mean|':>9s}  "
          f"{'mae_no_chg':>10s}  {'mae_roll8':>10s}  {'mae_ewma':>10s}  "
          f"{'mae_ar1':>10s}  {'best':<14s}  {'gain%':>6s}")
    print("  " + "-" * 110)
    results = {}
    for sym in syms:
        try:
            funding = fetch_funding(sym, args.days)
        except Exception as e:
            print(f"  {sym:<6s}  ERROR: {e}")
            continue
        r = evaluate(funding, holdout_frac=args.holdout_frac)
        results[sym] = r
        if "error" in r:
            print(f"  {sym:<6s}  {r['n_obs']:>5d}  ({r['error']})")
            continue
        m = r["mae"]
        gain = r.get("improvement_vs_no_change_pct")
        gain_s = f"{gain:+.1f}" if gain is not None else "  N/A"
        print(
            f"  {sym:<6s}  {r['n_obs']:>5d}  {r['mean_funding']:>+9.6f}  "
            f"{r['mean_abs_funding']:>9.6f}  "
            f"{m['no_change']:>10.6f}  {m['rolling_mean_8']:>10.6f}  "
            f"{m['ewma_0.3']:>10.6f}  {m['ar1']:>10.6f}  "
            f"{r['best_model']:<14s}  {gain_s:>5s}%"
        )

    out_path.write_text(json.dumps(results, indent=2, default=str))
    print(f"\n# wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
