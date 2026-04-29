#!/usr/bin/env python3
"""TIME_STOP_S sweep — replay's X3 hard-cap hypothesis test.

Sweep configs:
    baseline_3600        TIME_STOP_S=3600 (current default)
    7200s                TIME_STOP_S=7200  (2h)
    14400s               TIME_STOP_S=14400 (4h)
    28800s               TIME_STOP_S=28800 (8h, ~trading day)
    86400s               TIME_STOP_S=86400 (24h)
    disabled             DISABLE_TIME_STOP=1

Per config × {14d, 7d}: ρ vs HL closedPnl, replay $, trade count,
mean hold, time_stop exit count, focus residuals (AAVE/ZEC/xyz:MSTR),
top-10 abs |residual|.

Run from BASELINE — no bucketed cooldown active (it was demoted as
window-fragile on 7d).

Acceptance:
    14d Δρ ≥ +0.04
    AND 7d Δρ ≥ −0.02
    AND top-10 abs residual improves
    AND trade count does not collapse via deletion of live-positive trades
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.validate_replay_fit import parse_hl_closed_pnl, pearson  # noqa: E402

CONFIGS = [
    ("baseline_3600", {}),
    ("ts_7200", {"TIME_STOP_S": "7200"}),
    ("ts_14400", {"TIME_STOP_S": "14400"}),
    ("ts_28800", {"TIME_STOP_S": "28800"}),
    ("ts_86400", {"TIME_STOP_S": "86400"}),
    ("disabled", {"DISABLE_TIME_STOP": "1"}),
]

FOCUS_SYMS = ["AAVE", "ZEC", "xyz:MSTR"]


def _norm(s: str) -> str:
    return (s or "").replace("/USD", "").replace("/USDC", "")


def run_replay(env_overrides: dict, from_ms: int, to_ms: int):
    env = os.environ.copy()
    env.update(env_overrides)
    env["REPLAY_FROM_MS"] = str(from_ms)
    env["REPLAY_TO_MS"] = str(to_ms)
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
    trades = []
    with open(trades_path) as fh:
        for line in fh:
            try:
                trades.append(json.loads(line))
            except Exception:
                pass
    Path(pnl_path).unlink(missing_ok=True)
    Path(trades_path).unlink(missing_ok=True)
    return pnl, trades


def measure(name: str, env_overrides: dict, window_days: int):
    to_ms = int(dt.datetime.now(tz=dt.timezone.utc).timestamp() * 1000)
    from_ms = to_ms - window_days * 86_400_000
    sim_pnl, trades = run_replay(env_overrides, from_ms, to_ms)
    hl_pnl, _per_day, _hl_fees = parse_hl_closed_pnl(from_ms, to_ms)
    shared = sorted(set(sim_pnl) & set(hl_pnl))
    rho = pearson([sim_pnl[s] for s in shared], [hl_pnl[s] for s in shared])

    # per-trade aggregates
    n_trades = len(trades)
    holds_s = [(t["exit_ts"] - t["entry_ts"]) / 1000.0 for t in trades]
    mean_hold_s = sum(holds_s) / n_trades if n_trades > 0 else 0.0
    time_stop_count = sum(1 for t in trades if t.get("reason") == "time_stop")

    # residual rows
    rows = []
    for s in shared:
        rows.append(
            {
                "sym": s,
                "hl": hl_pnl[s],
                "sim": sim_pnl[s],
                "residual": hl_pnl[s] - sim_pnl[s],
                "abs_residual": abs(hl_pnl[s] - sim_pnl[s]),
            }
        )
    rows.sort(key=lambda r: -r["abs_residual"])
    top10_abs_sum = sum(r["abs_residual"] for r in rows[:10])
    focus = {}
    for s in FOCUS_SYMS:
        r = next((x for x in rows if x["sym"] == s), None)
        if r:
            focus[s] = r["residual"]
        else:
            focus[s] = None

    return {
        "name": name,
        "window_days": window_days,
        "rho": rho,
        "replay_total": sum(sim_pnl.values()),
        "live_total": sum(hl_pnl.values()),
        "n_trades": n_trades,
        "mean_hold_s": mean_hold_s,
        "time_stop_count": time_stop_count,
        "top10_abs_sum": top10_abs_sum,
        "focus": focus,
    }


def main():
    results = []
    for name, env in CONFIGS:
        for w in (14, 7):
            print(f"# {name} @ {w}d ...", file=sys.stderr)
            results.append(measure(name, env, w))

    # baseline references for delta calc
    base = {
        w: next(
            r for r in results if r["name"] == "baseline_3600" and r["window_days"] == w
        )
        for w in (14, 7)
    }

    print()
    hdr = (
        f"  {'config':16s}  {'win':>3s}  {'ρ':>8s}  {'Δρ':>8s}  "
        f"{'replay$':>9s}  {'trades':>6s}  {'mean h':>7s}  "
        f"{'time_stop':>9s}  {'top10|res|':>10s}  "
        f"{'AAVE_res':>9s}  {'ZEC_res':>9s}  {'MSTR_res':>9s}"
    )
    print(hdr)
    print("  " + "-" * 130)
    for r in results:
        b = base[r["window_days"]]
        d_rho = (
            (r["rho"] - b["rho"])
            if (r["rho"] is not None and b["rho"] is not None)
            else None
        )
        d_s = f"{d_rho:+.4f}" if d_rho is not None else "  N/A"
        mean_hold_h = r["mean_hold_s"] / 3600
        focus = r["focus"]
        aave = focus.get("AAVE")
        zec = focus.get("ZEC")
        mstr = focus.get("xyz:MSTR")
        print(
            f"  {r['name']:16s}  {r['window_days']:>3d}  {r['rho']:+.4f}  {d_s}  "
            f"${r['replay_total']:>+8.2f}  {r['n_trades']:>6d}  {mean_hold_h:>5.2f}h  "
            f"{r['time_stop_count']:>9d}  ${r['top10_abs_sum']:>9.2f}  "
            f"{('$' + format(aave, '+.2f')) if aave is not None else '   N/A':>9s}  "
            f"{('$' + format(zec, '+.2f')) if zec is not None else '   N/A':>9s}  "
            f"{('$' + format(mstr, '+.2f')) if mstr is not None else '   N/A':>9s}"
        )

    # Acceptance verdicts (14d window per spec)
    print()
    print("=== acceptance verdicts (14d-anchored, 7d sanity) ===")
    for name, _ in CONFIGS:
        if name == "baseline_3600":
            continue
        r14 = next(r for r in results if r["name"] == name and r["window_days"] == 14)
        r7 = next(r for r in results if r["name"] == name and r["window_days"] == 7)
        b14 = base[14]
        b7 = base[7]
        d14 = r14["rho"] - b14["rho"]
        d7 = r7["rho"] - b7["rho"]
        top10_delta = r14["top10_abs_sum"] - b14["top10_abs_sum"]
        # trade count: should not collapse below 30% of baseline
        trade_floor = b14["n_trades"] * 0.30
        rules = [
            ("14d Δρ ≥ +0.04", d14 >= 0.04, f"{d14:+.4f}"),
            ("7d ρ doesn't drop >0.02", d7 >= -0.02, f"{d7:+.4f}"),
            ("top-10 abs residual improves", top10_delta < 0, f"{top10_delta:+.2f}"),
            (
                f"trade count not collapsed (≥{trade_floor:.0f})",
                r14["n_trades"] >= trade_floor,
                f"{r14['n_trades']} vs floor {trade_floor:.0f}",
            ),
        ]
        verdict = "ACCEPT" if all(ok for _, ok, _ in rules) else "REJECT"
        print(f"\n  -- {name} --")
        for label, ok, val in rules:
            print(f"    [{'PASS' if ok else 'FAIL'}]  {label:48s}  {val}")
        print(f"    → {verdict}")

    out = ROOT / "autoresearch_gated" / "time_stop_sweep_matrix.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2, default=str))
    print(f"\n# wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
