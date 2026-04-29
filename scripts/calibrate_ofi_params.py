#!/usr/bin/env python3
"""
scripts/calibrate_ofi_params.py — Fit (beta, sigma, eta) per symbol from logs.

Reads engine JSONL logs (logs/engine.jsonl, logs/equities_engine.jsonl) and
emits config/ofi_params.json — the per-symbol parameter file consumed by
strategy/optimal_rate.py.

Two passes per symbol:

  1) OBI -> OU(beta, sigma)  via AR(1) MLE on consecutive signal_tick.obi
     dY = -beta Y dt + sigma dW
     phi_hat = Sum(Y_t Y_{t+1}) / Sum(Y_t^2)             (least-squares slope)
     sigma_eps^2 = (1/N) Sum (Y_{t+1} - phi_hat Y_t)^2   (residual variance)
     beta  = -ln(phi_hat) / dt
     sigma = sqrt( 2 beta sigma_eps^2 / (1 - phi_hat^2) )

  2) Realized slip -> eta via slope of |fill - expected|/expected on qty.
     Linear regression through origin: eta_hat = Sum(slip * qty) / Sum(qty^2).
     Fallback: median slip / median qty.

Stdlib only (json, math, statistics, argparse). No numpy required.

Usage:
  python3 scripts/calibrate_ofi_params.py
  python3 scripts/calibrate_ofi_params.py --logs logs/engine.jsonl logs/equities_engine.jsonl
  python3 scripts/calibrate_ofi_params.py --bar-dt 60 --out config/ofi_params.json
  python3 scripts/calibrate_ofi_params.py --dry-run    # print, do not write
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from statistics import median
from typing import Iterable


# ── Defaults that match what optimal_rate.OFIParams needs but the logs don't
# directly expose. The engine operator can tune these per environment.
KAPPA_DEFAULT = 0.0  # OFI toxicity penalty (0 = disabled until tuned)
LAM_DEFAULT = 0.0  # Running inventory risk (0 = rely on terminal p)
P_DEFAULT = 1.0  # Terminal-inventory penalty
GAMMA_DEFAULT_FRAC = 0.5  # gamma scaled from observed slip: gamma ~ slip / qty
ETA_FALLBACK = 1e-4  # per-qty OFI leakage when no slippage data found
BETA_FALLBACK = 0.05  # 1/sec; ~20s OFI half-life
SIGMA_FALLBACK = 0.10
MIN_OBI_SAMPLES = 60
MIN_SLIP_SAMPLES = 5


# ── Ingest ───────────────────────────────────────────────────────────────────
def iter_records(paths: Iterable[Path]) -> Iterable[dict]:
    for p in paths:
        if not p.exists():
            print(f"[warn] log not found: {p}", file=sys.stderr)
            continue
        with p.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def collect_obi_series(records: Iterable[dict]) -> dict[str, list[float]]:
    """Per-symbol time-ordered OBI series from signal_tick events."""
    series: dict[str, list[float]] = defaultdict(list)
    for rec in records:
        if rec.get("event") != "signal_tick":
            continue
        sym = rec.get("symbol")
        obi = rec.get("obi")
        if sym is None or obi is None:
            continue
        try:
            obi_f = float(obi)
        except (TypeError, ValueError):
            continue
        if math.isfinite(obi_f):
            series[sym].append(obi_f)
    return dict(series)


def collect_slippage_samples(
    records: Iterable[dict],
) -> dict[str, list[tuple[float, float]]]:
    """Per-symbol list of (qty, slip_ratio) from slippage + entry_signal cross-ref.

    The engine emits two log lines per fill:
      - entry_signal { symbol, qty, limit_px, ... }   (when order is constructed)
      - slippage     { symbol, expected, fill, pct }  (when fill is logged)

    We pair the most recent entry_signal with the next slippage event by symbol.
    Approximate; good enough for fitting eta on realized order-flow leakage.
    """
    pending_qty: dict[str, float] = {}
    samples: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for rec in records:
        sym = rec.get("symbol")
        if sym is None:
            continue
        ev = rec.get("event")
        if ev == "entry_signal":
            try:
                qty = float(rec.get("qty", 0.0))
            except (TypeError, ValueError):
                continue
            if qty > 0:
                pending_qty[sym] = qty
        elif ev == "slippage":
            try:
                expected = float(rec.get("expected", 0.0))
                fill = float(rec.get("fill", 0.0))
            except (TypeError, ValueError):
                continue
            if expected <= 0 or fill <= 0:
                continue
            slip = abs(fill - expected) / expected
            qty = pending_qty.pop(sym, None)
            if qty is None or qty <= 0:
                continue
            samples[sym].append((qty, slip))
    return dict(samples)


# ── Estimators ───────────────────────────────────────────────────────────────
def fit_ou_ar1(series: list[float], dt: float) -> tuple[float, float, int]:
    """OU MLE via AR(1). Returns (beta, sigma, n_used)."""
    n = len(series) - 1
    if n < MIN_OBI_SAMPLES:
        return BETA_FALLBACK, SIGMA_FALLBACK, n

    num = 0.0
    den = 0.0
    for y0, y1 in zip(series[:-1], series[1:]):
        num += y0 * y1
        den += y0 * y0
    if den <= 0.0:
        return BETA_FALLBACK, SIGMA_FALLBACK, n

    phi = num / den
    if phi <= 0.0 or phi >= 1.0:
        return BETA_FALLBACK, SIGMA_FALLBACK, n

    resid_sq = 0.0
    for y0, y1 in zip(series[:-1], series[1:]):
        e = y1 - phi * y0
        resid_sq += e * e
    sigma_eps2 = resid_sq / max(n, 1)

    beta = -math.log(phi) / dt
    var_continuous = 2.0 * beta * sigma_eps2 / max(1.0 - phi * phi, 1e-12)
    sigma = math.sqrt(max(var_continuous, 0.0))
    return beta, sigma, n


def fit_eta(samples: list[tuple[float, float]]) -> tuple[float, int]:
    """Linear regression of slip on qty through origin. Returns (eta, n_used)."""
    if len(samples) < MIN_SLIP_SAMPLES:
        if not samples:
            return ETA_FALLBACK, 0
        med_q = median(q for q, _ in samples) or 1.0
        med_s = median(s for _, s in samples)
        return max(med_s / med_q, 0.0), len(samples)

    num = sum(q * s for q, s in samples)
    den = sum(q * q for q, _ in samples)
    if den <= 0.0:
        return ETA_FALLBACK, len(samples)
    return max(num / den, 0.0), len(samples)


def fit_gamma(samples: list[tuple[float, float]]) -> tuple[float, int]:
    """Heuristic gamma from observed slip: assume slip ~ gamma * qty / price.

    With limited data we use a robust gamma proxy = median(slip / qty) * scale.
    The engine operator should refine this from execution backtests.
    """
    if not samples:
        return 1.0, 0
    ratios = [s / q for q, s in samples if q > 0]
    if not ratios:
        return 1.0, 0
    return max(median(ratios) * GAMMA_DEFAULT_FRAC, 1e-9), len(ratios)


# ── Driver ───────────────────────────────────────────────────────────────────
def calibrate(
    log_paths: list[Path],
    bar_dt: float,
) -> dict:
    records = list(iter_records(log_paths))
    obi_series = collect_obi_series(records)
    slip_samples = collect_slippage_samples(records)

    symbols = sorted(set(obi_series) | set(slip_samples))
    out: dict = {
        "_meta": {
            "bar_dt_seconds": bar_dt,
            "n_records": len(records),
            "n_symbols": len(symbols),
            "logs": [str(p) for p in log_paths],
            "version": 1,
        },
        "symbols": {},
    }

    for sym in symbols:
        series = obi_series.get(sym, [])
        samples = slip_samples.get(sym, [])

        beta, sigma, n_obi = fit_ou_ar1(series, bar_dt)
        eta, n_slip = fit_eta(samples)
        gamma, _ = fit_gamma(samples)

        out["symbols"][sym] = {
            "gamma": gamma,
            "beta": beta,
            "sigma": sigma,
            "eta": eta,
            "kappa": KAPPA_DEFAULT,
            "lam": LAM_DEFAULT,
            "p": P_DEFAULT,
            "_diag": {
                "n_obi_samples": n_obi,
                "n_slip_samples": n_slip,
                "obi_fallback_used": n_obi < MIN_OBI_SAMPLES,
                "slip_fallback_used": n_slip < MIN_SLIP_SAMPLES,
            },
        }

    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--logs",
        nargs="+",
        type=Path,
        default=[Path("logs/engine.jsonl"), Path("logs/equities_engine.jsonl")],
    )
    parser.add_argument(
        "--bar-dt",
        type=float,
        default=60.0,
        help="Seconds per bar in the input series (60 for crypto 1-min, 86400 for daily eq).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("config/ofi_params.json"),
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = calibrate(args.logs, args.bar_dt)

    if args.dry_run:
        json.dump(cfg, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, sort_keys=True)
        fh.write("\n")
    print(
        f"[calibrate] wrote {args.out} — "
        f"{cfg['_meta']['n_symbols']} symbols from {cfg['_meta']['n_records']} log records",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
