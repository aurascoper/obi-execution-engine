#!/usr/bin/env python3
"""
scripts/derive_expectation_bands.py — Stage 3 per-symbol expectation bands.

Per the operator-approved Stage 3 design (docs/stage3_promotion_design.md):
  - **Source priority**: Stage 2.5 live data first, replay calibration fallback.
    Replay was previously documented as ceiling-limited
    (project_mode2_session_policy_ceiling), so live is the cleaner anchor.
  - For each symbol, derive [pnl_low, pnl_high] = median ± 1.5 × IQR,
    then scale by sizing_ratio = stage_3_notional / stage_2_5_notional.
  - Symbols with insufficient samples fall through to class-default bands
    (HIP-3 vs native) and are flagged `band_source: default_class` for
    operator override.

Live ledger source: hl_engine.jsonl, soak window, hl_z tagged fills,
manual-cloid fills excluded (Gate E mask).

Read-only otherwise. Writes a draft `config/expectation_bands.json` for
operator review. Does NOT activate the bands or modify any engine state.

Usage:
  scripts/derive_expectation_bands.py \
      --soak-start 2026-04-28T19:35:07Z \
      --soak-end   2026-04-28T23:48:25Z \
      --stage-3-notional 75 \
      --stage-2-notional 50 \
      --out config/expectation_bands.json

  scripts/derive_expectation_bands.py --auto         # uses standard Stage 2.5
                                                     # window from soak_state

Universe coverage:
  --universe-file config/stage3_universe.json (default) — symbols listed in
  the universe but with no live trades in the soak window get a class-default
  band (HIP-3 vs native). This ensures Gate D can evaluate any symbol that
  *might* trade in Stage 3, not only ones that traded in Stage 2.5.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import sys
from pathlib import Path
from typing import Any

# Manual-cloid prefix per scripts/lib/manual_order.py.
MANUAL_CLOID_PREFIX = "0xdead0001"

# Class-default fallback bands (Stage 2 notional, $50). These are deliberately
# wide; operator should review and tighten on the symbols that matter.
# Asymmetric: floor is loss-aware (60% of fee+slippage budget); ceiling is
# realistic (small mean-reversion edge).
DEFAULT_BAND_HIP3: tuple[float, float] = (-0.40, 0.20)
DEFAULT_BAND_NATIVE: tuple[float, float] = (-0.50, 0.30)

# Sample-size thresholds.
N_BAND_SAMPLES = 5  # use live source if ≥ N_BAND_SAMPLES round-trips
N_THIN_SAMPLES = 2  # 2-4 round-trips → "thin live", widened band

# Default sizing ratio (Stage 3 minimum).
DEFAULT_STAGE_3_NOTIONAL = 75.0
DEFAULT_STAGE_2_NOTIONAL = 50.0


def _is_hip3(sym: str) -> bool:
    return ":" in (sym or "")


def _is_manual(cloid: str | None) -> bool:
    return bool(cloid) and str(cloid).startswith(MANUAL_CLOID_PREFIX)


def _round_trip_pnls(jsonl_path: Path, start: str, end: str) -> dict[str, list[float]]:
    """Per-symbol list of round-trip P&Ls (closed_pnl values from exit fills,
    where exit-leg is identified by closed_pnl != 0). Manual cloids excluded."""
    by_sym: dict[str, list[float]] = {}
    with jsonl_path.open() as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            ts = r.get("timestamp", "")
            if not (start <= ts <= end):
                continue
            if r.get("event") != "hl_fill_received":
                continue
            if _is_manual(r.get("cloid")):
                continue
            cp = r.get("closed_pnl")
            if cp is None:
                continue
            try:
                cp_f = float(cp)
            except (TypeError, ValueError):
                continue
            if cp_f == 0.0:
                # entry-leg fill (closed_pnl == 0); not a completed trip
                continue
            sym = r.get("symbol") or r.get("coin") or "?"
            by_sym.setdefault(sym, []).append(cp_f)
    return by_sym


def _band_from_live(
    values: list[float], sizing_ratio: float
) -> tuple[float, float, str]:
    n = len(values)
    if n >= N_BAND_SAMPLES:
        med = statistics.median(values)
        try:
            q = statistics.quantiles(values, n=4)
            iqr = q[2] - q[0]
        except statistics.StatisticsError:
            iqr = max(values) - min(values)
        if iqr == 0:
            iqr = max(0.05, abs(med) * 0.5)
        low = med - 1.5 * iqr
        high = med + 1.5 * iqr
        return low * sizing_ratio, high * sizing_ratio, "live_full"
    if n >= N_THIN_SAMPLES:
        # Use observed min/max with a small symmetric pad. Wider than full-IQR
        # bands deliberately — under-sampled tails get the benefit of the
        # doubt at Stage 3.
        lo, hi = min(values), max(values)
        rng = max(hi - lo, abs(statistics.median(values)) * 0.5, 0.10)
        med = statistics.median(values)
        return (med - rng) * sizing_ratio, (med + rng) * sizing_ratio, "live_thin"
    if n == 1:
        v = values[0]
        rng = max(abs(v) * 1.0, 0.20)
        return (v - rng) * sizing_ratio, (v + rng) * sizing_ratio, "live_single"
    return 0.0, 0.0, "no_data"


def _default_band(sym: str, sizing_ratio: float) -> tuple[float, float, str]:
    base = DEFAULT_BAND_HIP3 if _is_hip3(sym) else DEFAULT_BAND_NATIVE
    return base[0] * sizing_ratio, base[1] * sizing_ratio, "default_class"


def derive_bands(
    live_pnls: dict[str, list[float]],
    sizing_ratio: float,
    universe: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    bands: dict[str, dict[str, Any]] = {}
    syms_iter = list(live_pnls.keys())
    if universe:
        for u in universe:
            if u not in syms_iter:
                syms_iter.append(u)
    for sym in syms_iter:
        values = live_pnls.get(sym, [])
        if values:
            low, high, source = _band_from_live(values, sizing_ratio)
        else:
            low, high, source = _default_band(sym, sizing_ratio)
        bands[sym] = {
            "pnl_per_trip_usd": [round(low, 4), round(high, 4)],
            "n_live_samples": len(values),
            "live_median": round(statistics.median(values), 4) if values else None,
            "band_source": source,
        }
    return bands


def main() -> int:
    ap = argparse.ArgumentParser(description="Derive Stage 3 expectation bands")
    ap.add_argument(
        "--soak-start",
        default="2026-04-28T19:35:07Z",
        help="Soak window start (default: 2026-04-28 boot)",
    )
    ap.add_argument(
        "--soak-end",
        default="2026-04-28T23:48:25Z",
        help="Soak window end (default: report-time snapshot)",
    )
    ap.add_argument(
        "--stage-3-notional",
        type=float,
        default=DEFAULT_STAGE_3_NOTIONAL,
        help="Stage 3 per-trade notional ($, default 75)",
    )
    ap.add_argument(
        "--stage-2-notional",
        type=float,
        default=DEFAULT_STAGE_2_NOTIONAL,
        help="Stage 2.5 per-trade notional ($, default 50)",
    )
    ap.add_argument("--jsonl", default="logs/hl_engine.jsonl", help="Engine log path")
    ap.add_argument(
        "--out", default="config/expectation_bands.json", help="Output band config path"
    )
    ap.add_argument(
        "--max-outliers",
        type=int,
        default=2,
        help="Gate D max-outlier count (carried into config)",
    )
    ap.add_argument(
        "--min-round-trips",
        type=int,
        default=3,
        help="Gate D min round-trips per symbol (carried into config)",
    )
    ap.add_argument(
        "--min-aggregate-pnl-usd",
        type=float,
        default=-37.50,
        help="Aggregate floor for Gate D (default -$37.50, half loss-guard at Stage 2)",
    )
    ap.add_argument(
        "--universe-file",
        default="config/stage3_universe.json",
        help="JSON file declaring the Stage 3 universe; symbols without "
        "live data fall through to default-class bands (default: "
        "config/stage3_universe.json)",
    )
    args = ap.parse_args()

    jsonl_path = Path(args.jsonl)
    if not jsonl_path.exists():
        print(f"derive_expectation_bands: missing {jsonl_path}", file=sys.stderr)
        return 2

    sizing_ratio = args.stage_3_notional / args.stage_2_notional

    # Load declared Stage 3 universe so symbols with zero live trades still
    # get a default-class band (otherwise they're silently absent from Gate D).
    universe: list[str] = []
    universe_path = Path(args.universe_file)
    if universe_path.exists():
        try:
            uni_payload = json.loads(universe_path.read_text())
            for k in ("native", "hip3"):
                vals = uni_payload.get(k, [])
                if isinstance(vals, list):
                    universe.extend(str(v) for v in vals)
        except Exception as e:
            print(
                f"derive_expectation_bands: warning — could not parse {universe_path}: {e}",
                file=sys.stderr,
            )
    else:
        print(
            f"derive_expectation_bands: warning — universe file not found at {universe_path}; "
            "default-class bands will not be generated for non-traded symbols.",
            file=sys.stderr,
        )

    live_pnls = _round_trip_pnls(jsonl_path, args.soak_start, args.soak_end)
    bands = derive_bands(live_pnls, sizing_ratio, universe=universe or None)

    payload = {
        "schema_version": 1,
        "declared_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "stage": 3,
        "sizing_ratio": sizing_ratio,
        "stage_3_notional_usd": args.stage_3_notional,
        "stage_2_notional_usd": args.stage_2_notional,
        "min_round_trips_per_symbol": args.min_round_trips,
        "max_outlier_symbols": args.max_outliers,
        "min_aggregate_pnl_usd": args.min_aggregate_pnl_usd,
        "soak_window": {"start": args.soak_start, "end": args.soak_end},
        "universe_file": str(universe_path),
        "n_universe_declared": len(universe),
        "live_source_threshold_n": N_BAND_SAMPLES,
        "default_class_bands": {
            "hip3": {"pnl_per_trip_usd": list(DEFAULT_BAND_HIP3)},
            "native": {"pnl_per_trip_usd": list(DEFAULT_BAND_NATIVE)},
        },
        "bands": bands,
        "_provenance": {
            "live_symbols_with_data": len(live_pnls),
            "live_symbols_with_full_sample": sum(
                1 for v in live_pnls.values() if len(v) >= N_BAND_SAMPLES
            ),
            "live_symbols_with_thin_sample": sum(
                1
                for v in live_pnls.values()
                if N_THIN_SAMPLES <= len(v) < N_BAND_SAMPLES
            ),
            "live_symbols_with_single_sample": sum(
                1 for v in live_pnls.values() if len(v) == 1
            ),
            "default_only_symbols": [
                s for s, v in bands.items() if v["band_source"] == "default_class"
            ],
        },
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n")

    n = len(bands)
    sources = {}
    for v in bands.values():
        sources[v["band_source"]] = sources.get(v["band_source"], 0) + 1
    print(f"derive_expectation_bands: wrote {out_path}", file=sys.stderr)
    print(f"  symbols={n}  sizing_ratio={sizing_ratio:.2f}", file=sys.stderr)
    print(f"  band_source breakdown: {sources}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
