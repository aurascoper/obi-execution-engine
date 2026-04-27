#!/usr/bin/env python3
"""BOCPD run-length monitor (Roadmap Commit 3).

Adams-MacKay (2007) Bayesian Online Changepoint Detection over log
returns of HL 1h/15m/1m candles. NIG-Gaussian conjugate predictive
(Student-t) and constant hazard.

Per symbol+interval, returns:
    latest_run_length   argmax of posterior over run length at t=T
    changepoint_prob    posterior P(r_T = 0)
    mean_return         mean log return over the current run length
    vol                 stdev of log return over the current run length
    regime_label        stable | unstable | recent_break | ambiguous

Pure analysis. No engine wiring. Output:
    analysis_outputs/regime_runlength_latest.csv         (per-row latest)
    analysis_outputs/regime_runlength_history.jsonl      (full posterior trace)

Usage:
    venv/bin/python3 analysis/regime_runlength.py
    venv/bin/python3 analysis/regime_runlength.py --interval 1h --days 90
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
from pathlib import Path

import numpy as np
from scipy.special import gammaln

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "analysis_outputs"
DEFAULT_SYMBOLS = ("BTC", "ETH", "SOL", "DOGE", "AAVE")
DEFAULT_INTERVAL = "1h"
DEFAULT_DAYS = 90

# Hyperparameters (NIG conjugate prior)
PRIOR_MU0 = 0.0       # mean log return prior
PRIOR_KAPPA0 = 1.0    # pseudo-count for mean
PRIOR_ALPHA0 = 1.0    # IG shape (alpha=1 -> diffuse-ish)
PRIOR_BETA0 = 1e-4    # IG scale; small to allow scale inference

# Constant hazard: P(changepoint at any step) = 1/LAMBDA
DEFAULT_LAMBDA = 250  # prior expected run length in bars

# Truncate run-length axis (BOCPD memory grows linearly; cap for speed)
MAX_RUN_LENGTH = 2500


def fetch_candles(symbol: str, interval: str, days: int):
    from hyperliquid.info import Info
    from hyperliquid.utils import constants

    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    now_ms = int(dt.datetime.now(tz=dt.timezone.utc).timestamp() * 1000)
    from_ms = now_ms - days * 86_400_000
    raw = info.candles_snapshot(
        name=symbol, interval=interval, startTime=from_ms, endTime=now_ms
    ) or []
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


def log_returns(candles):
    rets = []
    times = []
    for i in range(1, len(candles)):
        t, c = candles[i]
        _, c_prev = candles[i - 1]
        if c > 0 and c_prev > 0:
            rets.append(math.log(c / c_prev))
            times.append(t)
    return times, rets


def student_t_log_pdf(x, mu, kappa, alpha, beta):
    """Posterior predictive for one observation under NIG conjugate.

    Returns log-density of x under Student-t with:
        location  μ
        scale²   = β * (κ+1) / (α * κ)
        df       = 2α

    Args may be float scalars or numpy arrays of equal length.
    """
    df = 2.0 * alpha
    scale_sq = beta * (kappa + 1.0) / (alpha * kappa)
    z = (x - mu) ** 2 / scale_sq
    # log-density Student-t (gammaln is the vectorized log-gamma)
    log_norm = (
        gammaln((df + 1) / 2.0)
        - gammaln(df / 2.0)
        - 0.5 * np.log(df * math.pi * scale_sq)
    )
    return log_norm - ((df + 1) / 2.0) * np.log1p(z / df)


def bocpd(returns, lam=DEFAULT_LAMBDA, max_rl=MAX_RUN_LENGTH):
    """Run BOCPD over a 1D returns series.

    Returns:
        R         (T+1, R_max) posterior over run lengths after each step
        run_length_mean  per-step expected run length
        cp_prob   per-step P(r_t = 0)
        mu_t      per-step posterior-mean estimate of regime mean
        var_t     per-step posterior-mean estimate of regime variance
    """
    T = len(returns)
    R_max = min(T + 1, max_rl + 1)
    # NIG state per run length r (r=0..R_max-1)
    mu = np.full(R_max, PRIOR_MU0)
    kappa = np.full(R_max, PRIOR_KAPPA0)
    alpha = np.full(R_max, PRIOR_ALPHA0)
    beta = np.full(R_max, PRIOR_BETA0)

    # posterior over run length
    R = np.zeros((T + 1, R_max))
    R[0, 0] = 1.0

    H = 1.0 / lam  # constant hazard

    cp_prob = np.zeros(T)
    rl_mean = np.zeros(T)
    mu_post_t = np.zeros(T)
    var_post_t = np.zeros(T)

    for t in range(T):
        x = returns[t]
        # 1. Predictive probability under each run length
        log_pred = student_t_log_pdf(x, mu, kappa, alpha, beta)
        # for numerical stability subtract max before exp
        max_lp = np.max(log_pred)
        pred = np.exp(log_pred - max_lp)

        # 2. Growth probabilities (run length continues)
        growth = R[t] * pred * (1 - H)
        # 3. Changepoint probability (collapses to r=0)
        cp = np.sum(R[t] * pred * H)

        new_R = np.zeros(R_max)
        new_R[0] = cp
        # shift growth into r+1
        end = min(R_max, R_max)  # ensure no overflow
        new_R[1:R_max] = growth[: R_max - 1]

        # Numerical normalize via the same max-subtraction factor
        total = new_R.sum()
        if total <= 0:
            new_R[0] = 1.0
            total = 1.0
        new_R /= total
        R[t + 1] = new_R

        cp_prob[t] = new_R[0]
        rls = np.arange(R_max)
        rl_mean[t] = np.sum(rls * new_R)

        # 4. Update sufficient stats for each run length (NIG conjugate)
        # Each run length's stats reflect all observations since the last
        # changepoint. Convention (Adams-MacKay): the param at index r
        # corresponds to having seen r observations since cp.
        # Update by shifting and applying the new datum to each shifted r.
        new_mu = np.empty(R_max)
        new_kappa = np.empty(R_max)
        new_alpha = np.empty(R_max)
        new_beta = np.empty(R_max)
        # r=0 resets to prior (a fresh changepoint hypothesis)
        new_mu[0] = PRIOR_MU0
        new_kappa[0] = PRIOR_KAPPA0
        new_alpha[0] = PRIOR_ALPHA0
        new_beta[0] = PRIOR_BETA0
        # for r>=1: derived from old run length r-1 plus this datum
        kappa_new = kappa[: R_max - 1] + 1
        new_kappa[1:R_max] = kappa_new
        new_mu[1:R_max] = (kappa[: R_max - 1] * mu[: R_max - 1] + x) / kappa_new
        new_alpha[1:R_max] = alpha[: R_max - 1] + 0.5
        new_beta[1:R_max] = (
            beta[: R_max - 1]
            + 0.5 * kappa[: R_max - 1] * (x - mu[: R_max - 1]) ** 2 / kappa_new
        )
        mu = new_mu
        kappa = new_kappa
        alpha = new_alpha
        beta = new_beta

        # 5. Posterior-mean estimate of current regime params (for reporting)
        mu_post_t[t] = float(np.sum(new_R * mu))
        # variance under NIG: E[σ²] = β / (α-1) for α>1
        # silence divide-by-zero in the masked-out branch — np.where evaluates
        # both branches even where mask is False
        with np.errstate(divide="ignore", invalid="ignore"):
            var_each = np.where(alpha > 1.0, beta / (alpha - 1.0), beta / alpha)
        var_post_t[t] = float(np.sum(new_R * var_each))

    return {
        "R": R,
        "cp_prob": cp_prob,
        "rl_mean": rl_mean,
        "mu_post": mu_post_t,
        "var_post": var_post_t,
    }


def classify(latest_rl: float, latest_cp: float, latest_var: float) -> str:
    if latest_cp > 0.50:
        return "recent_break"
    if latest_rl < 30:
        return "unstable"
    if latest_rl >= 120 and latest_cp < 0.10:
        return "stable"
    return "ambiguous"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--interval", default=DEFAULT_INTERVAL,
                    help="HL candle interval (1m, 15m, 1h, ...)")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ap.add_argument("--lambda", dest="lam", type=int, default=DEFAULT_LAMBDA,
                    help="hazard prior — mean run length in bars")
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    args = ap.parse_args()

    syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"regime_runlength_latest_{args.interval}_{args.days}d.csv"
    jsonl_path = out_dir / f"regime_runlength_history_{args.interval}_{args.days}d.jsonl"

    print(f"# BOCPD — interval {args.interval}, window {args.days}d, hazard λ={args.lam}")
    print()
    print(
        f"  {'sym':<6s}  {'n':>5s}  {'latest_rl':>9s}  {'argmax_rl':>9s}  "
        f"{'cp_prob':>8s}  {'mean_ret':>10s}  {'vol':>10s}  {'label':<14s}"
    )
    print("  " + "-" * 90)

    rows = []
    history_fh = jsonl_path.open("w")
    for sym in syms:
        try:
            candles = fetch_candles(sym, args.interval, args.days)
        except Exception as e:
            print(f"  {sym:<6s}  ERROR: {e}")
            continue
        times, rets = log_returns(candles)
        n = len(rets)
        if n < 50:
            print(f"  {sym:<6s}  insufficient data (n={n})")
            continue
        try:
            res = bocpd(np.asarray(rets, dtype=float), lam=args.lam)
        except Exception as e:
            print(f"  {sym:<6s}  bocpd failed: {e}")
            continue
        R_T = res["R"][-1]
        argmax_rl = int(np.argmax(R_T))
        cp_prob = float(R_T[0])
        rl_mean = float(res["rl_mean"][-1])
        # Recent regime stats over the most-likely run length
        # (use argmax; if 0, fall back to last 30 bars to compute a sane stat)
        run_len = max(argmax_rl, 30) if argmax_rl > 0 else 30
        recent = rets[-run_len:]
        mean_ret = float(np.mean(recent))
        vol = float(np.std(recent))
        label = classify(rl_mean, cp_prob, vol)
        print(
            f"  {sym:<6s}  {n:>5d}  {rl_mean:>9.1f}  {argmax_rl:>9d}  "
            f"{cp_prob:>8.4f}  {mean_ret:>+10.6f}  {vol:>10.6f}  {label:<14s}"
        )
        rows.append({
            "symbol": sym,
            "interval": args.interval,
            "days": args.days,
            "lambda": args.lam,
            "n_returns": n,
            "latest_run_length_mean": rl_mean,
            "argmax_run_length": argmax_rl,
            "changepoint_prob": cp_prob,
            "mean_return_recent": mean_ret,
            "vol_recent": vol,
            "regime_label": label,
            "z_recent": (mean_ret / vol) if vol > 0 else None,
        })
        # Per-step history (compact: ts, rl_mean, cp_prob)
        for i, t in enumerate(times):
            history_fh.write(json.dumps({
                "symbol": sym,
                "interval": args.interval,
                "ts": t,
                "rl_mean": float(res["rl_mean"][i]),
                "cp_prob": float(res["cp_prob"][i]),
                "mu_post": float(res["mu_post"][i]),
                "var_post": float(res["var_post"][i]),
            }) + "\n")
    history_fh.close()

    # CSV write
    if rows:
        import csv
        with csv_path.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    print()
    # Summary verdict
    if rows:
        labels = [r["regime_label"] for r in rows]
        n_break = sum(1 for L in labels if L == "recent_break")
        n_unstable = sum(1 for L in labels if L == "unstable")
        n_stable = sum(1 for L in labels if L == "stable")
        n_amb = sum(1 for L in labels if L == "ambiguous")
        print("=== summary ===")
        print(f"  recent_break : {n_break}")
        print(f"  unstable     : {n_unstable}")
        print(f"  stable       : {n_stable}")
        print(f"  ambiguous    : {n_amb}")
        print()
        print("=== verdict ===")
        if n_stable >= len(rows) - n_amb and n_break + n_unstable <= 1:
            print("  STATIC z-thresholds reasonable for this interval/window.")
            print("  → analysis/regime_threshold_backtest.py NOT required first.")
        elif n_break + n_unstable >= len(rows) // 2:
            print("  FREQUENT regime changes detected — static z-thresholds suspect.")
            print("  → analysis/regime_threshold_backtest.py IS the next non-risk step.")
        else:
            print("  MIXED regime stability across symbols.")
            print("  → consider per-symbol threshold logic before global rules.")
    print()
    print(f"# wrote {csv_path}")
    print(f"# wrote {jsonl_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
