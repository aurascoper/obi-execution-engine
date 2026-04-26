#!/usr/bin/env python3
"""Phase B Hypothesis 3 — live-entry-anchored replay.

Decisive architecture experiment per the GPT-5.5 plan:

    Mode A  candidate replay (existing z_entry_replay_gated.py)
    Mode B  live-entry + replay-exit  (this script)
    Mode C  live-entry + live-exit    (this script, sanity check)

For each mode we compute per-symbol PnL and Pearson ρ vs the live
exit_signal-derived ground truth. Decision tree:

    Mode C ρ >= 0.90 AND Mode B ρ >= 0.70 → fix entry generation, target reachable
    Mode C ρ >= 0.90 AND Mode B ρ <  0.50 → exits also broken; bigger lift
    Mode C ρ <  0.80                       → validation math itself broken

Live entries here = hl_fill_received transitions where running per-symbol
position goes from 0 → non-zero (matches the actual_opens mode in
diagnose_entry_alignment.py). Live exits = the matching close transition.

Reuses load_bars / load_ticks / mark_at / trend_sma_at from
z_entry_replay_gated.py. Exit logic (X1-X4) is a controlled copy from the
same file — keeping this script self-contained avoids restructuring the
production replay during an experiment.

Usage:
    venv/bin/python3 scripts/replay_from_live_entries.py [--window-days 14]
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

# Reuse from z_entry_replay_gated.py
import sys

sys.path.insert(0, str(ROOT))
from scripts.z_entry_replay_gated import (  # noqa: E402
    Z4H_EXIT_MAP,
    SHOCK_ARM,
    SHOCK_STEP,
    RATCHET_TRANCHES,
    STOP_LOSS_PCT,
    TIME_STOP_S,
    MIN_HOLD_FOR_REVERT_S,
    MIN_REVERT_BPS,
    load_bars,
    load_ticks,
    mark_at,
    thresholds_for,
)
from scripts.validate_replay_fit import parse_hl_closed_pnl, pearson  # noqa: E402

WINDOW_DAYS_DEFAULT = 14


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


def _norm(s: str) -> str:
    return (s or "").replace("/USD", "").replace("/USDC", "")


def load_live_trades(from_ms: int, to_ms: int):
    """Reconstruct live trades from hl_fill_received as a list of dicts:
        {sym, entry_ts, entry_px, side, qty, exit_ts, exit_px, realized_pnl}

    A trade = position transitions from 0 to non-zero, then back to 0
    (or sign-flips, which we treat as close+open). Partial fills are
    aggregated weighted-average for entry; final exit is the fill that
    returns position to 0.

    Returns trades whose entry_ts is within [from_ms, to_ms).
    """
    fills_by_sym: dict[str, list[tuple[int, str, float, float]]] = defaultdict(list)
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
            sym = _norm(r.get("symbol") or r.get("coin") or "")
            if not sym:
                continue
            side_str = (r.get("side") or "").lower()
            if side_str not in ("buy", "sell"):
                continue
            try:
                sz = float(r.get("sz", 0))
                px = float(r.get("px", 0))
            except (TypeError, ValueError):
                continue
            if sz <= 0 or px <= 0:
                continue
            fills_by_sym[sym].append((ts, side_str, sz, px))

    trades: list[dict] = []
    for sym, fills in fills_by_sym.items():
        fills.sort()
        pos = 0.0
        wsum_px = 0.0  # weighted sum of px*sz on the open side
        wsum_sz = 0.0
        entry_ts = 0
        side_int = 0
        for ts, side_str, sz, px in fills:
            d = sz if side_str == "buy" else -sz
            new_pos = pos + d
            # Was flat, now non-flat → entry
            if abs(pos) < 1e-9 and abs(new_pos) > 1e-9:
                entry_ts = ts
                side_int = 1 if new_pos > 0 else -1
                wsum_px = px * abs(d)
                wsum_sz = abs(d)
            # Was non-flat, now flat → exit (full close)
            elif abs(pos) > 1e-9 and abs(new_pos) < 1e-9:
                if entry_ts and from_ms <= entry_ts < to_ms:
                    entry_px = wsum_px / wsum_sz if wsum_sz > 0 else px
                    realized = (px - entry_px) * side_int * abs(pos)
                    trades.append(
                        {
                            "sym": sym,
                            "entry_ts": entry_ts,
                            "entry_px": entry_px,
                            "side": side_int,
                            "qty": abs(pos),
                            "exit_ts": ts,
                            "exit_px": px,
                            "realized_pnl": realized,
                        }
                    )
                wsum_px = 0.0
                wsum_sz = 0.0
                entry_ts = 0
                side_int = 0
            # Same-side add → update weighted entry
            elif (pos > 0 and d > 0) or (pos < 0 and d < 0):
                wsum_px += px * abs(d)
                wsum_sz += abs(d)
            # Opposite-side reduction (not a close) — partial reduction:
            # keep entry baseline; realized portion not tracked here. Treat
            # the remaining position as still open.
            elif pos * new_pos > 0 and abs(d) < abs(pos):
                pass  # partial reduction; leaving entry_px/side_int unchanged
            # Sign flip (cross through zero)
            elif pos * new_pos < 0:
                # Close out at this fill, then re-open in opposite direction
                if entry_ts and from_ms <= entry_ts < to_ms:
                    entry_px = wsum_px / wsum_sz if wsum_sz > 0 else px
                    realized = (px - entry_px) * side_int * abs(pos)
                    trades.append(
                        {
                            "sym": sym,
                            "entry_ts": entry_ts,
                            "entry_px": entry_px,
                            "side": side_int,
                            "qty": abs(pos),
                            "exit_ts": ts,
                            "exit_px": px,
                            "realized_pnl": realized,
                        }
                    )
                # New trade in opposite direction
                entry_ts = ts
                side_int = 1 if new_pos > 0 else -1
                wsum_px = px * abs(new_pos)
                wsum_sz = abs(new_pos)
            pos = new_pos
    return trades


def replay_x1_x4_from_entry(
    sym: str,
    side: int,
    entry_ts: int,
    entry_px: float,
    qty: float,
    ticks_sym: list[tuple[int, float, float, float]],
    bars,
    thr: tuple[float, float, float, float],
) -> tuple[float, str]:
    """Walk forward from entry_ts; return (pnl, exit_reason).

    Exit logic mirrors simulate_symbol_gated's X1-X4 chain. If no exit
    fires before the tick stream ends, close at the final tick's mark.
    """
    z_entry, z_exit, z_short_entry, z_exit_short = thr
    ts_only = [t[0] for t in ticks_sym]
    idx = bisect_left(ts_only, entry_ts)
    rs = None  # ratchet state

    for i in range(idx, len(ticks_sym)):
        ts, z, obi, z4 = ticks_sym[i]
        if ts < entry_ts:
            continue
        age_s = (ts - entry_ts) / 1000.0
        cur = mark_at(bars, sym, ts)
        if cur is None:
            continue
        adverse = (cur - entry_px) / entry_px * side
        exit_reason = None

        # X1 z_revert (with live-engine dampers)
        z_revert_candidate = False
        if side == 1 and z >= z_exit:
            z_revert_candidate = True
        elif side == -1 and z <= z_exit_short:
            z_revert_candidate = True
        if z_revert_candidate:
            ex = Z4H_EXIT_MAP.get(sym)
            if ex is not None and z4 == z4:
                ex_long, ex_short = ex
                patient_block = (side == 1 and z4 < ex_long) or (
                    side == -1 and z4 > ex_short
                )
                if patient_block:
                    z_revert_candidate = False
            favorable = (cur - entry_px) / entry_px * side
            if z_revert_candidate and (
                age_s < MIN_HOLD_FOR_REVERT_S or favorable < MIN_REVERT_BPS
            ):
                z_revert_candidate = False
            if z_revert_candidate:
                exit_reason = "z_revert"

        # X2 ratchet
        if exit_reason is None and z4 == z4:
            if rs is None:
                if side * z4 >= SHOCK_ARM:
                    rs = {"peak_abs": abs(z4), "tranches_done": 0}
            else:
                if side * z4 > rs["peak_abs"]:
                    rs["peak_abs"] = side * z4
                retrace = rs["peak_abs"] - side * z4
                done = rs["tranches_done"]
                if done == 0 and retrace >= SHOCK_STEP:
                    exit_reason = "ratchet_1"
                elif done == 1 and retrace >= 2 * SHOCK_STEP:
                    exit_reason = "ratchet_2"
                elif done == 2 and (
                    retrace >= RATCHET_TRANCHES * SHOCK_STEP or side * z4 <= 0
                ):
                    exit_reason = "ratchet_final"

        if exit_reason is None and adverse <= -STOP_LOSS_PCT:
            exit_reason = "stop_loss"
        if exit_reason is None and age_s >= TIME_STOP_S:
            exit_reason = "time_stop"

        if exit_reason is not None:
            pnl = (cur - entry_px) * side * qty
            return pnl, exit_reason

    # No exit fired — close at final tick's mark.
    if ticks_sym:
        last_ts = ticks_sym[-1][0]
        cur = mark_at(bars, sym, last_ts) or entry_px
        pnl = (cur - entry_px) * side * qty
        return pnl, "window_end"
    return 0.0, "no_exit"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-days", type=int, default=WINDOW_DAYS_DEFAULT)
    args = ap.parse_args()

    to_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    from_ms = to_ms - args.window_days * 86_400_000

    print(f"# window: {args.window_days}d  [{from_ms}..{to_ms})")

    print("# loading bars + ticks...")
    bars = load_bars()
    ticks = load_ticks()
    print(f"#   bars symbols: {len(bars)}  ticks symbols: {len(ticks)}")

    print("# reconstructing live trades from fills...")
    trades = load_live_trades(from_ms, to_ms)
    print(f"#   live trades: {len(trades)}")

    print("# loading HL closedPnl per-symbol (venue ground truth)...")
    live_per_sym, _, live_fees = parse_hl_closed_pnl(from_ms, to_ms)
    print(
        f"#   HL closedPnl symbols: {len(live_per_sym)}  "
        f"(gross ${sum(live_per_sym.values()):+.2f}, fees ${sum(live_fees.values()):+.2f})"
    )

    # Mode B: live entries → replay X1-X4 exits
    mode_b_per_sym: dict[str, float] = defaultdict(float)
    mode_b_exits: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    n_mode_b_trades = 0
    n_no_ticks = 0
    for trade in trades:
        sym = trade["sym"]
        ticks_sym = ticks.get(sym, [])
        if len(ticks_sym) < 2:
            n_no_ticks += 1
            continue
        thr = thresholds_for(sym)
        pnl, reason = replay_x1_x4_from_entry(
            sym=sym,
            side=trade["side"],
            entry_ts=trade["entry_ts"],
            entry_px=trade["entry_px"],
            qty=trade["qty"],
            ticks_sym=ticks_sym,
            bars=bars,
            thr=thr,
        )
        mode_b_per_sym[sym] += pnl
        mode_b_exits[sym][reason] += 1
        n_mode_b_trades += 1

    # Mode C: live entries → live exits (just sum realized_pnl per symbol)
    mode_c_per_sym: dict[str, float] = defaultdict(float)
    for trade in trades:
        mode_c_per_sym[trade["sym"]] += trade["realized_pnl"]

    # Compute ρ vs live_per_sym for B and C
    shared_b = sorted(set(live_per_sym) & set(mode_b_per_sym))
    shared_c = sorted(set(live_per_sym) & set(mode_c_per_sym))

    live_vec_b = [live_per_sym[s] for s in shared_b]
    b_vec = [mode_b_per_sym[s] for s in shared_b]
    rho_b = pearson(b_vec, live_vec_b) if shared_b else None

    live_vec_c = [live_per_sym[s] for s in shared_c]
    c_vec = [mode_c_per_sym[s] for s in shared_c]
    rho_c = pearson(c_vec, live_vec_c) if shared_c else None

    print()
    print("=== Mode A — candidate replay (existing) ===")
    print("  ρ (last measured)                    = 0.1467 (from notional rescale run)")
    print("  baseline reference, NOT recomputed here")

    print()
    print("=== Mode B — live-entry + replay-exit ===")
    print(f"  trades simulated:        {n_mode_b_trades}")
    print(f"  trades skipped (no ticks): {n_no_ticks}")
    print(f"  overlap symbols vs live: {len(shared_b)}")
    print(
        f"  ρ (Mode B)               = {rho_b:.4f}" if rho_b is not None else "  ρ N/A"
    )

    # Per-symbol top-10 by |live|
    print("\n  top-10 by |live| live vs Mode B:")
    print(f"    {'sym':<14s}  {'live':>9s}  {'mode_b':>9s}")
    for sym in sorted(shared_b, key=lambda s: -abs(live_per_sym[s]))[:10]:
        print(
            f"    {sym:<14s}  {live_per_sym[sym]:>+9.2f}  {mode_b_per_sym[sym]:>+9.2f}"
        )

    # Aggregate exit reason mix for Mode B
    mode_b_total_exits = defaultdict(int)
    for sym_exits in mode_b_exits.values():
        for reason, count in sym_exits.items():
            mode_b_total_exits[reason] += count
    print("\n  Mode B exit-reason mix:")
    for reason, count in sorted(mode_b_total_exits.items(), key=lambda kv: -kv[1]):
        print(f"    {reason:14s}  {count:5d}")

    print()
    print("=== Mode C — live-entry + live-exit (sanity check) ===")
    print(f"  trades:                  {len(trades)}")
    print(f"  overlap symbols vs live: {len(shared_c)}")
    print(
        f"  ρ (Mode C)               = {rho_c:.4f}" if rho_c is not None else "  ρ N/A"
    )

    print("\n  top-10 by |live| live vs Mode C:")
    print(f"    {'sym':<14s}  {'live':>9s}  {'mode_c':>9s}")
    for sym in sorted(shared_c, key=lambda s: -abs(live_per_sym[s]))[:10]:
        print(
            f"    {sym:<14s}  {live_per_sym[sym]:>+9.2f}  {mode_c_per_sym[sym]:>+9.2f}"
        )

    print()
    print("=== decision ===")
    if rho_c is None or rho_b is None:
        print("  ρ values not computable; insufficient data overlap.")
        return
    if rho_c < 0.80:
        print(f"  Mode C ρ={rho_c:.3f} < 0.80")
        print("  → validation math is itself off (live PnL extraction or trade")
        print("    reconstruction has an issue). Investigate before further work.")
    elif rho_b >= 0.70:
        print(f"  Mode B ρ={rho_b:.3f} >= 0.70 AND Mode C ρ={rho_c:.3f} >= 0.80")
        print("  → entry generation is the primary blocker.")
        print("    Path to ρ≥0.75 = anchor candidate replay to live entry signals,")
        print("    OR pivot validation to use Mode B as the autoresearch eval.")
    elif rho_b >= 0.50:
        print(f"  Mode B ρ={rho_b:.3f} in [0.50, 0.70); Mode C ρ={rho_c:.3f} >= 0.80")
        print("  → entry AND exit both contribute. Add exit modeling fixes")
        print(
            "    (missing exit reasons, fill model, funding) on top of entry anchoring."
        )
    else:
        print(
            f"  Mode B ρ={rho_b:.3f} < 0.50 even with live entries; Mode C ρ={rho_c:.3f} >= 0.80"
        )
        print("  → exits are also broken. Candidate-driven replay likely cannot")
        print(
            "    reach ρ≥0.75. Pivot to event-audit replay for live promotion gating."
        )


if __name__ == "__main__":
    main()
