#!/usr/bin/env python3
"""
scripts/gate_d_eval.py — forward-soak attribution evaluator (Gate D).

Per the operator-approved Stage 3 design (docs/stage3_promotion_design.md):

  Per-symbol forward-soak attribution against pre-declared expectation bands.
  Excludes manual-cloid fills (Gate E intervention mask).

  Decisions:
    - PASS    : all symbols with ≥ N round-trips have median trip-PnL inside
                their band, AND aggregate PnL ≥ min_aggregate_pnl_usd
    - PENDING : insufficient samples — fewer than N round-trips on enough
                symbols, OR specific symbols flagged thin (PENDING per
                operator rule: thin samples do NOT count toward fail)
    - FAIL    : more than K symbols (with sufficient sample) outside band,
                OR aggregate PnL below min_aggregate_pnl_usd

Inputs:
  - config/expectation_bands.json   — declared per-symbol bands
  - logs/hl_engine.jsonl            — fill ledger
  - soak window (start, end)        — from CLI or band config

Output:
  - JSON decision summary on stdout (and optionally appended to a JSONL file)
  - Human-readable digest on stderr

This evaluator is read-only. It never edits config, never submits orders,
never mutates engine state.

Usage:
  scripts/gate_d_eval.py
  scripts/gate_d_eval.py --bands config/expectation_bands.json --out logs/gate_d.jsonl
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import sys
from pathlib import Path
from typing import Any

MANUAL_CLOID_PREFIX = "0xdead0001"


def _is_manual(cloid: str | None) -> bool:
    return bool(cloid) and str(cloid).startswith(MANUAL_CLOID_PREFIX)


def _collect_round_trips(
    jsonl_path: Path, start: str, end: str
) -> tuple[dict[str, list[float]], dict[str, dict[str, Any]]]:
    """Returns (per-symbol trip-PnL list, per-symbol counters).

    Trip-PnL = closed_pnl on each exit-leg fill (closed_pnl != 0).
    Manual-cloid fills are filtered out before either side counts.
    """
    by_sym: dict[str, list[float]] = {}
    counters: dict[str, dict[str, Any]] = {}
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
            sym = r.get("symbol") or r.get("coin") or "?"
            counters.setdefault(
                sym,
                {
                    "fills": 0,
                    "manual_excluded": 0,
                    "entry_legs": 0,
                    "exit_legs": 0,
                    "fees": 0.0,
                },
            )
            counters[sym]["fills"] += 1
            if _is_manual(r.get("cloid")):
                counters[sym]["manual_excluded"] += 1
                continue
            cp = r.get("closed_pnl")
            try:
                cp_f = float(cp) if cp is not None else 0.0
            except (TypeError, ValueError):
                cp_f = 0.0
            try:
                fee = float(r.get("fee") or 0.0)
            except (TypeError, ValueError):
                fee = 0.0
            counters[sym]["fees"] += fee
            if cp_f == 0.0:
                counters[sym]["entry_legs"] += 1
            else:
                counters[sym]["exit_legs"] += 1
                by_sym.setdefault(sym, []).append(cp_f)
    return by_sym, counters


def _classify_symbol(
    sym: str, trips: list[float], band_cfg: dict[str, Any], n_min: int
) -> dict[str, Any]:
    n = len(trips)
    if n < n_min:
        return {
            "symbol": sym,
            "status": "pending_thin_sample",
            "n_round_trips": n,
            "median_trip_pnl": round(statistics.median(trips), 4) if trips else None,
            "band": band_cfg.get("pnl_per_trip_usd"),
            "in_band": None,
            "band_source": band_cfg.get("band_source"),
        }
    median = statistics.median(trips)
    band = band_cfg.get("pnl_per_trip_usd") or [None, None]
    low, high = band
    in_band = low is not None and high is not None and low <= median <= high
    return {
        "symbol": sym,
        "status": "in_band" if in_band else "outlier",
        "n_round_trips": n,
        "median_trip_pnl": round(median, 4),
        "band": band,
        "in_band": in_band,
        "band_source": band_cfg.get("band_source"),
    }


def evaluate(
    bands_path: Path,
    jsonl_path: Path,
    soak_start: str | None,
    soak_end: str | None,
) -> dict[str, Any]:
    bands_payload = json.loads(bands_path.read_text())
    bands = bands_payload.get("bands", {})
    n_min = int(bands_payload.get("min_round_trips_per_symbol", 3))
    k_max = int(bands_payload.get("max_outlier_symbols", 2))
    min_agg = float(bands_payload.get("min_aggregate_pnl_usd", -37.50))
    win = bands_payload.get("soak_window", {})
    start = soak_start or win.get("start") or "0000-00-00T00:00:00Z"
    end = soak_end or win.get("end") or "9999-12-31T23:59:59Z"

    trips_by_sym, counters = _collect_round_trips(jsonl_path, start, end)
    classifications: list[dict[str, Any]] = []
    in_band: list[str] = []
    outliers: list[str] = []
    pending: list[str] = []
    untracked_with_trips: list[str] = []

    for sym, trips in trips_by_sym.items():
        if sym not in bands:
            untracked_with_trips.append(sym)
            continue
        c = _classify_symbol(sym, trips, bands[sym], n_min)
        classifications.append(c)
        if c["status"] == "pending_thin_sample":
            pending.append(sym)
        elif c["in_band"]:
            in_band.append(sym)
        else:
            outliers.append(sym)

    aggregate_pnl = sum(sum(v) for v in trips_by_sym.values())
    aggregate_floor_breach = aggregate_pnl < min_agg
    n_outliers = len(outliers)
    outlier_breach = n_outliers > k_max
    # Minimum number of evaluable (non-pending) symbols required to call PASS.
    # Without this guard, "1 symbol in_band, 19 pending" returns PASS by the
    # letter of the rule but is a weak signal. Default 3 — operator can override.
    min_evaluable = int(bands_payload.get("min_evaluable_symbols_for_pass", 3))
    n_evaluable = len(in_band) + n_outliers
    insufficient_evaluable = n_evaluable < min_evaluable

    if outlier_breach or aggregate_floor_breach:
        decision = "fail"
    elif insufficient_evaluable:
        decision = "pending"
    elif n_outliers == 0 and len(in_band) >= 1:
        decision = "pass"
    else:
        decision = "pending"

    return {
        "event": "gate_d_evaluation",
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "decision": decision,
        "soak_window": {"start": start, "end": end},
        "thresholds": {
            "min_round_trips_per_symbol": n_min,
            "max_outlier_symbols": k_max,
            "min_aggregate_pnl_usd": min_agg,
            "min_evaluable_symbols_for_pass": min_evaluable,
        },
        "summary": {
            "n_symbols_evaluated": len(classifications),
            "n_in_band": len(in_band),
            "n_outliers": n_outliers,
            "n_pending_thin_sample": len(pending),
            "n_untracked_with_trips": len(untracked_with_trips),
            "aggregate_pnl": round(aggregate_pnl, 4),
            "aggregate_floor_breach": aggregate_floor_breach,
            "outlier_count_breach": outlier_breach,
            "insufficient_evaluable_symbols": insufficient_evaluable,
        },
        "in_band_symbols": in_band,
        "outlier_symbols": outliers,
        "pending_symbols": pending,
        "untracked_symbols_with_trips": untracked_with_trips,
        "classifications": classifications,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Gate D forward-soak evaluator")
    ap.add_argument("--bands", default="config/expectation_bands.json")
    ap.add_argument("--jsonl", default="logs/hl_engine.jsonl")
    ap.add_argument(
        "--soak-start",
        default=None,
        help="Override soak start (default: from bands config)",
    )
    ap.add_argument(
        "--soak-end",
        default=None,
        help="Override soak end (default: from bands config)",
    )
    ap.add_argument(
        "--out", default=None, help="Append decision JSON to this file (also stdout)"
    )
    ap.add_argument(
        "--summary-only",
        action="store_true",
        help="Print only summary fields, not per-symbol classifications",
    )
    args = ap.parse_args()

    bands_path = Path(args.bands)
    jsonl_path = Path(args.jsonl)
    if not bands_path.exists():
        print(f"gate_d_eval: missing bands {bands_path}", file=sys.stderr)
        return 2
    if not jsonl_path.exists():
        print(f"gate_d_eval: missing jsonl {jsonl_path}", file=sys.stderr)
        return 2

    result = evaluate(bands_path, jsonl_path, args.soak_start, args.soak_end)

    if args.summary_only:
        out = {k: v for k, v in result.items() if k != "classifications"}
    else:
        out = result
    body = json.dumps(out, indent=2)
    print(body)

    if args.out:
        op = Path(args.out)
        op.parent.mkdir(parents=True, exist_ok=True)
        with op.open("a") as f:
            f.write(json.dumps(out) + "\n")

    s = result["summary"]
    print(
        f"gate_d_eval: decision={result['decision']}  "
        f"in_band={s['n_in_band']}  outliers={s['n_outliers']}  "
        f"pending={s['n_pending_thin_sample']}  "
        f"agg_pnl=${s['aggregate_pnl']:.2f}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
