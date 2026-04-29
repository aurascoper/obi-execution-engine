#!/usr/bin/env python3
"""Live-session-anchored replay (Mode 1 audit).

Hypothesis being tested:
    Replay's residual is dominated by trade-cardinality / session-boundary
    mismatch. If replay used live's actual session boundaries (entry/exit
    timestamps and prices), how close does its per-symbol PnL get to HL
    closedPnl truth?

This module DOES NOT ship a deployable strategy. It tests whether
session-level abstraction is the missing layer before committing to a
multi-day Mode-2 (policy) implementation.

Method (Mode 1 — audit / oracle):

  1. Walk hl_fill_received forward; reconstruct live SESSIONS per symbol:
        flat → nonzero exposure  = session_open
        nonzero → flat            = session_close
        sign-flip (cross zero)    = session_close + session_open
     For each session, capture: open_ts, open_px (vwap of opening fills),
     close_ts, close_px (vwap of closing fills), peak_qty, side,
     live_session_closed_pnl (sum of closed_pnl across all reduction
     fills in the session), live_session_fees.

  2. For each live session, compute "audit replay PnL" =
         side · peak_qty · (close_px − open_px)
     This is the PnL replay would realize if it perfectly mimicked live's
     session boundaries with no premature exit chain.

  3. Aggregate per-symbol audit PnL; correlate vs HL closedPnl per
     symbol over the same window. ρ.

Decision rules (per the spec):
    14d ρ ≥ 0.70 AND 7d improves   → session abstraction is THE layer;
                                      proceed to Mode 2 policy implementation
    14d ρ < 0.55                   → sessionization alone insufficient;
                                      residual is from manual closes,
                                      funding, etc.

NOT touching: hl_engine.py, strategy/, risk/, maker_engine.py.

Usage:
    venv/bin/python3 scripts/replay_position_sessions.py
    venv/bin/python3 scripts/replay_position_sessions.py --window-days 14
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / "hl_engine.jsonl"
DEFAULT_OUT = ROOT / "autoresearch_gated" / "replay_position_sessions.json"

sys.path.insert(0, str(ROOT))
from scripts.validate_replay_fit import parse_hl_closed_pnl, pearson  # noqa: E402

FOCUS_SYMS = ["AAVE", "ZEC", "xyz:MSTR"]


def _parse_ts_ms(s) -> int:
    if isinstance(s, (int, float)):
        return int(s * 1000) if s < 1e12 else int(s)
    if isinstance(s, str):
        try:
            x = s[:-1] + "+00:00" if s.endswith("Z") else s
            return int(dt.datetime.fromisoformat(x).timestamp() * 1000)
        except Exception:
            return 0
    return 0


def _norm(s: str) -> str:
    return (s or "").replace("/USD", "").replace("/USDC", "")


# ── Session reconstruction ────────────────────────────────────────────────
def reconstruct_all_sessions() -> dict[str, list[dict]]:
    """Walk hl_fill_received chronologically; return per-symbol sessions.

    Returns ALL sessions ever seen (no window filter applied here — clipping
    is done downstream so we can handle pre-window sessions correctly).

    Each session record:
        open_ts, open_px, open_qty (initial qty at flat→nonzero)
        close_ts, close_px (None if still open at end of fill stream)
        peak_qty, side ('long' or 'short')
        live_session_closed_pnl   sum closed_pnl across reduction fills
        live_session_fees          sum fee
        n_fills, n_opens, n_reductions
        trajectory                 [(ts_ms, signed_pos_after_fill), ...]
    """
    fills_by_sym: dict[str, list[dict]] = defaultdict(list)
    if not LOG.exists():
        return {}

    with LOG.open() as fh:
        for line in fh:
            if '"hl_fill_received"' not in line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get("event") != "hl_fill_received":
                continue
            ts_ms = _parse_ts_ms(o.get("timestamp", ""))
            if ts_ms <= 0:
                continue
            sym = _norm(o.get("symbol") or o.get("coin") or "")
            if not sym:
                continue
            try:
                sz = float(o.get("sz", 0) or 0)
                px = float(o.get("px", 0) or 0)
            except (TypeError, ValueError):
                continue
            side = (o.get("side") or "").lower()
            if sz <= 0 or px <= 0 or side not in ("buy", "sell"):
                continue
            try:
                cp = float(o.get("closed_pnl", 0) or 0)
            except (TypeError, ValueError):
                cp = 0.0
            try:
                fee = float(o.get("fee", 0) or 0)
            except (TypeError, ValueError):
                fee = 0.0
            fills_by_sym[sym].append(
                {
                    "ts_ms": ts_ms,
                    "side": side,
                    "sz": sz,
                    "px": px,
                    "closed_pnl": cp,
                    "fee": fee,
                }
            )

    sessions_by_sym: dict[str, list[dict]] = defaultdict(list)
    for sym, fills in fills_by_sym.items():
        fills.sort(key=lambda f: f["ts_ms"])
        pos = 0.0
        cur: dict | None = None
        for f in fills:
            ts_ms = f["ts_ms"]
            sz = f["sz"]
            px = f["px"]
            d = sz if f["side"] == "buy" else -sz
            new_pos = pos + d

            opening = abs(pos) < 1e-9 and abs(new_pos) > 1e-9
            closing = abs(pos) > 1e-9 and abs(new_pos) < 1e-9
            sign_flip = (pos * new_pos) < 0

            if sign_flip:
                if cur is not None:
                    cur["close_ts"] = ts_ms
                    cur["close_px"] = px
                    cur["live_session_closed_pnl"] += f["closed_pnl"]
                    cur["live_session_fees"] += f["fee"]
                    cur["n_fills"] += 1
                    cur["n_reductions"] += 1
                    cur["trajectory"].append((ts_ms, new_pos))
                    sessions_by_sym[sym].append(cur)
                cur = {
                    "symbol": sym,
                    "open_ts": ts_ms,
                    "open_px": px,
                    "side": "long" if new_pos > 0 else "short",
                    "open_qty": abs(new_pos),
                    "peak_qty": abs(new_pos),
                    "n_fills": 1,
                    "n_opens": 1,
                    "n_reductions": 0,
                    "live_session_closed_pnl": f["closed_pnl"],
                    "live_session_fees": f["fee"],
                    "close_ts": None,
                    "close_px": None,
                    "trajectory": [(ts_ms, new_pos)],
                }
            elif opening:
                cur = {
                    "symbol": sym,
                    "open_ts": ts_ms,
                    "open_px": px,
                    "side": "long" if new_pos > 0 else "short",
                    "open_qty": abs(new_pos),
                    "peak_qty": abs(new_pos),
                    "n_fills": 1,
                    "n_opens": 1,
                    "n_reductions": 0,
                    "live_session_closed_pnl": f["closed_pnl"],
                    "live_session_fees": f["fee"],
                    "close_ts": None,
                    "close_px": None,
                    "trajectory": [(ts_ms, new_pos)],
                }
            elif closing:
                if cur is not None:
                    cur["close_ts"] = ts_ms
                    cur["close_px"] = px
                    cur["live_session_closed_pnl"] += f["closed_pnl"]
                    cur["live_session_fees"] += f["fee"]
                    cur["n_fills"] += 1
                    cur["n_reductions"] += 1
                    cur["trajectory"].append((ts_ms, new_pos))
                    sessions_by_sym[sym].append(cur)
                cur = None
            else:
                if cur is not None:
                    cur["n_fills"] += 1
                    cur["live_session_closed_pnl"] += f["closed_pnl"]
                    cur["live_session_fees"] += f["fee"]
                    cur["trajectory"].append((ts_ms, new_pos))
                    if abs(new_pos) > abs(pos):
                        cur["n_opens"] += 1
                        if abs(new_pos) > cur["peak_qty"]:
                            cur["peak_qty"] = abs(new_pos)
                    else:
                        cur["n_reductions"] += 1
            pos = new_pos

        # session still open at end of fill stream — leave close_ts = None
        if cur is not None:
            sessions_by_sym[sym].append(cur)

    return dict(sessions_by_sym)


# ── Session window-clipping with mark_at lookup ───────────────────────────
def fetch_marks(symbols: list[str], window_start_ms: int, window_end_ms: int) -> dict:
    """For each symbol, fetch a 1h candle range. Native symbols use the
    main MAINNET Info; HIP-3/builder symbols (xyz:, vntl:, hyna:, flx:, km:,
    cash:, para:) need a per-DEX Info instance.

    Returns {sym: [(ts_open, close), ...]} sorted ascending.
    """
    from hyperliquid.info import Info
    from hyperliquid.utils import constants

    pad_ms = 6 * 3_600_000
    fetch_from = window_start_ms - pad_ms
    fetch_to = window_end_ms + pad_ms

    # Group symbols by source: native + per-DEX
    by_dex: dict[str | None, list[str]] = {None: []}
    DEX_PREFIXES = ("xyz", "vntl", "hyna", "flx", "km", "cash", "para")
    for sym in symbols:
        dex = None
        for d in DEX_PREFIXES:
            if sym.startswith(f"{d}:"):
                dex = d
                break
        by_dex.setdefault(dex, []).append(sym)

    # Per-DEX Info instance + candle fetch
    marks: dict[str, list[tuple[int, float]]] = {}
    info_cache: dict[str | None, "Info"] = {}
    for dex, syms in by_dex.items():
        if not syms:
            continue
        if dex not in info_cache:
            try:
                kw = dict(skip_ws=True)
                if dex:
                    kw["perp_dexs"] = [dex]
                info_cache[dex] = Info(constants.MAINNET_API_URL, **kw)
            except Exception as e:
                print(f"# warn: Info(dex={dex}) init failed: {e}", file=sys.stderr)
                continue
        info = info_cache[dex]
        for sym in syms:
            try:
                raw = (
                    info.candles_snapshot(
                        name=sym, interval="1h", startTime=fetch_from, endTime=fetch_to
                    )
                    or []
                )
            except Exception as e:
                print(
                    f"# warn: candles {sym} (dex={dex or 'native'}) failed: {e}",
                    file=sys.stderr,
                )
                continue
            bars = []
            for r in raw:
                try:
                    t_open = int(r.get("t", 0))
                    close = float(r.get("c", 0))
                    if close > 0:
                        bars.append((t_open, close))
                except (TypeError, ValueError):
                    continue
            bars.sort()
            if bars:
                marks[sym] = bars
    return marks


def mark_at(sym: str, ts_ms: int, marks: dict) -> float | None:
    """Return close price of the 1h candle whose open is the latest <= ts_ms."""
    bars = marks.get(sym)
    if not bars:
        return None
    # bars sorted ascending by open ts
    chosen = None
    for t_open, close in bars:
        if t_open <= ts_ms:
            chosen = close
        else:
            break
    if chosen is None and bars:
        chosen = bars[0][1]
    return chosen


def peak_qty_in_range(session: dict, start_ms: int, end_ms: int) -> float:
    """Maximum |signed_pos| across the trajectory whose ts ∈ [start_ms, end_ms].

    For sessions opened pre-window, the qty at window_start is the latest
    trajectory entry whose ts < start_ms. Include that as the baseline.
    """
    trajectory = session.get("trajectory") or []
    pos_at_start = None
    in_range_max = 0.0
    for ts, pos in trajectory:
        if ts < start_ms:
            pos_at_start = abs(pos)
        elif ts <= end_ms:
            if abs(pos) > in_range_max:
                in_range_max = abs(pos)
    # Combine: position carried in at window_start + any in-window peaks
    candidates = []
    if pos_at_start is not None:
        candidates.append(pos_at_start)
    if in_range_max > 0:
        candidates.append(in_range_max)
    if not candidates:
        # Fallback: use session peak_qty
        return session.get("peak_qty", 0.0)
    return max(candidates)


def clip_session_to_window(
    session: dict, from_ms: int, to_ms: int, marks: dict
) -> dict | None:
    """Return clipped audit record or None if session doesn't overlap window.

    Overlap test: open_ts < to_ms AND (close_ts is None OR close_ts > from_ms).
    """
    open_ts = session["open_ts"]
    close_ts = session["close_ts"]
    if not (open_ts < to_ms and (close_ts is None or close_ts > from_ms)):
        return None

    sym = session["symbol"]
    if open_ts < from_ms:
        eff_entry_ts = from_ms
        eff_entry_px = mark_at(sym, from_ms, marks)
    else:
        eff_entry_ts = open_ts
        eff_entry_px = session["open_px"]
    if close_ts is None or close_ts > to_ms:
        eff_exit_ts = to_ms
        eff_exit_px = mark_at(sym, to_ms, marks)
    else:
        eff_exit_ts = close_ts
        eff_exit_px = session["close_px"]

    if (
        eff_entry_px is None
        or eff_exit_px is None
        or eff_entry_px <= 0
        or eff_exit_px <= 0
    ):
        return None

    qty = peak_qty_in_range(session, eff_entry_ts, eff_exit_ts)
    if qty <= 0:
        return None

    side_sign = 1.0 if session["side"] == "long" else -1.0
    pnl = side_sign * qty * (eff_exit_px - eff_entry_px)
    return {
        "symbol": sym,
        "side": session["side"],
        "open_ts": open_ts,
        "close_ts": close_ts,
        "eff_entry_ts": eff_entry_ts,
        "eff_exit_ts": eff_exit_ts,
        "eff_entry_px": eff_entry_px,
        "eff_exit_px": eff_exit_px,
        "qty_for_window": qty,
        "audit_pnl": pnl,
        "duration_in_window_s": (eff_exit_ts - eff_entry_ts) / 1000.0,
        "live_session_closed_pnl": session.get("live_session_closed_pnl", 0.0),
        "open_pre_window": open_ts < from_ms,
        "close_post_window": close_ts is None or close_ts > to_ms,
    }


# ── Audit metric ──────────────────────────────────────────────────────────
def audit_session_pnl(session: dict) -> float:
    """Replay-side PnL if it perfectly mimicked live's session boundaries:
        side_sign · peak_qty · (close_px − open_px)
    Uses peak_qty so adds during the session count toward exposure.
    """
    side_sign = 1.0 if session["side"] == "long" else -1.0
    return side_sign * session["peak_qty"] * (session["close_px"] - session["open_px"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-days", type=int, default=14)
    ap.add_argument("--also-7d", action="store_true", default=True)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    summaries = []
    windows = [args.window_days]
    if args.also_7d and args.window_days != 7:
        windows.append(7)

    print(
        "# reconstructing ALL sessions (no window filter at this layer) ...",
        file=sys.stderr,
    )
    all_sessions_by_sym = reconstruct_all_sessions()

    for w in windows:
        to_ms = int(dt.datetime.now(tz=dt.timezone.utc).timestamp() * 1000)
        from_ms = to_ms - w * 86_400_000

        print(f"\n## window {w}d  [{from_ms}..{to_ms})", file=sys.stderr)

        # Identify symbols with any session overlapping window (for candle fetch)
        active_syms = set()
        for sym, sess in all_sessions_by_sym.items():
            for s in sess:
                if s["open_ts"] < to_ms and (
                    s["close_ts"] is None or s["close_ts"] > from_ms
                ):
                    active_syms.add(sym)
                    break

        print(
            f"# fetching marks for {len(active_syms)} active symbols ...",
            file=sys.stderr,
        )
        marks = fetch_marks(sorted(active_syms), from_ms, to_ms)

        print("# clipping sessions to window ...", file=sys.stderr)
        clipped_by_sym: dict[str, list[dict]] = defaultdict(list)
        n_sessions_total = 0
        n_pre_window = 0
        n_post_window = 0
        total_dur_s = 0.0
        for sym, sess in all_sessions_by_sym.items():
            for s in sess:
                clip = clip_session_to_window(s, from_ms, to_ms, marks)
                if clip is None:
                    continue
                clipped_by_sym[sym].append(clip)
                n_sessions_total += 1
                if clip["open_pre_window"]:
                    n_pre_window += 1
                if clip["close_post_window"]:
                    n_post_window += 1
                total_dur_s += clip["duration_in_window_s"]

        print("# pulling HL closedPnl ...", file=sys.stderr)
        hl_pnl, _per_day, _hl_fees = parse_hl_closed_pnl(from_ms, to_ms)

        # per-symbol audit PnL
        audit_by_sym: dict[str, float] = {}
        per_sym_n_sessions: dict[str, int] = {}
        per_sym_mean_dur_s: dict[str, float] = {}
        for sym, sess in clipped_by_sym.items():
            audit_by_sym[sym] = sum(s["audit_pnl"] for s in sess)
            per_sym_n_sessions[sym] = len(sess)
            durs = [s["duration_in_window_s"] for s in sess]
            per_sym_mean_dur_s[sym] = sum(durs) / len(durs) if durs else 0.0

        shared = sorted(set(audit_by_sym) & set(hl_pnl))
        rho = pearson([audit_by_sym[s] for s in shared], [hl_pnl[s] for s in shared])
        replay_total = sum(audit_by_sym.values())
        live_total = sum(hl_pnl.values())

        # top-10 abs residual
        residuals = []
        for s in shared:
            r = hl_pnl[s] - audit_by_sym[s]
            residuals.append(
                {
                    "sym": s,
                    "hl": hl_pnl[s],
                    "audit": audit_by_sym[s],
                    "residual": r,
                    "abs_residual": abs(r),
                }
            )
        residuals.sort(key=lambda r: -r["abs_residual"])
        top10_abs_sum = sum(r["abs_residual"] for r in residuals[:10])

        # Focus residuals
        focus = {}
        for sym in FOCUS_SYMS:
            r = next((x for x in residuals if x["sym"] == sym), None)
            focus[sym] = r["residual"] if r else None

        # Session-count comparison
        n_replay_opens_total = (
            n_sessions_total  # in audit, replay sessions == live sessions
        )
        mean_session_dur_s = (
            total_dur_s / n_sessions_total if n_sessions_total > 0 else 0.0
        )

        summary = {
            "window_days": w,
            "from_ms": from_ms,
            "to_ms": to_ms,
            "n_symbols_with_sessions": len(clipped_by_sym),
            "n_sessions_total": n_sessions_total,
            "shared_n": len(shared),
            "rho": rho,
            "audit_replay_total": replay_total,
            "live_total": live_total,
            "top10_abs_residual_sum": top10_abs_sum,
            "focus": focus,
            "mean_session_duration_s": mean_session_dur_s,
            "top10_residuals": residuals[:10],
        }
        summaries.append(summary)

        # Console summary per window
        print(
            f"  shared symbols: {len(shared)}  total clipped sessions: {n_sessions_total}"
        )
        print(f"    pre-window opens:  {n_pre_window}")
        print(f"    post-window opens: {n_post_window}")
        print(
            f"  ρ (audit vs HL closedPnl): {rho:+.4f}"
            if rho is not None
            else "  ρ: N/A"
        )
        print(f"  audit replay total: ${replay_total:+.2f}")
        print(f"  live total:         ${live_total:+.2f}")
        print(
            f"  mean window-clipped session duration: {mean_session_dur_s / 3600:.2f}h"
        )
        print(f"  top-10 abs residual sum: ${top10_abs_sum:.2f}")
        print(
            f"  AAVE residual:    {('$' + format(focus.get('AAVE'), '+.2f')) if focus.get('AAVE') is not None else 'N/A'}"
        )
        print(
            f"  ZEC residual:     {('$' + format(focus.get('ZEC'), '+.2f')) if focus.get('ZEC') is not None else 'N/A'}"
        )
        print(
            f"  xyz:MSTR residual:{('$' + format(focus.get('xyz:MSTR'), '+.2f')) if focus.get('xyz:MSTR') is not None else 'N/A'}"
        )
        print()
        print("  top-5 |residual| symbols:")
        for r in residuals[:5]:
            print(
                f"    {r['sym']:14s}  hl=${r['hl']:>+9.2f}  audit=${r['audit']:>+9.2f}  resid=${r['residual']:>+9.2f}"
            )

    # Decision verdict (anchored to 14d window primarily)
    print("\n=== DECISION ===")
    main_summary = next((s for s in summaries if s["window_days"] == 14), summaries[0])
    rho14 = main_summary.get("rho")
    rho7 = next((s["rho"] for s in summaries if s["window_days"] == 7), None)
    if rho14 is None:
        print("  insufficient data for 14d ρ")
    else:
        print(f"  14d audit ρ: {rho14:+.4f}")
        if rho7 is not None:
            print(f"  7d audit ρ: {rho7:+.4f}")
        if rho14 >= 0.70:
            print("  → 14d ρ ≥ 0.70: SESSION ABSTRACTION IS THE MISSING LAYER")
            print("    proceed to Mode 2 (policy-mode session replay)")
        elif rho14 < 0.55:
            print("  → 14d ρ < 0.55: SESSIONIZATION ALONE INSUFFICIENT")
            print(
                "    residual not from session boundaries; investigate manual closes,"
            )
            print(
                "    funding, multi-fill VWA carefully before any policy implementation"
            )
        else:
            print(
                "  → 14d ρ in [0.55, 0.70): MIXED signal; needs further decomposition"
            )
            print("    before committing to Mode 2")

    out_path.write_text(json.dumps(summaries, indent=2, default=str))
    print(f"\n# wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
