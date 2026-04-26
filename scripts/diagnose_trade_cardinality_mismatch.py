#!/usr/bin/env python3
"""Trade-cardinality mismatch diagnostic — replay vs live, cross-sectional.

Hypothesis (after ratchet & topup-VWA falsifications):
    Replay over-trades. It opens many short round-trips while live holds
    few sustained sessions. The PnL gap is dominated by trade-cardinality
    and holding-duration mismatch, not entry size or exit slicing.

Method:
  - Run baseline full-mode replay; emit per-trade records via
    REPLAY_TRADES_OUT.
  - Walk hl_fill_received in window; identify live "sessions" via
    flat → nonzero → flat transitions on running signed qty.
  - Per shared symbol compute n_replay_opens, n_live_sessions, durations,
    notional turnover.
  - Correlate |residual| against:
      open-count delta = |n_replay_opens − n_live_sessions|
      open-count ratio = n_replay_opens / max(1, n_live_sessions)
      hold-duration delta = |replay_total_hold_s − live_total_session_s|
      hold ratio = replay_mean_hold_s / max(1, live_mean_session_s)
      notional turnover delta = |replay_turnover − live_turnover|
  - Compute Pearson AND Spearman (Spearman is the safer one when ZEC
    dominates).

Falsifier:
  Pearson AND Spearman r(|residual|, open-count delta or ratio) < 0.35
  AND top-residual median open_count_ratio < 3×

Confirmed (proceed to bounded experiments):
  Spearman(|residual|, open_count_ratio) ≥ 0.50
  OR top-10 residual median open_count_ratio ≥ 5×
  OR top-residual median (live_session_s / replay_hold_s) ≥ 5×
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / "hl_engine.jsonl"

sys.path.insert(0, str(ROOT))
from scripts.validate_replay_fit import parse_hl_closed_pnl, pearson  # noqa: E402


def _norm(s: str) -> str:
    return (s or "").replace("/USD", "").replace("/USDC", "")


def _parse_ts(s) -> int:
    if isinstance(s, (int, float)):
        return int(s * 1000) if s < 1e12 else int(s)
    if isinstance(s, str):
        try:
            x = s[:-1] + "+00:00" if s.endswith("Z") else s
            return int(dt.datetime.fromisoformat(x).timestamp() * 1000)
        except Exception:
            return 0
    return 0


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Spearman rank correlation. Returns None if undefined."""
    n = len(xs)
    if n < 2 or len(ys) != n:
        return None

    def rank(values):
        order = sorted(range(n), key=lambda i: values[i])
        ranks = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and values[order[j + 1]] == values[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1  # 1-based avg rank
            for k in range(i, j + 1):
                ranks[order[k]] = avg
            i = j + 1
        return ranks

    rx = rank(xs)
    ry = rank(ys)
    return pearson(rx, ry)


def replay_trades(from_ms: int, to_ms: int):
    """Run baseline full-mode replay, capture per-trade records and per-symbol PnL."""
    env = os.environ.copy()
    env["REPLAY_FROM_MS"] = str(from_ms)
    env["REPLAY_TO_MS"] = str(to_ms)
    env["RATCHET_EXIT_MODEL"] = "full"
    with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False) as tmp_p:
        env["REPLAY_PERSYM_OUT"] = tmp_p.name
        pnl_path = tmp_p.name
    with tempfile.NamedTemporaryFile("w+", suffix=".jsonl", delete=False) as tmp_t:
        env["REPLAY_TRADES_OUT"] = tmp_t.name
        trades_path = tmp_t.name
    cmd = [sys.executable, str(ROOT / "scripts" / "z_entry_replay_gated.py")]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, cwd=ROOT)
    if r.returncode != 0:
        print(r.stderr[-1000:], file=sys.stderr)
        raise SystemExit(f"replay failed rc={r.returncode}")
    pnl = json.loads(Path(pnl_path).read_text())
    trades_by_sym: dict[str, list[dict]] = defaultdict(list)
    with open(trades_path) as fh:
        for line in fh:
            try:
                t = json.loads(line)
            except Exception:
                continue
            sym = _norm(t.get("symbol", ""))
            if sym:
                trades_by_sym[sym].append(t)
    Path(pnl_path).unlink(missing_ok=True)
    Path(trades_path).unlink(missing_ok=True)
    return pnl, dict(trades_by_sym)


def live_sessions(from_ms: int, to_ms: int):
    """Walk hl_fill_received: identify live sessions per symbol.
    Session = flat → nonzero (open) → flat (close).

    Returns per-symbol:
      n_sessions, sessions: [{open_ts, close_ts, peak_qty, open_px, close_px}]
    Falls back to sessions with open_ts in [from_ms, to_ms) only.
    """
    fills_by_sym: dict[str, list[tuple[int, str, float, float]]] = defaultdict(list)
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
            ts_ms = _parse_ts(o.get("timestamp", ""))
            if ts_ms <= 0:
                continue
            sym = _norm(o.get("symbol") or o.get("coin") or "")
            if not sym:
                continue
            try:
                sz = float(o.get("sz", 0) or 0)
                px = float(o.get("px", 0) or 0)
                side = (o.get("side") or "").lower()
            except (TypeError, ValueError):
                continue
            if sz <= 0 or side not in ("buy", "sell"):
                continue
            fills_by_sym[sym].append((ts_ms, side, sz, px))

    sessions: dict[str, list[dict]] = defaultdict(list)
    for sym, fills in fills_by_sym.items():
        fills.sort()
        pos = 0.0
        cur_session: dict | None = None
        for ts_ms, side, sz, px in fills:
            d = sz if side == "buy" else -sz
            new_pos = pos + d
            # flat → nonzero
            if abs(pos) < 1e-9 and abs(new_pos) > 1e-9:
                cur_session = {
                    "open_ts": ts_ms,
                    "open_px": px,
                    "side": "long" if new_pos > 0 else "short",
                    "peak_qty": abs(new_pos),
                }
            # nonzero → flat (or sign-flip is treated as close+open)
            if abs(pos) > 1e-9 and abs(new_pos) < 1e-9:
                if cur_session is not None:
                    cur_session["close_ts"] = ts_ms
                    cur_session["close_px"] = px
                    if cur_session["open_ts"] >= from_ms and cur_session["open_ts"] < to_ms:
                        sessions[sym].append(cur_session)
                cur_session = None
            elif pos * new_pos < 0:
                # sign-flip: close prev, open new
                if cur_session is not None:
                    cur_session["close_ts"] = ts_ms
                    cur_session["close_px"] = px
                    if cur_session["open_ts"] >= from_ms and cur_session["open_ts"] < to_ms:
                        sessions[sym].append(cur_session)
                cur_session = {
                    "open_ts": ts_ms,
                    "open_px": px,
                    "side": "long" if new_pos > 0 else "short",
                    "peak_qty": abs(new_pos),
                }
            else:
                # same-direction add or partial reduction
                if cur_session is not None and abs(new_pos) > cur_session["peak_qty"]:
                    cur_session["peak_qty"] = abs(new_pos)
            pos = new_pos
        # If session never closes (held through end), record with synthetic close at to_ms
        if cur_session is not None:
            cur_session["close_ts"] = to_ms
            cur_session["close_px"] = cur_session["open_px"]  # unknown
            if cur_session["open_ts"] >= from_ms and cur_session["open_ts"] < to_ms:
                sessions[sym].append(cur_session)
    return dict(sessions)


def run_window(window_days: int):
    to_ms = int(dt.datetime.now(tz=dt.timezone.utc).timestamp() * 1000)
    from_ms = to_ms - window_days * 86_400_000
    print(f"\n## window {window_days}d  [{from_ms}..{to_ms})", file=sys.stderr)

    print("# pulling HL closedPnl ...", file=sys.stderr)
    hl_pnl, _per_day, _hl_fees = parse_hl_closed_pnl(from_ms, to_ms)

    print("# running replay (full mode) with REPLAY_TRADES_OUT ...", file=sys.stderr)
    sim_pnl, replay_trades_by_sym = replay_trades(from_ms, to_ms)

    print("# building live sessions from hl_fill_received ...", file=sys.stderr)
    live_sess = live_sessions(from_ms, to_ms)

    shared = sorted(set(sim_pnl) & set(hl_pnl))
    rows = []
    for s in shared:
        rt = replay_trades_by_sym.get(s, [])
        ls = live_sess.get(s, [])
        n_replay_opens = len(rt)
        n_live_sessions = len(ls)
        replay_total_hold_s = sum(
            max(0, (t["exit_ts"] - t["entry_ts"]) / 1000.0) for t in rt
        )
        replay_mean_hold_s = (
            replay_total_hold_s / n_replay_opens if n_replay_opens > 0 else 0.0
        )
        live_total_s = sum(
            max(0, (sess["close_ts"] - sess["open_ts"]) / 1000.0) for sess in ls
        )
        live_mean_s = live_total_s / n_live_sessions if n_live_sessions > 0 else 0.0
        replay_turnover = sum(t["initial_qty"] * t["entry_vwap"] for t in rt)
        live_turnover = sum(sess["peak_qty"] * sess["open_px"] for sess in ls)
        residual = hl_pnl[s] - sim_pnl[s]
        rows.append(
            {
                "sym": s,
                "hl": hl_pnl[s],
                "sim": sim_pnl[s],
                "residual": residual,
                "abs_residual": abs(residual),
                "n_replay_opens": n_replay_opens,
                "n_live_sessions": n_live_sessions,
                "open_count_delta": abs(n_replay_opens - n_live_sessions),
                "open_count_ratio": n_replay_opens / max(1, n_live_sessions),
                "replay_total_hold_s": replay_total_hold_s,
                "live_total_session_s": live_total_s,
                "hold_delta_s": abs(replay_total_hold_s - live_total_s),
                "replay_mean_hold_s": replay_mean_hold_s,
                "live_mean_session_s": live_mean_s,
                "hold_ratio": (replay_mean_hold_s / live_mean_s) if live_mean_s > 0 else 0.0,
                "replay_turnover": replay_turnover,
                "live_turnover": live_turnover,
                "turnover_delta": abs(replay_turnover - live_turnover),
            }
        )
    rows.sort(key=lambda r: -r["abs_residual"])
    for i, r in enumerate(rows):
        r["abs_residual_rank"] = i + 1

    abs_res = [r["abs_residual"] for r in rows]
    out: dict = {"window_days": window_days, "shared_n": len(rows), "rows": rows}
    if len(rows) >= 2:
        for key in (
            "open_count_delta",
            "open_count_ratio",
            "hold_delta_s",
            "hold_ratio",
            "turnover_delta",
        ):
            xs = [r[key] for r in rows]
            out[f"pearson_{key}"] = pearson(xs, abs_res)
            out[f"spearman_{key}"] = spearman(xs, abs_res)
    out["top10"] = rows[:10]
    return out


def fmt(x):
    return f"{x:+.4f}" if x is not None else " N/A"


def report(r: dict):
    w = r["window_days"]
    print(f"\n=== window {w}d (shared n={r['shared_n']}) ===")
    print(f"  correlations of |residual| vs ...")
    for key in (
        "open_count_delta",
        "open_count_ratio",
        "hold_delta_s",
        "hold_ratio",
        "turnover_delta",
    ):
        p = r.get(f"pearson_{key}")
        s = r.get(f"spearman_{key}")
        print(f"    {key:22s}  Pearson={fmt(p)}  Spearman={fmt(s)}")
    print(f"\n  top-10 by |residual|:")
    print(
        f"    {'#':>2s}  {'sym':<14s}  {'|res|':>7s}  "
        f"{'rOpens':>6s}  {'lSess':>5s}  {'ratio':>6s}  "
        f"{'rHold s':>9s}  {'lHold s':>9s}  {'rTurn$':>9s}  {'lTurn$':>9s}"
    )
    for r2 in r["top10"]:
        print(
            f"    {r2['abs_residual_rank']:>2d}  {r2['sym']:<14s}  "
            f"${r2['abs_residual']:>6.0f}  "
            f"{r2['n_replay_opens']:>6d}  {r2['n_live_sessions']:>5d}  "
            f"{r2['open_count_ratio']:>5.1f}x  "
            f"{r2['replay_mean_hold_s']:>9.0f}  {r2['live_mean_session_s']:>9.0f}  "
            f"${r2['replay_turnover']:>+8.0f}  ${r2['live_turnover']:>+8.0f}"
        )

    # Top-10 medians
    if r["top10"]:
        ratios = sorted(x["open_count_ratio"] for x in r["top10"])
        med_ratio = ratios[len(ratios) // 2]
        live_means = [x["live_mean_session_s"] for x in r["top10"] if x["live_mean_session_s"] > 0]
        replay_means = [x["replay_mean_hold_s"] for x in r["top10"] if x["replay_mean_hold_s"] > 0]
        if replay_means and live_means:
            mean_ratio = (sum(live_means) / len(live_means)) / (sum(replay_means) / len(replay_means))
        else:
            mean_ratio = None
        print(f"\n  top-10 median open_count_ratio: {med_ratio:.2f}x")
        if mean_ratio is not None:
            print(f"  top-10 mean(live_session_s) / mean(replay_hold_s): {mean_ratio:.2f}x")


def verdict(results):
    print("\n=== verdict per spec ===")
    for r in results:
        w = r["window_days"]
        sp_ratio = r.get("spearman_open_count_ratio")
        sp_delta = r.get("spearman_open_count_delta")
        top = r["top10"]
        if not top:
            print(f"  {w}d: no shared symbols")
            continue
        ratios = sorted(x["open_count_ratio"] for x in top)
        med_ratio = ratios[len(ratios) // 2]
        live_means = [x["live_mean_session_s"] for x in top if x["live_mean_session_s"] > 0]
        replay_means = [x["replay_mean_hold_s"] for x in top if x["replay_mean_hold_s"] > 0]
        hold_ratio_top = None
        if replay_means and live_means:
            hold_ratio_top = (sum(live_means) / len(live_means)) / (sum(replay_means) / len(replay_means))
        flags = []
        # Falsifier check
        if (
            sp_ratio is not None and sp_delta is not None
            and abs(sp_ratio) < 0.35 and abs(sp_delta) < 0.35
            and med_ratio < 3.0
        ):
            flags.append("FALSIFIED — both Spearman <0.35 and top median ratio <3×")
        # Confirmed checks
        confirmed = False
        if sp_ratio is not None and sp_ratio >= 0.50:
            flags.append(f"Spearman(|res|, open_count_ratio)={sp_ratio:+.3f} ≥+0.50 (confirmed)")
            confirmed = True
        if med_ratio >= 5.0:
            flags.append(f"top-10 median open_count_ratio={med_ratio:.1f}x ≥5× (confirmed)")
            confirmed = True
        if hold_ratio_top is not None and hold_ratio_top >= 5.0:
            flags.append(
                f"top-10 mean(live_session_s)/mean(replay_hold_s)={hold_ratio_top:.1f}x ≥5× (confirmed)"
            )
            confirmed = True
        if not flags:
            flags.append("marginal — no decisive signal")
        print(f"  {w}d:")
        for f in flags:
            print(f"    - {f}")
        print(f"    → {'PROCEED' if confirmed else 'DEPRIORITIZE'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--windows", default="14,7")
    ap.add_argument("--out", default=str(ROOT / "autoresearch_gated" / "cardinality_mismatch.json"))
    args = ap.parse_args()

    windows = [int(w) for w in args.windows.split(",") if w]
    results = [run_window(w) for w in windows]
    for r in results:
        report(r)
    verdict(results)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2, default=str))
    print(f"\n# wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
