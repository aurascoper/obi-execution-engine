#!/usr/bin/env python3
"""
scripts/fetch_earnings.py — populate config/earnings/<date>.json from yfinance.

For each ticker in our HIP3_XYZ_TRADABLE universe, query the Yahoo Finance
earnings-dates endpoint and select any earnings event whose UTC date matches
the target date. Writes the result to config/earnings/<date>.json in the
schema consumed by scripts/earnings_analyzer.py.

Read-only otherwise. No engine touches, no git commits, no order paths.

Usage:
  scripts/fetch_earnings.py --date 2026-04-29
  scripts/fetch_earnings.py --date 2026-04-29 --dry-run    # prints, no write
  scripts/fetch_earnings.py --date 2026-04-29 --window-h 36

Behavior:
  - Default --window-h is 24, meaning "earnings event datetime in [date 00:00 UTC, date+1 00:00 UTC]".
    For US after-close prints the venue stamp may be next-day in UTC, so
    --window-h 36 captures both before-open AND prior-after-close prints
    that print up to ~12h after midnight UTC.
  - On a yfinance error for a single symbol, log + continue. A symbol failure
    never aborts the run.
  - On any fatal IO error, exit non-zero. The analyzer can still run on
    a partial file.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import warnings
from pathlib import Path

# Suppress yfinance pandas FutureWarnings during the per-symbol loop — they
# are noise and not actionable from the calling layer.
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)


# Mirror of earnings_analyzer.HIP3_XYZ_TRADABLE. Keep in sync by hand for
# now; a future refactor can share the constant via a small module.
HIP3_XYZ_TRADABLE: tuple[str, ...] = (
    "HIMS", "HOOD", "ORCL", "EWY", "XYZ100", "CRWV", "TSLA", "CL", "SNDK",
    "SKHX", "MSFT", "MU", "SP500", "AMD", "PLTR", "BRENTOIL", "GOLD",
    "SILVER", "NATGAS", "COPPER", "PLATINUM", "TSM", "GOOGL", "META",
    "AAPL", "LLY", "NFLX", "COST", "BABA", "RKLB", "MRVL", "EUR", "JP225",
    "XLE", "PALLADIUM", "URANIUM", "RIVN",
)

# Pure-equity subset — the rest are commodity / index / FX baskets that
# don't have earnings events. Skip them to avoid wasted yfinance calls.
NON_EARNINGS_NAMES: frozenset[str] = frozenset({
    "EWY", "XYZ100", "CL", "SP500", "BRENTOIL", "GOLD", "SILVER", "NATGAS",
    "COPPER", "PLATINUM", "EUR", "JP225", "XLE", "PALLADIUM", "URANIUM",
})


def _classify_session(when: dt.datetime, target_date: dt.date) -> str:
    """Heuristic: pre-04:00 ET → before_open; post-20:00 ET → after_close;
    else during/unknown. yfinance timestamps are UTC-aware. We approximate
    ET as UTC-4 (DST) since this is best-effort metadata, not a trade gate."""
    et_hour = (when.hour - 4) % 24  # rough EDT
    if when.date() == target_date:
        if et_hour < 9:
            return "before_open"
        if et_hour >= 16:
            return "after_close"
        return "during"
    if when.date() < target_date:
        return "after_close"
    return "before_open"


def _fetch_one(yf_module, ticker: str, target_date: dt.date, window_h: int):
    """Return (matched_event_dict_or_None, error_str_or_None)."""
    try:
        t = yf_module.Ticker(ticker)
        df = t.get_earnings_dates(limit=4)
    except Exception as e:
        return None, f"yfinance_error: {type(e).__name__}: {e}"
    if df is None or df.empty:
        return None, "no_data"
    win_start = dt.datetime.combine(target_date, dt.time(0, 0), tzinfo=dt.timezone.utc)
    win_end = win_start + dt.timedelta(hours=window_h)
    for idx_ts in df.index:
        ts = idx_ts.to_pydatetime() if hasattr(idx_ts, "to_pydatetime") else idx_ts
        if getattr(ts, "tzinfo", None) is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        else:
            ts = ts.astimezone(dt.timezone.utc)
        if win_start <= ts < win_end:
            try:
                info = t.info
                company = info.get("shortName") or info.get("longName") or ticker
            except Exception:
                company = ticker
            return {
                "company": company,
                "ticker": ticker,
                "session": _classify_session(ts, target_date),
                "earnings_ts_utc": ts.isoformat(),
                "source": "yfinance",
            }, None
    return None, "no_event_in_window"


def main() -> int:
    ap = argparse.ArgumentParser(description="Yahoo Finance earnings fetcher")
    ap.add_argument("--date", required=True, help="Target date YYYY-MM-DD")
    ap.add_argument("--window-h", type=int, default=24,
                    help="Hours after start-of-target-date to consider (default 24; use 36 to catch after-close prints stamped next-day UTC)")
    ap.add_argument("--out", default=None,
                    help="Output path (default: config/earnings/<date>.json)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print result, do not write the file")
    args = ap.parse_args()

    try:
        target_date = dt.date.fromisoformat(args.date)
    except ValueError as e:
        print(f"fetch_earnings: bad --date {args.date}: {e}", file=sys.stderr)
        return 2

    try:
        import yfinance as yf
    except ImportError:
        print("fetch_earnings: yfinance not available in this venv", file=sys.stderr)
        return 2

    matched: list[dict] = []
    errors: dict[str, str] = {}
    skipped_non_earnings: list[str] = []

    for ticker in HIP3_XYZ_TRADABLE:
        if ticker in NON_EARNINGS_NAMES:
            skipped_non_earnings.append(ticker)
            continue
        evt, err = _fetch_one(yf, ticker, target_date, args.window_h)
        if evt:
            matched.append(evt)
            print(
                f"  HIT  {ticker:<8s}  {evt['session']:<12s}  {evt['earnings_ts_utc']}  {evt['company']}",
                file=sys.stderr,
            )
        elif err and err not in ("no_event_in_window", "no_data"):
            errors[ticker] = err
            print(f"  ERR  {ticker:<8s}  {err}", file=sys.stderr)

    payload = {
        "date": args.date,
        "schema_version": 1,
        "names": matched,
        "_provenance": {
            "source": "yfinance",
            "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "window_h": args.window_h,
            "n_universe": len(HIP3_XYZ_TRADABLE),
            "n_skipped_non_earnings": len(skipped_non_earnings),
            "n_errors": len(errors),
            "errors": errors,
        },
    }

    out_path = Path(args.out or f"config/earnings/{args.date}.json")
    if args.dry_run:
        print(json.dumps(payload, indent=2))
        print(f"\n[DRY RUN] {len(matched)} hits, {len(errors)} errors. Would write {out_path}", file=sys.stderr)
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n")
    print(
        f"fetch_earnings: wrote {out_path}  hits={len(matched)}  errors={len(errors)}  skipped={len(skipped_non_earnings)}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
