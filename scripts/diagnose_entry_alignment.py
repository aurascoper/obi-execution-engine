#!/usr/bin/env python3
"""Phase B Hypothesis 1 measurement: live entry_signal vs replay opens.

Compares the live engine's entry_signal events to the replay harness's
emitted opens within ±60s windows per symbol. Reports precision, recall,
side agreement, and per-symbol breakdown.

Decision rule (per the GPT-5.5 deep-research plan):
    if precision <70% or recall <70% or side_agreement <80%:
        candidate generation is the primary bottleneck.
        Pivot to live-entry-anchored replay (Hypothesis 3).
    else:
        entry identity is fine; gap is exit / fill / funding.

Usage:
    # 1. Run replay with opens emission
    REPLAY_OPENS_OUT=/tmp/replay_opens.jsonl venv/bin/python3 scripts/z_entry_replay_gated.py

    # 2. Run this diagnostic
    venv/bin/python3 scripts/diagnose_entry_alignment.py [--window-days 14] \
        [--match-window-s 60] [--opens /tmp/replay_opens.jsonl]
"""

from __future__ import annotations

import argparse
import json
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / "hl_engine.jsonl"


def _parse_ts_ms(ts) -> int:
    if isinstance(ts, (int, float)):
        return int(ts * 1000) if ts < 1e12 else int(ts)
    if isinstance(ts, str):
        try:
            if ts.endswith("Z"):
                ts = ts[:-1] + "+00:00"
            return int(datetime.fromisoformat(ts).timestamp() * 1000)
        except Exception:
            return 0
    return 0


def _norm_sym(s: str) -> str:
    return (s or "").replace("/USD", "").replace("/USDC", "")


def _side_int(direction: str) -> int:
    """live entry_signal direction is 'long' / 'short'; map to +1 / -1."""
    if not direction:
        return 0
    d = direction.lower()
    if d in ("long", "buy"):
        return 1
    if d in ("short", "sell"):
        return -1
    return 0


def load_live_entries(from_ms: int, to_ms: int):
    """Returns {sym: [(ts_ms, side, tag), ...]} from entry_signal events.
    Pre-fill — many of these are rejected at HL (would-cross etc)."""
    out: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
    with LOG.open() as f:
        for line in f:
            if '"event": "entry_signal"' not in line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("event") != "entry_signal":
                continue
            ts = _parse_ts_ms(r.get("timestamp", ""))
            if ts < from_ms or ts >= to_ms:
                continue
            sym = _norm_sym(r.get("symbol") or r.get("coin") or "")
            if not sym:
                continue
            side = _side_int(r.get("direction", ""))
            if side == 0:
                continue
            tag = r.get("tag", "")
            out[sym].append((ts, side, tag))
    for sym in out:
        out[sym].sort()
    return out


def load_live_actual_opens(from_ms: int, to_ms: int):
    """Returns {sym: [(ts_ms, side, tag), ...]} for ACTUAL position openings,
    derived from hl_fill_received by tracking running per-symbol position.

    A 'live open' is any fill that transitions the running position from 0
    to non-zero (or sign-flips, which we treat as close+open).
    """
    fills: list[tuple[int, str, int, float]] = []  # (ts, sym, side_int, sz)
    with LOG.open() as f:
        for line in f:
            if '"hl_fill_received"' not in line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("event") != "hl_fill_received":
                continue
            ts = _parse_ts_ms(r.get("timestamp", ""))
            if ts < from_ms or ts >= to_ms:
                continue
            sym = _norm_sym(r.get("symbol") or r.get("coin") or "")
            if not sym:
                continue
            side_int = 1 if (r.get("side", "") or "").lower() == "buy" else -1
            try:
                sz = float(r.get("sz", 0))
            except (TypeError, ValueError):
                continue
            if sz <= 0:
                continue
            fills.append((ts, sym, side_int, sz))
    fills.sort()

    pos_by_sym: dict[str, float] = defaultdict(float)
    out: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
    for ts, sym, side, sz in fills:
        prev = pos_by_sym[sym]
        delta = side * sz
        new = prev + delta
        # Open = transition from 0 (or near-zero dust) to non-zero
        if abs(prev) < 1e-9 and abs(new) > 1e-9:
            out[sym].append((ts, 1 if new > 0 else -1, ""))
        # Sign flip: position changes sign — count as close+open
        elif prev * new < 0:
            out[sym].append((ts, 1 if new > 0 else -1, "flip"))
        pos_by_sym[sym] = new
    for sym in out:
        out[sym].sort()
    return out


def load_replay_opens(path: Path, from_ms: int, to_ms: int):
    """Returns {sym: [(ts_ms, side), ...]} from replay opens JSONL."""
    out: dict[str, list[tuple[int, int]]] = defaultdict(list)
    if not path.exists():
        return out
    with path.open() as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = int(r.get("ts", 0))
            if ts < from_ms or ts >= to_ms:
                continue
            sym = r.get("symbol", "")
            if not sym:
                continue
            side = int(r.get("side", 0))
            if side == 0:
                continue
            out[sym].append((ts, side))
    for sym in out:
        out[sym].sort()
    return out


def find_match(opens: list[tuple[int, int]], target_ts: int, window_ms: int):
    """Return (idx, ts, side) of nearest open in `opens` within ±window_ms of
    target_ts, or None. opens must be sorted by ts."""
    if not opens:
        return None
    ts_only = [t for t, _ in opens]
    j = bisect_left(ts_only, target_ts)
    candidates = []
    if j < len(opens):
        candidates.append(j)
    if j > 0:
        candidates.append(j - 1)
    best = None
    best_dt = window_ms + 1
    for k in candidates:
        dt = abs(opens[k][0] - target_ts)
        if dt <= window_ms and dt < best_dt:
            best = (k, opens[k][0], opens[k][1])
            best_dt = dt
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-days", type=int, default=14)
    ap.add_argument("--match-window-s", type=int, default=60)
    ap.add_argument("--opens", default="/tmp/replay_opens.jsonl")
    ap.add_argument(
        "--live-source",
        choices=("entry_signal", "actual_opens"),
        default="actual_opens",
        help="entry_signal: pre-fill candidates (~10k/14d). "
        "actual_opens: derived from hl_fill_received transitions (~tens-hundreds/14d).",
    )
    args = ap.parse_args()

    to_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    from_ms = to_ms - args.window_days * 86_400_000
    match_ms = args.match_window_s * 1000

    if args.live_source == "actual_opens":
        live = load_live_actual_opens(from_ms, to_ms)
    else:
        live = load_live_entries(from_ms, to_ms)
    replay = load_replay_opens(Path(args.opens), from_ms, to_ms)

    n_live = sum(len(v) for v in live.values())
    n_replay = sum(len(v) for v in replay.values())
    print(f"# window: {args.window_days}d  match_window: ±{args.match_window_s}s")
    print(f"# live source: {args.live_source}")
    print(f"# live entries:   {n_live} across {len(live)} symbols")
    print(f"# replay opens:   {n_replay} across {len(replay)} symbols")

    # Greedy matching: for each live entry, find nearest replay open in same
    # symbol within window. Mark replay-side as consumed so each open matches
    # at most one live entry.
    per_sym_stats: dict[str, dict] = defaultdict(
        lambda: {
            "live_n": 0,
            "replay_n": 0,
            "exact_side_match": 0,
            "side_flip": 0,
            "missing_in_replay": 0,
            "replay_only": 0,
            "ts_errors_ms": [],
        }
    )

    all_ts_errors_ms: list[int] = []

    all_syms = set(live.keys()) | set(replay.keys())
    for sym in sorted(all_syms):
        live_entries = live.get(sym, [])
        replay_opens = list(replay.get(sym, []))  # mutable copy
        consumed_replay = [False] * len(replay_opens)
        per_sym_stats[sym]["live_n"] = len(live_entries)
        per_sym_stats[sym]["replay_n"] = len(replay_opens)

        for live_ts, live_side, _tag in live_entries:
            # Find nearest unconsumed replay open
            best_idx = None
            best_dt = match_ms + 1
            for j, (rts, _rside) in enumerate(replay_opens):
                if consumed_replay[j]:
                    continue
                dt = abs(rts - live_ts)
                if dt <= match_ms and dt < best_dt:
                    best_idx = j
                    best_dt = dt
            if best_idx is None:
                per_sym_stats[sym]["missing_in_replay"] += 1
                continue
            consumed_replay[best_idx] = True
            rts, rside = replay_opens[best_idx]
            per_sym_stats[sym]["ts_errors_ms"].append(rts - live_ts)
            all_ts_errors_ms.append(rts - live_ts)
            if rside == live_side:
                per_sym_stats[sym]["exact_side_match"] += 1
            else:
                per_sym_stats[sym]["side_flip"] += 1

        # Unconsumed replay opens are replay-only
        per_sym_stats[sym]["replay_only"] = sum(1 for c in consumed_replay if not c)

    # Aggregate
    tot = {
        k: 0
        for k in (
            "live_n",
            "replay_n",
            "exact_side_match",
            "side_flip",
            "missing_in_replay",
            "replay_only",
        )
    }
    for s in per_sym_stats.values():
        for k in tot:
            tot[k] += s[k]

    matched_total = tot["exact_side_match"] + tot["side_flip"]
    precision = (matched_total / tot["replay_n"]) if tot["replay_n"] else 0.0
    recall = (matched_total / tot["live_n"]) if tot["live_n"] else 0.0
    side_agreement = (tot["exact_side_match"] / matched_total) if matched_total else 0.0

    median_ts_err = (
        sorted(all_ts_errors_ms)[len(all_ts_errors_ms) // 2] if all_ts_errors_ms else 0
    )

    print()
    print("=== aggregate ===")
    print(
        f"  matched (any side):    {matched_total}  ({matched_total / (tot['live_n'] or 1):.1%} of live)"
    )
    print(f"  exact side match:      {tot['exact_side_match']}")
    print(f"  side flips:            {tot['side_flip']}")
    print(f"  missing in replay:     {tot['missing_in_replay']}")
    print(f"  replay-only opens:     {tot['replay_only']}")
    print()
    print(f"  PRECISION:             {precision:.3f}  (matched / replay opens)")
    print(f"  RECALL:                {recall:.3f}  (matched / live entries)")
    print(f"  SIDE AGREEMENT:        {side_agreement:.3f}  (exact / matched)")
    print(f"  median ts error (ms):  {median_ts_err:+d}")

    # Per-symbol breakdown of worst offenders
    print()
    print("=== top-10 symbols by missing_in_replay ===")
    print(
        f"  {'sym':<14s}  {'live':>5s}  {'replay':>6s}  {'match':>5s}  {'flip':>4s}  {'missing':>7s}  {'replay_only':>11s}"
    )
    by_missing = sorted(
        per_sym_stats.items(), key=lambda kv: -kv[1]["missing_in_replay"]
    )
    for sym, s in by_missing[:10]:
        if s["live_n"] == 0 and s["replay_n"] == 0:
            continue
        print(
            f"  {sym:<14s}  {s['live_n']:>5d}  {s['replay_n']:>6d}  "
            f"{s['exact_side_match']:>5d}  {s['side_flip']:>4d}  "
            f"{s['missing_in_replay']:>7d}  {s['replay_only']:>11d}"
        )

    print()
    print("=== top-10 symbols by replay_only (over-firing) ===")
    print(
        f"  {'sym':<14s}  {'live':>5s}  {'replay':>6s}  {'match':>5s}  {'flip':>4s}  {'missing':>7s}  {'replay_only':>11s}"
    )
    by_replay_only = sorted(per_sym_stats.items(), key=lambda kv: -kv[1]["replay_only"])
    for sym, s in by_replay_only[:10]:
        if s["live_n"] == 0 and s["replay_n"] == 0:
            continue
        print(
            f"  {sym:<14s}  {s['live_n']:>5d}  {s['replay_n']:>6d}  "
            f"{s['exact_side_match']:>5d}  {s['side_flip']:>4d}  "
            f"{s['missing_in_replay']:>7d}  {s['replay_only']:>11d}"
        )

    print()
    print("=== decision ===")
    issues = []
    if precision < 0.70:
        issues.append(f"precision {precision:.3f} <0.70")
    if recall < 0.70:
        issues.append(f"recall {recall:.3f} <0.70")
    if side_agreement < 0.80:
        issues.append(f"side_agreement {side_agreement:.3f} <0.80")
    if issues:
        print("PROCEED to Hypothesis 3 (live-entry-anchored replay)")
        print(f"  triggered: {', '.join(issues)}")
        print("  candidate generation is the primary bottleneck.")
    else:
        print("DEPRIORITIZE — entry alignment is acceptable.")
        print("  gap is downstream (exits / fills / funding).")


if __name__ == "__main__":
    main()
