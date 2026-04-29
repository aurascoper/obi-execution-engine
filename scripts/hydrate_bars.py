#!/usr/bin/env python3
"""
scripts/hydrate_bars.py — pull 1m/5m/15m OHLCV bars for the Stage 3 universe
into data/cache/bars.sqlite.

Phase 1 (seed) + Phase 2 (rolling refresh) per docs/execution_calibration_sketch.md
Operator-approved 2026-04-29 (universe=96 full, forward-only, all three intervals).

Behavior is identical between seed and rolling modes: we always pull the full
HL retention window per interval and rely on `INSERT OR REPLACE` to handle
idempotent re-pulls. The mode flag is logging/summary tone only.

HL retention by interval (empirical, hl_pairs_discover.py:92):
  1m  → ~3.6d
  5m  → ~17.5d
  15m → ~52d

Defaults pad slightly under the empirical retention to avoid trailing 404
sub-requests on the oldest fringes. Re-running the script before retention
expires keeps the bars.sqlite window rolling forward.

Run modes:
  seed       Initial pull. Logs per-symbol coverage assertion.
  rolling    Forward-capture refresh. Identical behavior, terser output.

Usage:
  venv/bin/python3 scripts/hydrate_bars.py --mode seed
  venv/bin/python3 scripts/hydrate_bars.py --mode rolling --rps 1.0
  venv/bin/python3 scripts/hydrate_bars.py --mode seed --intervals 1m
  venv/bin/python3 scripts/hydrate_bars.py --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Defer imports that pull the trading SDK until after argparse so --help is fast.
DEFAULT_UNIVERSE_FILE = ROOT / "config" / "stage3_universe.json"
DEFAULT_DB = ROOT / "data" / "cache" / "bars.sqlite"
DEFAULT_AUDIT_LOG = ROOT / "logs" / "bar_hydrate.jsonl"

# Lookback windows per interval (days). Pad under HL retention so we don't
# burn API calls on guaranteed-empty fringes. Stays identical seed↔rolling.
RETENTION_DAYS = {
    "1m": 3.5,
    "5m": 17.0,
    "15m": 51.0,
}

# Coverage warning thresholds (fraction of expected bars missing).
GAP_WARN_RATIO = 0.05
GAP_FAIL_RATIO = 0.20

INTERVAL_SECONDS = {"1m": 60, "5m": 300, "15m": 900}


def parse_universe(path: Path) -> tuple[list[str], list[str]]:
    payload = json.loads(path.read_text())
    native = list(payload.get("native", []))
    hip3 = list(payload.get("hip3", []))
    return native, hip3


def required_dexs(hip3_symbols: list[str]) -> list[str]:
    return sorted({s.split(":", 1)[0] for s in hip3_symbols if ":" in s})


def expected_bar_count(interval: str, days: float) -> int:
    return int(days * 86400 / INTERVAL_SECONDS[interval])


def emit_audit(audit_log: Path, payload: dict) -> None:
    audit_log.parent.mkdir(parents=True, exist_ok=True)
    with audit_log.open("a") as f:
        f.write(json.dumps(payload) + "\n")


def hydrate_one(
    cache,
    info,
    symbol: str,
    interval: str,
    days: float,
    audit_log: Path,
    sleep_between_chunks_s: float,
) -> dict:
    t0 = time.monotonic()
    try:
        n_bars = cache.hydrate(
            info,
            coin=symbol,
            interval=interval,
            lookback_days=int(days) if days == int(days) else max(1, int(days + 1)),
            symbol=symbol,
            sleep_between_calls_s=sleep_between_chunks_s,
        )
        ok = True
        err = None
    except Exception as exc:
        n_bars = 0
        ok = False
        err = f"{type(exc).__name__}: {exc}"

    expected = expected_bar_count(interval, days)
    gap = max(0.0, (expected - n_bars) / expected) if expected > 0 else 0.0
    if not ok:
        status = "fail"
    elif gap > GAP_FAIL_RATIO:
        status = "gap_fail"
    elif gap > GAP_WARN_RATIO:
        status = "gap_warn"
    else:
        status = "ok"

    record = {
        "event": "bar_hydrate_symbol",
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "symbol": symbol,
        "interval": interval,
        "lookback_days": days,
        "expected_bars": expected,
        "stored_bars": n_bars,
        "gap_ratio": round(gap, 4),
        "status": status,
        "duration_s": round(time.monotonic() - t0, 3),
        "error": err,
    }
    emit_audit(audit_log, record)
    return record


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Hydrate bars.sqlite for the Stage 3 universe"
    )
    ap.add_argument("--mode", choices=("seed", "rolling"), default="seed")
    ap.add_argument(
        "--universe-file",
        default=str(DEFAULT_UNIVERSE_FILE),
        help="JSON file with 'native' and 'hip3' arrays of symbols",
    )
    ap.add_argument("--db", default=str(DEFAULT_DB), help="SQLite cache path")
    ap.add_argument(
        "--intervals",
        default="1m,5m,15m",
        help="Comma-separated subset of {1m,5m,15m} (default: all three)",
    )
    ap.add_argument(
        "--rps",
        type=float,
        default=1.0,
        help="Per-symbol requests-per-second throttle (default 1.0; jitter ±0.3s applied)",
    )
    ap.add_argument(
        "--inter-chunk-sleep",
        type=float,
        default=0.15,
        help="Sleep between sub-window chunks for a single symbol (default 0.15s)",
    )
    ap.add_argument(
        "--audit-log",
        default=str(DEFAULT_AUDIT_LOG),
        help="JSONL audit trail (default logs/bar_hydrate.jsonl)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan output only — does not hit the network or write the cache",
    )
    args = ap.parse_args()

    universe_path = Path(args.universe_file)
    if not universe_path.exists():
        print(
            f"hydrate_bars: missing universe file at {universe_path}", file=sys.stderr
        )
        return 2
    native, hip3 = parse_universe(universe_path)
    universe = native + hip3
    dexs = required_dexs(hip3)

    intervals = [iv.strip() for iv in args.intervals.split(",") if iv.strip()]
    bad = [iv for iv in intervals if iv not in RETENTION_DAYS]
    if bad:
        print(f"hydrate_bars: unsupported intervals {bad}", file=sys.stderr)
        return 2

    total_calls = len(universe) * len(intervals)
    est_seconds = total_calls / max(args.rps, 0.1)
    audit_log = Path(args.audit_log)

    print(
        f"[plan] mode={args.mode} universe={len(universe)} (native={len(native)}, hip3={len(hip3)})  "
        f"intervals={intervals}  dexs={dexs}",
        file=sys.stderr,
    )
    print(
        f"[plan] symbol-calls={total_calls}  rps={args.rps}  est_wallclock≈{est_seconds:.0f}s",
        file=sys.stderr,
    )
    print(f"[plan] db={args.db}  audit={audit_log}", file=sys.stderr)

    if args.dry_run:
        print("[plan] --dry-run set; not connecting or writing.", file=sys.stderr)
        return 0

    # Defer SDK + cache imports so --dry-run/--help stay snappy.
    from hyperliquid.info import Info
    from hyperliquid.utils import constants
    from data.bar_cache import BarCache

    info_kwargs = {"skip_ws": True}
    if dexs:
        info_kwargs["perp_dexs"] = [""] + dexs
    try:
        info = Info(constants.MAINNET_API_URL, **info_kwargs)
    except TypeError:
        # Older SDK: drop perp_dexs and rely on HIP-3 prefixed-coin POST fallback.
        info = Info(constants.MAINNET_API_URL, skip_ws=True)
        print(
            "[warn] SDK rejected perp_dexs; HIP-3 falls back to HTTP POST path",
            file=sys.stderr,
        )

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    cache = BarCache(args.db)

    run_started = dt.datetime.now(dt.timezone.utc)
    summary = {
        "event": "bar_hydrate_summary_start",
        "ts": run_started.isoformat(),
        "mode": args.mode,
        "universe_size": len(universe),
        "intervals": intervals,
        "dexs": dexs,
        "rps": args.rps,
    }
    emit_audit(audit_log, summary)

    counts = {"ok": 0, "gap_warn": 0, "gap_fail": 0, "fail": 0}
    bars_total = 0
    failures: list[tuple[str, str, str]] = []
    base_sleep = 1.0 / max(args.rps, 0.1)

    try:
        for interval in intervals:
            days = RETENTION_DAYS[interval]
            for symbol in universe:
                rec = hydrate_one(
                    cache,
                    info,
                    symbol=symbol,
                    interval=interval,
                    days=days,
                    audit_log=audit_log,
                    sleep_between_chunks_s=args.inter_chunk_sleep,
                )
                counts[rec["status"]] = counts.get(rec["status"], 0) + 1
                bars_total += rec["stored_bars"]
                tag = "OK " if rec["status"] == "ok" else rec["status"].upper()
                line = (
                    f"  {tag:<8s} {symbol:<22s} {interval:>3s}  "
                    f"+{rec['stored_bars']:>5d} bars  "
                    f"gap={rec['gap_ratio']:>5.1%}  "
                    f"{rec['duration_s']:>5.2f}s"
                )
                if rec["status"] in ("fail", "gap_fail"):
                    failures.append(
                        (symbol, interval, rec.get("error") or rec["status"])
                    )
                    print(line, file=sys.stderr)
                elif args.mode == "seed" or rec["status"] != "ok":
                    print(line, file=sys.stderr)
                # rate throttle with jitter
                time.sleep(
                    max(0.0, base_sleep + random.uniform(-0.3, 0.3) * base_sleep)
                )
    finally:
        cache.close()

    run_ended = dt.datetime.now(dt.timezone.utc)
    summary_end = {
        "event": "bar_hydrate_summary_end",
        "ts": run_ended.isoformat(),
        "duration_s": round((run_ended - run_started).total_seconds(), 1),
        "mode": args.mode,
        "universe_size": len(universe),
        "intervals": intervals,
        "bars_inserted_total": bars_total,
        "counts": counts,
        "n_failures": len(failures),
        "failed_symbols": [f"{s}/{iv}" for s, iv, _ in failures[:25]],
    }
    emit_audit(audit_log, summary_end)

    print("", file=sys.stderr)
    print(
        f"[done] mode={args.mode}  bars_inserted={bars_total}  "
        f"ok={counts.get('ok', 0)}  warn={counts.get('gap_warn', 0)}  "
        f"fail={counts.get('gap_fail', 0) + counts.get('fail', 0)}  "
        f"duration={summary_end['duration_s']}s",
        file=sys.stderr,
    )
    if failures:
        print("[done] failures (first 10):", file=sys.stderr)
        for s, iv, err in failures[:10]:
            print(f"        {s} {iv}: {err}", file=sys.stderr)

    return 0 if not failures else (1 if counts.get("ok", 0) > 0 else 2)


if __name__ == "__main__":
    sys.exit(main())
