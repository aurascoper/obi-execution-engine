#!/usr/bin/env python3
"""scripts/build_replay_mid_window.py — extract a reproducible replay
window of mid prices for use as the markout source in
math_core/fill_model.py (Task 24 / Gate 2D).

Reads 1-minute bars from `data/cache/bars.sqlite`, takes bar closes as
mid, linearly interpolates to a regular dt grid (default 10s to match
the simulator), and writes the result as a JSON artifact under
`data/replay_windows/`.

Output JSON shape (committed alongside the family-sweep artifact):
  {
    "kind": "replay_mid_window",
    "git_sha": "...",
    "timestamp_utc": "...",
    "source": "data/cache/bars.sqlite",
    "symbol": "BTC",
    "interval_source": "1m",
    "dt_s": 10,
    "t_start_ms": int,        # first bar t_close_ms
    "t_end_ms": int,
    "n_bars": int,
    "n_grid_points": int,
    "mid_path": [float, ...]  # length n_grid_points
  }

The mid_path is the raw replay environment input. Loading it into the
simulator means the fill model becomes deterministic on mid; the only
remaining stochasticity is the OBI AR(1) process and aggressor flow.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
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


def fetch_1m_window(
    db: Path,
    symbol: str,
    n_bars: int,
    end_offset_bars: int = 0,
) -> list[tuple[int, float]]:
    """Pull the most-recent `n_bars` 1m bars for `symbol`, optionally
    skipping the last `end_offset_bars` (so we can build deterministic
    earlier-window slices). Returns [(t_close_ms, close_price), ...]
    in ascending time order."""
    con = sqlite3.connect(db)
    cur = con.cursor()
    rows = cur.execute(
        "SELECT t_close_ms, c FROM bars "
        "WHERE interval='1m' AND symbol=? "
        "ORDER BY t_close_ms DESC LIMIT ? OFFSET ?",
        (symbol, n_bars, end_offset_bars),
    ).fetchall()
    con.close()
    rows = sorted(rows)
    return [(int(r[0]), float(r[1])) for r in rows]


def interpolate_to_grid(
    bars: list[tuple[int, float]], dt_s: float
) -> tuple[list[float], int, int]:
    """Linear-interpolate (t_ms, close) bars onto a regular dt-second
    grid that spans the bars' time range. Returns (mid_path, t_start_ms,
    t_end_ms)."""
    if len(bars) < 2:
        raise ValueError("need at least 2 bars to interpolate")
    t0_ms = bars[0][0]
    t_end_ms = bars[-1][0]
    span_s = (t_end_ms - t0_ms) / 1000.0
    n_points = int(span_s // dt_s) + 1
    out: list[float] = []
    j = 0
    for k in range(n_points):
        target_ms = t0_ms + int(k * dt_s * 1000)
        while j + 1 < len(bars) and bars[j + 1][0] <= target_ms:
            j += 1
        if j + 1 >= len(bars):
            out.append(bars[-1][1])
            continue
        t_a, p_a = bars[j]
        t_b, p_b = bars[j + 1]
        if t_b == t_a:
            out.append(p_a)
        else:
            w = (target_ms - t_a) / (t_b - t_a)
            out.append(p_a + w * (p_b - p_a))
    return out, t0_ms, t_end_ms


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=ROOT / "data/cache/bars.sqlite")
    ap.add_argument("--symbol", type=str, default="BTC")
    ap.add_argument("--n-bars", type=int, default=90,
                    help="how many 1m bars (90 = 90 min of replay)")
    ap.add_argument("--end-offset-bars", type=int, default=0,
                    help="skip this many most-recent bars before slicing "
                         "(use 0 for most recent)")
    ap.add_argument("--dt-s", type=float, default=10.0)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    bars = fetch_1m_window(args.db, args.symbol, args.n_bars, args.end_offset_bars)
    if len(bars) < args.n_bars:
        print(f"warn: requested {args.n_bars} bars, got {len(bars)}")
    mid_path, t0_ms, t_end_ms = interpolate_to_grid(bars, args.dt_s)

    record = {
        "kind": "replay_mid_window",
        "git_sha": _git_sha(),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "source": str(args.db.relative_to(ROOT)),
        "symbol": args.symbol,
        "interval_source": "1m",
        "dt_s": args.dt_s,
        "t_start_ms": t0_ms,
        "t_end_ms": t_end_ms,
        "t_start_iso": datetime.fromtimestamp(t0_ms / 1000, tz=timezone.utc).isoformat(),
        "t_end_iso": datetime.fromtimestamp(t_end_ms / 1000, tz=timezone.utc).isoformat(),
        "n_bars": len(bars),
        "n_grid_points": len(mid_path),
        "mid_min": min(mid_path),
        "mid_max": max(mid_path),
        "mid_first": mid_path[0],
        "mid_last": mid_path[-1],
        "mid_total_change_bps": (mid_path[-1] - mid_path[0]) / mid_path[0] * 10_000.0,
        "mid_path": mid_path,
    }

    out_path = args.out or (
        ROOT / f"data/replay_windows/replay_{args.symbol}_{args.n_bars}m_offset{args.end_offset_bars}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(record, indent=2))

    print(f"\nReplay window written: {out_path}")
    print(f"  symbol           = {args.symbol}")
    print(f"  bars used        = {len(bars)} (interval=1m)")
    print(f"  grid points      = {len(mid_path)} (dt={args.dt_s}s)")
    print(f"  span             = {record['t_start_iso']} → {record['t_end_iso']}")
    print(f"  mid first / last = {record['mid_first']:.4f} / {record['mid_last']:.4f}")
    print(f"  mid min / max    = {record['mid_min']:.4f} / {record['mid_max']:.4f}")
    print(f"  total move (bps) = {record['mid_total_change_bps']:+.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
