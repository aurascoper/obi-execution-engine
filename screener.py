#!/usr/bin/env python3
"""
screener.py — Universe scanner with institutional quality filters.

Scans S&P 500 ∪ NASDAQ 100 ∪ Russell 3000 for mean-reversion signals using
the same dual-gate strategy as the live equities engine.

Quality filters (hardcoded — do not lower without risk review):
  MIN_PRICE = $20.00           eliminates penny stocks and binary-event names
  MIN_ADV   = 1,000,000 shares ensures institutional liquidity (fills + exits)

Signal zones:
  LONG:  z < -1.25σ  (oversold — mean-reversion long candidate)
  SHORT: z > +1.25σ  (overbought — mean-reversion short candidate)

Usage:
  source env.sh && python3 screener.py
  source env.sh && python3 screener.py --new-only   # hide symbols already in engine
  source env.sh && python3 screener.py --sector Financials
  source env.sh && python3 screener.py --min-z 1.5  # tighter threshold
"""

import os
import sys
import time
import argparse
import requests
import numpy as np
import pandas as pd
from io import StringIO
from datetime import datetime, timezone, timedelta

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

# ── Quality filters ────────────────────────────────────────────────────────────
MIN_PRICE = 20.0  # minimum last close ($)
MIN_ADV = 1_000_000  # minimum 30-day average daily volume (shares)
Z_THRESHOLD = 1.25  # |z| threshold for entry/short zones
WINDOW = 60  # bars for z-score rolling window
ADV_WINDOW = 30  # bars for ADV calculation
BATCH = 100  # symbols per Alpaca API request
SLEEP = 0.3  # seconds between batches (rate limiting)

HEADERS = {"User-Agent": "Mozilla/5.0 (research screen; contact: aurascoper@github)"}


# ── Index fetchers ─────────────────────────────────────────────────────────────


def _sp500() -> list[str]:
    r = requests.get(
        "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        headers=HEADERS,
        timeout=15,
    )
    df = pd.read_html(StringIO(r.text))[0]
    return df["Symbol"].str.replace(".", "-", regex=False).tolist()


def _nasdaq100() -> list[str]:
    r = requests.get(
        "https://en.wikipedia.org/wiki/Nasdaq-100", headers=HEADERS, timeout=15
    )
    tables = pd.read_html(StringIO(r.text))
    df = next(t for t in tables if "Ticker" in t.columns)
    return df["Ticker"].tolist()


def _russell3000() -> tuple[list[str], dict[str, str]]:
    """Returns (tickers, sector_map) from iShares IWV holdings CSV."""
    url = (
        "https://www.ishares.com/us/products/239714/ishares-russell-3000-etf/"
        "1467271812596.ajax?fileType=csv&fileName=IWV_holdings&dataType=fund"
    )
    r = requests.get(url, headers=HEADERS, timeout=20)
    df_raw = pd.read_csv(StringIO(r.text), skiprows=9, on_bad_lines="skip")
    df_raw.columns = df_raw.columns.str.strip()
    equities = df_raw[
        (df_raw["Asset Class"] == "Equity")
        & (df_raw["Ticker"].notna())
        & (~df_raw["Ticker"].str.contains(r"\s|-", na=True))
    ].copy()
    tickers = equities["Ticker"].str.strip().unique().tolist()
    sector_map = dict(
        zip(
            equities["Ticker"].str.strip(),
            equities["Sector"].str.strip(),
        )
    )
    return tickers, sector_map


def build_universe() -> tuple[list[str], dict[str, str]]:
    """Union of S&P 500, NASDAQ 100, and Russell 3000. Returns (tickers, sector_map)."""
    print("Fetching index membership...", flush=True)
    try:
        sp500 = _sp500()
        print(f"  S&P 500:     {len(sp500):>4} symbols")
    except Exception as e:
        print(f"  S&P 500 fetch failed: {e}")
        sp500 = []
    try:
        nq100 = _nasdaq100()
        print(f"  NASDAQ 100:  {len(nq100):>4} symbols")
    except Exception as e:
        print(f"  NASDAQ 100 fetch failed: {e}")
        nq100 = []
    try:
        r3k, sector_map = _russell3000()
        print(f"  Russell 3000:{len(r3k):>4} symbols")
    except Exception as e:
        print(f"  Russell 3000 fetch failed: {e}")
        r3k, sector_map = [], {}

    tickers = sorted(set(sp500) | set(nq100) | set(r3k))
    print(f"  Combined:    {len(tickers):>4} unique symbols\n")
    return tickers, sector_map


# ── Bar fetcher ────────────────────────────────────────────────────────────────


def fetch_bars(
    tickers: list[str],
    api_key: str,
    api_secret: str,
    lookback_days: int = 95,
) -> pd.DataFrame:
    """Fetch daily OHLCV bars for all tickers via IEX feed (free tier)."""
    client = StockHistoricalDataClient(api_key, api_secret)
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=lookback_days)

    n_batches = (len(tickers) + BATCH - 1) // BATCH
    all_rows = []

    for i in range(0, len(tickers), BATCH):
        batch = tickers[i : i + BATCH]
        try:
            req = StockBarsRequest(
                symbol_or_symbols=batch,
                timeframe=TimeFrame.Day,
                start=start,
                end=now,
                limit=20_000,
                feed="iex",
            )
            chunk = client.get_stock_bars(req).df.reset_index()
            all_rows.append(chunk)
        except Exception:
            pass
        done = min(i + BATCH, len(tickers))
        if done % 500 == 0 or done == len(tickers):
            print(f"  fetched {done:>4}/{len(tickers)} tickers...", flush=True)
        time.sleep(SLEEP)

    if not all_rows:
        return pd.DataFrame()
    return pd.concat(all_rows, ignore_index=True)


# ── Signal computation ─────────────────────────────────────────────────────────


def compute_signals(
    bars: pd.DataFrame,
    sector_map: dict[str, str],
    min_price: float = MIN_PRICE,
    min_adv: float = MIN_ADV,
    z_threshold: float = Z_THRESHOLD,
    sector_filter: str | None = None,
) -> tuple[list[tuple], list[tuple]]:
    """
    Returns (longs, shorts) where each entry is:
      (z, symbol, last_close, adv_30, sector)
    Both lists sorted by abs(z) descending.
    """
    longs, shorts = [], []

    for sym, grp in bars.groupby("symbol"):
        grp = grp.sort_values("timestamp")
        closes = grp["close"].values
        vols = grp["volume"].values

        if len(closes) < max(WINDOW, ADV_WINDOW):
            continue

        last_close = closes[-1]
        adv_30 = float(np.mean(vols[-ADV_WINDOW:]))

        # ── Quality filters ────────────────────────────────────────────────
        if last_close < min_price:
            continue
        if adv_30 < min_adv:
            continue

        # ── Z-score ────────────────────────────────────────────────────────
        window = closes[-WINDOW:]
        mu, sig = window.mean(), window.std(ddof=1)
        if sig < 1e-10:
            continue
        z = (last_close - mu) / sig

        sector = sector_map.get(sym, "Unknown")
        if sector_filter and sector.lower() != sector_filter.lower():
            continue

        row = (z, sym, last_close, adv_30, sector)
        if z < -z_threshold:
            longs.append(row)
        elif z > z_threshold:
            shorts.append(row)

    longs.sort()  # most negative first
    shorts.sort(key=lambda x: -x[0])  # most positive first
    return longs, shorts


# ── Printer ────────────────────────────────────────────────────────────────────


def _fmt_adv(adv: float) -> str:
    if adv >= 1_000_000:
        return f"{adv / 1_000_000:.1f}M"
    return f"{adv / 1_000:.0f}K"


def print_results(
    longs: list[tuple],
    shorts: list[tuple],
    already_in: set[str],
    new_only: bool,
    scanned_date: str,
) -> None:
    def _print_zone(rows, label, flag):
        visible = [r for r in rows if not new_only or r[1] not in already_in]
        tag = "(new only)" if new_only else ""
        print(f"\n=== {label}  {tag}  ({len(visible)} stocks) ===")
        print(f"  {'SYM':<8} {'z':>7}   {'Price':>9}   {'ADV':>7}   Sector")
        print(f"  {'─' * 8} {'─' * 7}   {'─' * 9}   {'─' * 7}   {'─' * 24}")
        for z, sym, px, adv, sec in visible:
            engine_tag = "" if sym not in already_in else "  ✓"
            print(
                f"  {sym:<8} {z:+.3f}σ   ${px:>8.2f}   {_fmt_adv(adv):>7}   {sec}{engine_tag}"
            )

    print(f"\n{'═' * 70}")
    print(f"  MEAN-REVERSION SCREEN  —  {scanned_date}")
    print(
        f"  Filters: price > ${MIN_PRICE:.0f}  |  30-day ADV > {MIN_ADV / 1e6:.0f}M shares"
    )
    print(f"  Signal:  |z| > {Z_THRESHOLD}σ  over {WINDOW}-bar rolling window")
    print(f"{'═' * 70}")
    _print_zone(longs, "LONG ZONE   z < -1.25σ", "◀ LONG")
    _print_zone(shorts, "SHORT ZONE  z > +1.25σ", "◀ SHORT")
    print()


# ── Entry point ────────────────────────────────────────────────────────────────


def main() -> None:
    ap = argparse.ArgumentParser(description="Mean-reversion universe screener")
    ap.add_argument(
        "--new-only",
        action="store_true",
        help="Hide symbols already tracked by the equities engine",
    )
    ap.add_argument(
        "--sector",
        type=str,
        default=None,
        help="Filter output to a single sector (e.g. 'Financials')",
    )
    ap.add_argument(
        "--min-z",
        type=float,
        default=Z_THRESHOLD,
        help=f"Override z-score threshold (default {Z_THRESHOLD})",
    )
    ap.add_argument(
        "--min-price",
        type=float,
        default=MIN_PRICE,
        help=f"Override minimum price filter (default ${MIN_PRICE:.0f})",
    )
    ap.add_argument(
        "--min-adv",
        type=float,
        default=MIN_ADV,
        help=f"Override minimum ADV filter (default {MIN_ADV / 1e6:.0f}M)",
    )
    args = ap.parse_args()

    api_key = os.environ["ALPACA_API_KEY_ID"]
    api_secret = os.environ["ALPACA_API_SECRET_KEY"]

    # Symbols currently tracked by equities engine (for tagging / --new-only)
    try:
        sys.path.insert(0, os.path.dirname(__file__))
        from equities_engine import SYMBOLS as ENGINE_SYMBOLS

        already_in = set(ENGINE_SYMBOLS)
    except Exception:
        already_in = set()

    tickers, sector_map = build_universe()

    print(f"Fetching {WINDOW}-day daily bars (IEX feed)...")
    bars = fetch_bars(tickers, api_key, api_secret)
    if bars.empty:
        print("No bar data returned. Check API credentials.")
        sys.exit(1)
    print(f"Data returned for {bars['symbol'].nunique()} symbols\n")

    print("Computing z-scores + applying quality filters...")
    longs, shorts = compute_signals(
        bars,
        sector_map,
        min_price=args.min_price,
        min_adv=args.min_adv,
        z_threshold=args.min_z,
        sector_filter=args.sector,
    )

    print_results(
        longs,
        shorts,
        already_in,
        new_only=args.new_only,
        scanned_date=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )


if __name__ == "__main__":
    main()
