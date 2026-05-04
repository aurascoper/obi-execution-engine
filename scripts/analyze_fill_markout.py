"""scripts/analyze_fill_markout.py — Per-fill markout by quote-stability bucket.

Sibling to scripts/analyze_quoter_shadow.py. Observation-only Phase B
diagnostic. Reads logs/hl_engine.jsonl, joins fill_observation events
with the mid time series the engine already logs in quoter_shadow events,
and reports markout distributions:

  - by stability_bucket
  - by symbol × stability_bucket
  - by tod_bucket × stability_bucket
  - by raw flag combination (multi-hot, preserves overlap info)

Each table is dual-reported (all fills + maker-only-estimate) and gated
by a minimum-sample guard. Stable-bucket vs whole-population markout is
printed as a baseline banner so a human can spot join-logic drift fast.

Usage:
  venv/bin/python3 scripts/analyze_fill_markout.py [path/to/log.jsonl]

Exit codes:
  0  — analyzer ran cleanly
  1  — log unreadable / no fill_observation events found
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


SCHEMA_VERSION = 1
DEFAULT_LOG = Path("logs/hl_engine.jsonl")
DEFAULT_OUT = Path("autoresearch_gated/fill_markout_by_stability_distributions.json")
MARKOUT_HORIZONS_S = (1.0, 5.0, 15.0, 60.0)
MIN_SAMPLE_TOPLEVEL = 30
MIN_SAMPLE_PER_CELL = 10
BUCKETS = ("stable", "unstable_flip", "unstable_withdrawal", "unstable_widening")
FLAG_COMBOS = (
    "none",
    "widening_only",
    "withdrawal_only",
    "flip_only",
    "widening+withdrawal",
    "widening+flip",
    "withdrawal+flip",
    "all_three",
)


# ── Time + bucket helpers (reused idioms) ───────────────────────────────────

def _parse_iso(s: str) -> Optional[float]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def _tod_bucket(t_s: float) -> str:
    """Reused from analyze_diagnostic_cohorts.py — UTC 6-hour banding."""
    hr = datetime.fromtimestamp(t_s, tz=timezone.utc).hour
    if hr < 6:
        return "00-06_UTC_asia"
    if hr < 12:
        return "06-12_UTC_eu"
    if hr < 18:
        return "12-18_UTC_us_open"
    return "18-24_UTC_us_close"


def _lookup_future_mid(
    series: list[tuple[float, float]], t_fill: float, horizon_s: float
) -> Optional[float]:
    """Reused from analyze_quoter_shadow.py:123-137 — linear interpolation."""
    target = t_fill + horizon_s
    if not series or target < series[0][0] or target > series[-1][0]:
        return None
    for i in range(len(series) - 1):
        t_a, p_a = series[i]
        t_b, p_b = series[i + 1]
        if t_a <= target <= t_b:
            if t_b == t_a:
                return p_a
            w = (target - t_a) / (t_b - t_a)
            return p_a + w * (p_b - p_a)
    return None


def _flag_combo_label(fw: bool, fwd: bool, ffl: bool) -> str:
    n = int(fw) + int(fwd) + int(ffl)
    if n == 0:
        return "none"
    if n == 3:
        return "all_three"
    if n == 1:
        if fw:
            return "widening_only"
        if fwd:
            return "withdrawal_only"
        return "flip_only"
    # n == 2
    if fw and fwd:
        return "widening+withdrawal"
    if fw and ffl:
        return "widening+flip"
    return "withdrawal+flip"


# ── Log loader ──────────────────────────────────────────────────────────────

def load_log(log_path: Path) -> tuple[list[dict], dict[str, list[tuple[float, float]]]]:
    """Return (fill_observation events with parsed _t, mid_by_sym from quoter_shadow)."""
    fills: list[dict] = []
    mid_by_sym: dict[str, list[tuple[float, float]]] = defaultdict(list)
    with log_path.open() as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            ev = d.get("event")
            ts = _parse_iso(d.get("timestamp", ""))
            if ts is None:
                continue
            sym = d.get("symbol")
            if ev == "fill_observation":
                d["_t"] = ts
                fills.append(d)
            elif ev == "quoter_shadow" and sym is not None:
                mid = d.get("mid")
                if mid is not None:
                    try:
                        mid_by_sym[sym].append((ts, float(mid)))
                    except (TypeError, ValueError):
                        pass
    for s in mid_by_sym:
        mid_by_sym[s].sort()
    return fills, mid_by_sym


# ── Markout stat aggregation ────────────────────────────────────────────────

def _stat_block(values: list[float], min_n: int) -> dict[str, Any]:
    n = len(values)
    if n < min_n:
        return {"label": "insufficient_sample", "n": n}
    s = sorted(values)

    def pct(p: float) -> float:
        if not s:
            return 0.0
        idx = max(0, min(len(s) - 1, int(round(p * (len(s) - 1)))))
        return s[idx]

    return {
        "n": n,
        "mean": round(statistics.fmean(values), 6),
        "p10": round(pct(0.10), 6),
        "p50": round(pct(0.50), 6),
        "p90": round(pct(0.90), 6),
    }


def _markout_bp(side: str, fill_px: float, future_mid: float) -> float:
    """Markout sign convention (matches math_core/fill_model.resolve_markout):
    positive = good for our side. SELL: profit if future_mid < fill_px.
    BUY:  profit if future_mid > fill_px. Returns basis points.
    """
    if fill_px <= 0:
        return 0.0
    if side == "sell":
        return (fill_px - future_mid) / fill_px * 10_000.0
    return (future_mid - fill_px) / fill_px * 10_000.0


# ── Aggregation core ────────────────────────────────────────────────────────

def aggregate(
    fills: list[dict], mid_by_sym: dict[str, list[tuple[float, float]]]
) -> dict[str, Any]:
    """Compute all the tables. Returns the JSON-serializable result dict."""
    missing = {
        "missing_fill_px": 0,
        "missing_fill_ts": 0,
        "missing_submit_ts": 0,
        "missing_mid_lookup_at_horizon": 0,
    }

    # markouts_by_horizon: for each fill, dict {horizon_s: bp_value or None}
    enriched: list[dict] = []
    for f in fills:
        fill_px = f.get("fill_px")
        fill_ts = f.get("fill_ts") or f.get("_t")
        side = (f.get("side") or "").lower()
        sym = f.get("symbol") or ""
        if fill_px is None:
            missing["missing_fill_px"] += 1
            continue
        if fill_ts is None:
            missing["missing_fill_ts"] += 1
            continue
        if f.get("submit_ts") is None:
            missing["missing_submit_ts"] += 1
            # not a hard skip; quote_age stats just lose this row
        series = mid_by_sym.get(sym, [])
        markouts = {}
        for h in MARKOUT_HORIZONS_S:
            fm = _lookup_future_mid(series, float(fill_ts), h)
            if fm is None:
                missing["missing_mid_lookup_at_horizon"] += 1
                markouts[h] = None
            else:
                markouts[h] = _markout_bp(side, float(fill_px), fm)
        enriched.append({**f, "_markouts": markouts, "_fill_ts_used": float(fill_ts)})

    # ── Tables ──
    def collect_buckets(filter_fn) -> dict[str, dict[str, dict[str, Any]]]:
        out: dict[str, dict[str, dict[str, Any]]] = {}
        for h in MARKOUT_HORIZONS_S:
            cells: dict[str, list[float]] = defaultdict(list)
            for f in enriched:
                if not filter_fn(f):
                    continue
                bk = f.get("stability_bucket") or "stable"
                v = f["_markouts"].get(h)
                if v is not None:
                    cells[bk].append(v)
            out[f"+{int(h)}s"] = {
                bk: _stat_block(cells.get(bk, []), MIN_SAMPLE_TOPLEVEL)
                for bk in BUCKETS
            }
        return out

    def collect_2d(
        key_fn, filter_fn, min_n: int
    ) -> dict[str, dict[str, dict[str, dict[str, Any]]]]:
        # outer: horizon -> primary_key -> bucket -> stat
        out: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
        for h in MARKOUT_HORIZONS_S:
            cells: dict[tuple[str, str], list[float]] = defaultdict(list)
            for f in enriched:
                if not filter_fn(f):
                    continue
                k = key_fn(f)
                bk = f.get("stability_bucket") or "stable"
                v = f["_markouts"].get(h)
                if v is not None:
                    cells[(k, bk)].append(v)
            outer: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
            for (k, bk), vs in cells.items():
                outer[k][bk] = _stat_block(vs, min_n)
            out[f"+{int(h)}s"] = dict(outer)
        return out

    def collect_flag_combo(filter_fn) -> dict[str, dict[str, dict[str, Any]]]:
        out: dict[str, dict[str, dict[str, Any]]] = {}
        for h in MARKOUT_HORIZONS_S:
            cells: dict[str, list[float]] = defaultdict(list)
            for f in enriched:
                if not filter_fn(f):
                    continue
                combo = _flag_combo_label(
                    bool(f.get("flag_widening")),
                    bool(f.get("flag_withdrawal")),
                    bool(f.get("flag_flip")),
                )
                v = f["_markouts"].get(h)
                if v is not None:
                    cells[combo].append(v)
            out[f"+{int(h)}s"] = {
                combo: _stat_block(cells.get(combo, []), MIN_SAMPLE_PER_CELL)
                for combo in FLAG_COMBOS
            }
        return out

    all_filter = lambda f: True  # noqa: E731
    maker_filter = lambda f: bool(f.get("is_maker_estimate"))  # noqa: E731

    by_bucket_all = collect_buckets(all_filter)
    by_bucket_maker = collect_buckets(maker_filter)
    by_symbol_all = collect_2d(lambda f: f.get("symbol") or "", all_filter, MIN_SAMPLE_PER_CELL)
    by_symbol_maker = collect_2d(lambda f: f.get("symbol") or "", maker_filter, MIN_SAMPLE_PER_CELL)
    by_tod_all = collect_2d(
        lambda f: _tod_bucket(f["_fill_ts_used"]), all_filter, MIN_SAMPLE_PER_CELL
    )
    by_tod_maker = collect_2d(
        lambda f: _tod_bucket(f["_fill_ts_used"]), maker_filter, MIN_SAMPLE_PER_CELL
    )
    by_flag_combo_all = collect_flag_combo(all_filter)
    by_flag_combo_maker = collect_flag_combo(maker_filter)

    # Baseline banner: stable-bucket markout vs whole-population markout
    def pop_mean(filter_fn, h: float) -> Optional[float]:
        vs = [
            f["_markouts"][h]
            for f in enriched
            if filter_fn(f) and f["_markouts"].get(h) is not None
        ]
        return round(statistics.fmean(vs), 6) if vs else None

    def stable_mean(table: dict, key: str) -> Optional[float]:
        cell = table.get(key, {}).get("stable", {})
        return cell.get("mean") if isinstance(cell, dict) else None

    baseline = {}
    for h in (5.0, 15.0):
        key = f"+{int(h)}s"
        pm_all = pop_mean(all_filter, h)
        sm_all = stable_mean(by_bucket_all, key)
        baseline[f"stable_vs_population_delta_{int(h)}s_bp_all"] = (
            round(sm_all - pm_all, 6) if (sm_all is not None and pm_all is not None) else None
        )
        pm_mk = pop_mean(maker_filter, h)
        sm_mk = stable_mean(by_bucket_maker, key)
        baseline[f"stable_vs_population_delta_{int(h)}s_bp_maker_only"] = (
            round(sm_mk - pm_mk, 6) if (sm_mk is not None and pm_mk is not None) else None
        )

    # Quote age summary (separate from markout, uses different exclusion)
    qages = [f.get("quote_age_ms") for f in fills if f.get("quote_age_ms") is not None]
    qage_block = _stat_block([float(q) for q in qages], min_n=MIN_SAMPLE_PER_CELL)

    return {
        "n_fills_total": len(fills),
        "n_fills_with_markout_any": len(enriched),
        "n_fills_maker_only_estimate": sum(1 for f in fills if f.get("is_maker_estimate")),
        "n_fills_shadow": sum(1 for f in fills if f.get("is_shadow")),
        "missing_context": missing,
        **baseline,
        "quote_age_ms": qage_block,
        "markout_by_bucket": {
            "all": by_bucket_all,
            "maker_only_estimate": by_bucket_maker,
        },
        "markout_by_symbol_x_bucket": {
            "all": by_symbol_all,
            "maker_only_estimate": by_symbol_maker,
        },
        "markout_by_tod_x_bucket": {
            "all": by_tod_all,
            "maker_only_estimate": by_tod_maker,
        },
        "markout_by_flag_combo": {
            "all": by_flag_combo_all,
            "maker_only_estimate": by_flag_combo_maker,
        },
    }


# ── Stdout reporting ────────────────────────────────────────────────────────

def _fmt_cell(c: dict[str, Any]) -> str:
    if "label" in c:
        return f"{c['label']}(n={c['n']})"
    return f"n={c['n']:>4}  μ={c['mean']:+.2f}  p10={c['p10']:+.2f}  p50={c['p50']:+.2f}  p90={c['p90']:+.2f}"


def _print_bucket_table(title: str, table: dict[str, dict[str, dict[str, Any]]]) -> None:
    print(f"\n── {title} ─────────────────────────────────────────")
    print(f"{'horizon':<8}{'bucket':<24}stats")
    for horizon in sorted(table.keys()):
        for bk in BUCKETS:
            cell = table[horizon].get(bk, {"label": "missing", "n": 0})
            print(f"{horizon:<8}{bk:<24}{_fmt_cell(cell)}")


def _print_2d_table(title: str, table: dict[str, dict[str, dict[str, dict[str, Any]]]]) -> None:
    print(f"\n── {title} ─────────────────────────────────────────")
    for horizon in sorted(table.keys()):
        print(f"\n  horizon {horizon}")
        for primary in sorted(table[horizon].keys()):
            print(f"    {primary}")
            for bk in BUCKETS:
                if bk in table[horizon][primary]:
                    print(f"      {bk:<22}{_fmt_cell(table[horizon][primary][bk])}")


def _print_flag_combo_table(title: str, table: dict[str, dict[str, dict[str, Any]]]) -> None:
    print(f"\n── {title} ─────────────────────────────────────────")
    for horizon in sorted(table.keys()):
        print(f"\n  horizon {horizon}")
        for combo in FLAG_COMBOS:
            cell = table[horizon].get(combo, {"label": "missing", "n": 0})
            print(f"    {combo:<24}{_fmt_cell(cell)}")


def render_stdout(result: dict[str, Any]) -> None:
    print("=" * 78)
    print(f"FILL MARKOUT BY STABILITY BUCKET   schema_version={SCHEMA_VERSION}")
    print("=" * 78)
    print(f"n_fills_total              = {result['n_fills_total']}")
    print(f"n_fills_with_markout_any   = {result['n_fills_with_markout_any']}")
    print(f"n_fills_maker_only_estimate= {result['n_fills_maker_only_estimate']}")
    print(f"n_fills_shadow             = {result['n_fills_shadow']}")
    print(f"missing_context            = {result['missing_context']}")

    print("\n── BASELINE BANNER (sanity check) ────────────────────────")
    for k in sorted(k for k in result if k.startswith("stable_vs_population_delta_")):
        v = result[k]
        flag = ""
        if isinstance(v, (int, float)) and abs(v) > 0.5:
            flag = "  ⚠ LARGE — check join logic"
        print(f"  {k:<55}= {v}{flag}")

    print("\n── QUOTE AGE (ms) ────────────────────────────────────────")
    print(f"  {_fmt_cell(result['quote_age_ms'])}")

    _print_bucket_table("MARKOUT BY BUCKET — all fills", result["markout_by_bucket"]["all"])
    _print_bucket_table("MARKOUT BY BUCKET — maker-only-estimate", result["markout_by_bucket"]["maker_only_estimate"])
    _print_2d_table("MARKOUT BY SYMBOL × BUCKET — all fills", result["markout_by_symbol_x_bucket"]["all"])
    _print_2d_table("MARKOUT BY SYMBOL × BUCKET — maker-only-estimate", result["markout_by_symbol_x_bucket"]["maker_only_estimate"])
    _print_2d_table("MARKOUT BY TOD × BUCKET — all fills", result["markout_by_tod_x_bucket"]["all"])
    _print_2d_table("MARKOUT BY TOD × BUCKET — maker-only-estimate", result["markout_by_tod_x_bucket"]["maker_only_estimate"])
    _print_flag_combo_table("MARKOUT BY FLAG COMBO — all fills", result["markout_by_flag_combo"]["all"])
    _print_flag_combo_table("MARKOUT BY FLAG COMBO — maker-only-estimate", result["markout_by_flag_combo"]["maker_only_estimate"])


# ── git sha helper ──────────────────────────────────────────────────────────

def _git_sha() -> Optional[str]:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=2, check=True,
        ).stdout.strip() or None
    except Exception:
        return None


# ── main ────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log", nargs="?", default=str(DEFAULT_LOG))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    log_path = Path(args.log)
    if not log_path.exists():
        print(f"ERROR: log not found: {log_path}", file=sys.stderr)
        return 1

    fills, mid_by_sym = load_log(log_path)
    if not fills:
        print(f"WARN: no fill_observation events in {log_path}")
        # Still write an empty artifact so downstream knows we ran.
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps({
            "schema_version": SCHEMA_VERSION,
            "kind": "fill_markout_by_stability_distributions",
            "git_sha": _git_sha(),
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "log_path": str(log_path),
            "n_fills_total": 0,
            "note": "no fill_observation events found",
        }, indent=2))
        return 0

    result = aggregate(fills, mid_by_sym)
    out = {
        "schema_version": SCHEMA_VERSION,
        "kind": "fill_markout_by_stability_distributions",
        "git_sha": _git_sha(),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "log_path": str(log_path),
        **result,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2))

    render_stdout(result)
    print(f"\nWrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
