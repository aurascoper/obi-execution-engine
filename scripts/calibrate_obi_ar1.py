#!/usr/bin/env python3
"""scripts/calibrate_obi_ar1.py — fit AR(1) parameters to live signal_tick.obi.

Reads `event=signal_tick` records from logs/hl_engine.jsonl, resamples each
symbol's OBI series to a regular dt grid (10s by default), fits an AR(1)
process per symbol, and writes the pooled parameters to config/obi_ar1.json.

AR(1) form (used by math_core/fill_model.py):

    y_t = (1 − φ) · μ + φ · y_{t−1} + ε,    ε ~ N(0, σ²)

Calibration:
  1. parse signal_tick events from the log
  2. per symbol, build a (t, obi) series; require ≥ N_min samples
  3. resample to a regular dt-second grid via forward-fill
  4. AR(1) regression: y_t = a + b · y_{t−1}; φ = b, μ = a / (1 − b),
     σ = std of regression residuals
  5. pool per-symbol estimates (median for φ, μ, σ; max-abs observed
     for clip)

Output JSON shape:
  {
    "kind": "obi_ar1_calibration",
    "git_sha": "...",
    "timestamp_utc": "...",
    "source_log": "logs/hl_engine.jsonl",
    "dt_s": 10,
    "n_symbols_total": int,
    "n_symbols_used": int,
    "n_samples_min": int,
    "pooled": {"phi": ..., "mu": ..., "sigma": ..., "clip": ...},
    "per_symbol": {symbol: {"phi": ..., "mu": ..., "sigma": ..., "clip": ..., "n": int}}
  }

This script is read-only against the log and write-only into config/. It
does not touch any live path.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception:
        return "unknown"


def _parse_iso(ts: str) -> float:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def load_signal_ticks(log_path: Path) -> dict[str, list[tuple[float, float]]]:
    """Return {symbol: [(t_seconds, obi), ...]} for each signal_tick row."""
    by_sym: dict[str, list[tuple[float, float]]] = {}
    with log_path.open() as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("event") != "signal_tick":
                continue
            obi = d.get("obi")
            sym = d.get("symbol")
            ts = d.get("timestamp")
            if obi is None or sym is None or ts is None:
                continue
            try:
                t = _parse_iso(ts)
            except Exception:
                continue
            by_sym.setdefault(sym, []).append((t, float(obi)))
    for sym in by_sym:
        by_sym[sym].sort()
    return by_sym


def resample_forward_fill(series: list[tuple[float, float]], dt_s: float) -> list[float]:
    """Resample a sparse (t, obi) series to a regular dt grid via forward-fill."""
    if len(series) < 2:
        return []
    t0 = series[0][0]
    t_end = series[-1][0]
    n_steps = int(math.floor((t_end - t0) / dt_s)) + 1
    if n_steps <= 1:
        return []
    out: list[float] = []
    j = 0
    last_obi = series[0][1]
    for k in range(n_steps):
        target_t = t0 + k * dt_s
        while j + 1 < len(series) and series[j + 1][0] <= target_t:
            j += 1
            last_obi = series[j][1]
        out.append(last_obi)
    return out


def fit_ar1(y: list[float]) -> dict | None:
    """OLS y_t = a + b·y_{t−1} + ε. Returns φ, μ, σ, n, clip or None."""
    if len(y) < 30:
        return None
    x = y[:-1]
    z = y[1:]
    n = len(x)
    mx = sum(x) / n
    mz = sum(z) / n
    cov = sum((xi - mx) * (zi - mz) for xi, zi in zip(x, z)) / n
    var = sum((xi - mx) ** 2 for xi in x) / n
    if var <= 1e-12:
        return None
    b = cov / var
    a = mz - b * mx
    residuals = [zi - (a + b * xi) for xi, zi in zip(x, z)]
    sigma = (sum(r * r for r in residuals) / max(1, n - 2)) ** 0.5
    if abs(1.0 - b) < 1e-9:
        mu = mz
    else:
        mu = a / (1.0 - b)
    return {
        "phi": b,
        "mu": mu,
        "sigma": sigma,
        "n": n,
        "clip": max(abs(min(y)), abs(max(y))),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", type=Path, default=ROOT / "logs/hl_engine.jsonl")
    ap.add_argument("--out", type=Path, default=ROOT / "config/obi_ar1.json")
    ap.add_argument("--dt-s", type=float, default=10.0)
    ap.add_argument("--n-samples-min", type=int, default=200)
    args = ap.parse_args()

    print(f"Reading signal_tick events from {args.log}...")
    by_sym = load_signal_ticks(args.log)
    print(f"  found {len(by_sym)} symbols, "
          f"{sum(len(v) for v in by_sym.values())} total ticks")

    per_symbol: dict[str, dict] = {}
    for sym, series in sorted(by_sym.items()):
        if len(series) < args.n_samples_min:
            continue
        y = resample_forward_fill(series, args.dt_s)
        fit = fit_ar1(y)
        if fit is None:
            continue
        per_symbol[sym] = fit

    if not per_symbol:
        print("ERROR: no symbol cleared the calibration sample bar.")
        return 1

    phis = [v["phi"] for v in per_symbol.values()]
    mus = [v["mu"] for v in per_symbol.values()]
    sigmas = [v["sigma"] for v in per_symbol.values()]
    clips = [v["clip"] for v in per_symbol.values()]
    pooled = {
        "phi": statistics.median(phis),
        "mu": statistics.median(mus),
        "sigma": statistics.median(sigmas),
        "clip": min(0.999, max(clips)),
    }

    result = {
        "kind": "obi_ar1_calibration",
        "git_sha": _git_sha(),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "source_log": str(args.log.relative_to(ROOT)),
        "dt_s": args.dt_s,
        "n_samples_min": args.n_samples_min,
        "n_symbols_total": len(by_sym),
        "n_symbols_used": len(per_symbol),
        "pooled": pooled,
        "pooled_method": "median across per-symbol fits, clip = max-abs observed",
        "per_symbol": per_symbol,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2))
    print(f"\nCalibration written: {args.out}")
    print(f"  symbols used: {len(per_symbol)} / {len(by_sym)}")
    print(f"  pooled phi   = {pooled['phi']:+.4f}")
    print(f"  pooled mu    = {pooled['mu']:+.4f}")
    print(f"  pooled sigma = {pooled['sigma']:+.4f}")
    print(f"  clip         = {pooled['clip']:+.4f}")

    p_lo, p_hi = min(phis), max(phis)
    s_lo, s_hi = min(sigmas), max(sigmas)
    m_lo, m_hi = min(mus), max(mus)
    print(f"\n  per-symbol phi range:   [{p_lo:+.4f}, {p_hi:+.4f}]")
    print(f"  per-symbol mu range:    [{m_lo:+.4f}, {m_hi:+.4f}]")
    print(f"  per-symbol sigma range: [{s_lo:+.4f}, {s_hi:+.4f}]")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
