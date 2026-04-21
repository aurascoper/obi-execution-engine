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

Modes:
  Mean-reversion (default):
    source env.sh && python3 screener.py
    source env.sh && python3 screener.py --new-only   # hide symbols already in engine
    source env.sh && python3 screener.py --sector Financials
    source env.sh && python3 screener.py --min-z 1.5  # tighter threshold

  Momentum / trend-following:
    source env.sh && python3 screener.py --momentum
    source env.sh && python3 screener.py --momentum --sector Technology
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

# ── Momentum mode constants ───────────────────────────────────────────────────
MOMENTUM_LOOKBACK_DAYS = 400  # ~260 trading days; IEX gaps can eat 10-15 days
SMA_WINDOW = 240  # bars for trend SMA
Z_4H_WINDOW = 240  # bars for macro z-score (reuses SMA window on daily bars)
Z_4H_THRESHOLD = 0.5  # macro momentum confirmation

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


def compute_momentum_signals(
    bars: pd.DataFrame,
    sector_map: dict[str, str],
    min_price: float = MIN_PRICE,
    min_adv: float = MIN_ADV,
    z_threshold: float = Z_THRESHOLD,
    sector_filter: str | None = None,
) -> tuple[list[tuple], list[tuple]]:
    """
    Momentum / trend-following screen.

    LONG:  close > SMA_240 AND z_4h > 0.5 AND z_60 > +1.25  (trending up)
    SHORT: close < SMA_240 AND z_4h < -0.5 AND z_60 < -1.25  (trending down)

    Returns (longs, shorts) where each entry is:
      (z, symbol, last_close, adv_30, sector, z_4h, sma_240, pct_above_sma)
    """
    longs, shorts = [], []

    for sym, grp in bars.groupby("symbol"):
        grp = grp.sort_values("timestamp")
        closes = grp["close"].values
        vols = grp["volume"].values

        if len(closes) < max(SMA_WINDOW, WINDOW, ADV_WINDOW):
            continue

        last_close = closes[-1]
        adv_30 = float(np.mean(vols[-ADV_WINDOW:]))

        if last_close < min_price or adv_30 < min_adv:
            continue

        # 60-bar z-score (same as mean-reversion)
        window_60 = closes[-WINDOW:]
        mu60, sig60 = window_60.mean(), window_60.std(ddof=1)
        if sig60 < 1e-10:
            continue
        z = (last_close - mu60) / sig60

        # 240-bar z-score (macro momentum)
        window_240 = closes[-Z_4H_WINDOW:]
        mu240, sig240 = window_240.mean(), window_240.std(ddof=1)
        if sig240 < 1e-10:
            continue
        z_4h = (last_close - mu240) / sig240

        # 240-bar SMA (trend direction)
        sma_240 = float(np.mean(closes[-SMA_WINDOW:]))
        pct_above = (last_close - sma_240) / sma_240 * 100

        sector = sector_map.get(sym, "Unknown")
        if sector_filter and sector.lower() != sector_filter.lower():
            continue

        row = (z, sym, last_close, adv_30, sector, z_4h, sma_240, pct_above)

        # Momentum LONG: trending up + macro confirmation + short-term strength
        if last_close > sma_240 and z_4h > Z_4H_THRESHOLD and z > z_threshold:
            longs.append(row)

        # Momentum SHORT: trending down + macro confirmation + short-term weakness
        elif last_close < sma_240 and z_4h < -Z_4H_THRESHOLD and z < -z_threshold:
            shorts.append(row)

    longs.sort(key=lambda x: -x[0])  # strongest momentum first
    shorts.sort()  # most negative first
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


def print_momentum_results(
    longs: list[tuple],
    shorts: list[tuple],
    already_in: set[str],
    new_only: bool,
    scanned_date: str,
) -> None:
    def _print_zone(rows, label):
        visible = [r for r in rows if not new_only or r[1] not in already_in]
        tag = "(new only)" if new_only else ""
        print(f"\n=== {label}  {tag}  ({len(visible)} stocks) ===")
        print(
            f"  {'SYM':<8} {'z':>7}  {'z_4h':>7}  {'%SMA':>7}  {'Price':>9}  {'ADV':>7}  Sector"
        )
        print(
            f"  {'─' * 8} {'─' * 7}  {'─' * 7}  {'─' * 7}  {'─' * 9}  {'─' * 7}  {'─' * 24}"
        )
        for z, sym, px, adv, sec, z4h, _sma, pct in visible:
            engine_tag = "" if sym not in already_in else "  ✓"
            print(
                f"  {sym:<8} {z:+.3f}σ  {z4h:+.3f}σ  {pct:+.1f}%  ${px:>8.2f}  {_fmt_adv(adv):>7}  {sec}{engine_tag}"
            )

    print(f"\n{'═' * 78}")
    print(f"  MOMENTUM / TREND-FOLLOWING SCREEN  —  {scanned_date}")
    print(
        f"  Filters: price > ${MIN_PRICE:.0f}  |  30-day ADV > {MIN_ADV / 1e6:.0f}M shares"
    )
    print(
        f"  Signal:  close vs SMA-{SMA_WINDOW}  |  z_4h {'>' if True else '<'} {Z_4H_THRESHOLD}σ  |  z {'>' if True else '<'} {Z_THRESHOLD}σ"
    )
    print(f"{'═' * 78}")
    _print_zone(longs, "MOMENTUM LONG   ▲  (close > SMA, z_4h > 0.5, z > +1.25)")
    _print_zone(shorts, "MOMENTUM SHORT  ▼  (close < SMA, z_4h < -0.5, z < -1.25)")
    print()


# ── Entry point ────────────────────────────────────────────────────────────────


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Mean-reversion & momentum universe screener"
    )
    ap.add_argument(
        "--momentum",
        action="store_true",
        help="Switch to momentum/trend-following mode (buy strength, sell weakness)",
    )
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
    ap.add_argument(
        "--json",
        action="store_true",
        help="Emit longs/shorts as JSON instead of the human-readable table",
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

    lookback = MOMENTUM_LOOKBACK_DAYS if args.momentum else 95
    mode_label = "momentum" if args.momentum else "mean-reversion"
    print(f"Fetching {lookback}-day daily bars for {mode_label} mode (IEX feed)...")
    bars = fetch_bars(tickers, api_key, api_secret, lookback_days=lookback)
    if bars.empty:
        print("No bar data returned. Check API credentials.")
        sys.exit(1)
    print(f"Data returned for {bars['symbol'].nunique()} symbols\n")

    scanned_date = datetime.now().strftime("%Y-%m-%d %H:%M")

    if args.momentum:
        print("Computing momentum signals (SMA-240 + z_4h + z)...")
        longs, shorts = compute_momentum_signals(
            bars,
            sector_map,
            min_price=args.min_price,
            min_adv=args.min_adv,
            z_threshold=args.min_z,
            sector_filter=args.sector,
        )
        if args.json:
            import json as _json

            def _row_mom(r):
                z, sym, px, adv, sec, z4h, _sma, pct = r
                return {
                    "symbol": sym,
                    "z": z,
                    "z_4h": z4h,
                    "pct_sma": pct,
                    "price": px,
                    "adv": adv,
                    "sector": sec,
                    "mode": "momentum",
                }

            print(
                _json.dumps(
                    {
                        "longs": [_row_mom(r) for r in longs],
                        "shorts": [_row_mom(r) for r in shorts],
                    },
                    indent=2,
                )
            )
        else:
            print_momentum_results(
                longs,
                shorts,
                already_in,
                new_only=args.new_only,
                scanned_date=scanned_date,
            )
    else:
        print("Computing z-scores + applying quality filters...")
        longs, shorts = compute_signals(
            bars,
            sector_map,
            min_price=args.min_price,
            min_adv=args.min_adv,
            z_threshold=args.min_z,
            sector_filter=args.sector,
        )
        if args.json:
            import json as _json

            def _row_mr(r):
                z, sym, px, adv, sec = r
                return {
                    "symbol": sym,
                    "z": z,
                    "price": px,
                    "adv": adv,
                    "sector": sec,
                    "mode": "mean_reversion",
                }

            print(
                _json.dumps(
                    {
                        "longs": [_row_mr(r) for r in longs],
                        "shorts": [_row_mr(r) for r in shorts],
                    },
                    indent=2,
                )
            )
        else:
            print_results(
                longs,
                shorts,
                already_in,
                new_only=args.new_only,
                scanned_date=scanned_date,
            )


if __name__ == "__main__":
    main()
