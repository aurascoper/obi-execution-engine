#!/usr/bin/env python3
"""Bucketed re-entry cooldown matrix.

Configs:
  baseline                          (no cooldown)
  global_3600                       (single knob, prior winner with AAVE harm)
  bucketed_1800                     (HIP-3 + ZEC: 1800s, longs: 0)
  bucketed_3600                     (HIP-3 + ZEC: 3600s, longs: 0)
  bucketed_7200                     (HIP-3 + ZEC: 7200s, longs: 0)

Acceptance per spec:
  14d Δρ ≥ +0.04
  AND 7d ρ does not worsen by >0.02
  AND AAVE residual worsens by < $25
  AND top-10 abs residual improves by ≥ $50
  AND xyz equity bucket abs residual improves
  AND deleted counterfactual PnL on long-hold natives is not strongly positive
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.validate_replay_fit import parse_hl_closed_pnl, pearson  # noqa: E402

LONG_HOLD_NATIVES = {
    "AAVE",
    "ETH",
    "BTC",
    "SOL",
    "LDO",
    "CRV",
    "BNB",
    "SUI",
    "TAO",
    "DOGE",
    "LINK",
    "ADA",
    "AVAX",
    "LTC",
    "BCH",
    "DOT",
    "UNI",
    "POL",
    "RENDER",
    "FIL",
    "HYPE",
    "NEAR",
    "ENA",
    "PAXG",
    "ARB",
    "XRP",
}


def _norm(s: str) -> str:
    return (s or "").replace("/USD", "").replace("/USDC", "")


def is_xyz_equity(sym: str) -> bool:
    return sym.startswith("xyz:")


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
    trades_by_sym: dict[str, list[dict]] = defaultdict(list)
    n_trades = 0
    with open(trades_path) as fh:
        for line in fh:
            try:
                t = json.loads(line)
            except Exception:
                continue
            sym = _norm(t.get("symbol", ""))
            if sym:
                trades_by_sym[sym].append(t)
                n_trades += 1
    Path(pnl_path).unlink(missing_ok=True)
    Path(trades_path).unlink(missing_ok=True)
    return pnl, dict(trades_by_sym), n_trades


def measure(
    name: str, env_overrides: dict, window_days: int, baseline_trades=None
) -> dict:
    to_ms = int(dt.datetime.now(tz=dt.timezone.utc).timestamp() * 1000)
    from_ms = to_ms - window_days * 86_400_000
    sim_pnl, trades_by_sym, n_trades = run_replay(env_overrides, from_ms, to_ms)
    hl_pnl, _per_day, _hl_fees = parse_hl_closed_pnl(from_ms, to_ms)
    shared = sorted(set(sim_pnl) & set(hl_pnl))
    rho = pearson([sim_pnl[s] for s in shared], [hl_pnl[s] for s in shared])

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

    # Per-symbol replay PnL for delta computation
    sym_pnl = {s: sim_pnl.get(s, 0.0) for s in shared}

    # Long-hold-native deleted counterfactual: pnl removed on long-hold natives
    long_hold_deleted_pnl = 0.0
    if baseline_trades is not None:
        for sym in LONG_HOLD_NATIVES:
            base_t = baseline_trades.get(sym, [])
            cur_t = trades_by_sym.get(sym, [])
            base_keys = {(t["entry_ts"], t["side"]): t for t in base_t}
            cur_keys = {(t["entry_ts"], t["side"]) for t in cur_t}
            for k, t in base_keys.items():
                if k not in cur_keys:
                    long_hold_deleted_pnl += t["pnl"]

    return {
        "name": name,
        "window_days": window_days,
        "rho": rho,
        "replay_total": sum(sim_pnl.values()),
        "live_total": sum(hl_pnl.values()),
        "n_trades": n_trades,
        "shared_n": len(shared),
        "rows": rows,
        "trades_by_sym": trades_by_sym,
        "sym_pnl": sym_pnl,
        "long_hold_deleted_pnl": long_hold_deleted_pnl,
    }


CONFIGS = [
    ("baseline", {}),
    ("global_3600", {"MIN_REENTRY_COOLDOWN_S": "3600"}),
    (
        "bucketed_1800",
        {
            "REENTRY_COOLDOWN_BY_SYMBOL": "config/gates/reentry_cooldown_by_symbol_1800.json"
        },
    ),
    (
        "bucketed_3600",
        {"REENTRY_COOLDOWN_BY_SYMBOL": "config/gates/reentry_cooldown_by_symbol.json"},
    ),
    (
        "bucketed_7200",
        {
            "REENTRY_COOLDOWN_BY_SYMBOL": "config/gates/reentry_cooldown_by_symbol_7200.json"
        },
    ),
]


def main():
    results: dict[str, dict[int, dict]] = {}
    base_trades: dict[int, dict] = {}
    for name, env in CONFIGS:
        results[name] = {}
        for w in (14, 7):
            print(f"# {name} @ {w}d ...", file=sys.stderr)
            r = measure(name, env, w, base_trades.get(w))
            results[name][w] = r
            if name == "baseline":
                base_trades[w] = r["trades_by_sym"]

    # Print summary
    print()
    print(
        f"  {'config':22s}  {'win':>3s}  {'ρ':>8s}  {'Δρ':>8s}  "
        f"{'replay$':>9s}  {'trades':>6s}  "
        f"{'AAVE_res':>8s}  {'top10|res|':>10s}  {'xyz_eq|res|':>11s}  "
        f"{'longhold_del$':>13s}"
    )
    print("  " + "-" * 130)
    base_aave = {}
    base_top10_abs = {}
    base_xyz_abs = {}
    for w in (14, 7):
        b = results["baseline"][w]
        aave_row = next((r for r in b["rows"] if r["sym"] == "AAVE"), None)
        base_aave[w] = aave_row["residual"] if aave_row else 0.0
        base_top10_abs[w] = sum(r["abs_residual"] for r in b["rows"][:10])
        base_xyz_abs[w] = sum(
            r["abs_residual"] for r in b["rows"] if is_xyz_equity(r["sym"])
        )

    for name, _ in CONFIGS:
        for w in (14, 7):
            r = results[name][w]
            base_rho = results["baseline"][w]["rho"]
            d_rho = (
                (r["rho"] - base_rho)
                if (r["rho"] is not None and base_rho is not None)
                else None
            )
            d_s = f"{d_rho:+.4f}" if d_rho is not None else "  N/A"
            aave_row = next((x for x in r["rows"] if x["sym"] == "AAVE"), None)
            aave_res = aave_row["residual"] if aave_row else 0.0
            top10_abs = sum(x["abs_residual"] for x in r["rows"][:10])
            xyz_abs = sum(
                x["abs_residual"] for x in r["rows"] if is_xyz_equity(x["sym"])
            )
            print(
                f"  {name:22s}  {w:>3d}  {r['rho']:+.4f}  {d_s}  "
                f"${r['replay_total']:>+8.2f}  {r['n_trades']:>6d}  "
                f"${aave_res:>+7.2f}  ${top10_abs:>9.2f}  ${xyz_abs:>10.2f}  "
                f"${r['long_hold_deleted_pnl']:>+12.2f}"
            )

    # Acceptance verdicts
    print()
    print("=== acceptance per spec (14d window) ===")
    for name, _ in CONFIGS:
        if name == "baseline":
            continue
        r14 = results[name][14]
        r7 = results[name][7]
        b14 = results["baseline"][14]
        b7 = results["baseline"][7]
        d14 = r14["rho"] - b14["rho"]
        d7 = r7["rho"] - b7["rho"]
        aave_row = next((x for x in r14["rows"] if x["sym"] == "AAVE"), None)
        aave_res_now = aave_row["residual"] if aave_row else 0.0
        aave_delta_abs = abs(aave_res_now) - abs(base_aave[14])
        top10_abs_now = sum(x["abs_residual"] for x in r14["rows"][:10])
        top10_delta = top10_abs_now - base_top10_abs[14]
        xyz_abs_now = sum(
            x["abs_residual"] for x in r14["rows"] if is_xyz_equity(x["sym"])
        )
        xyz_delta = xyz_abs_now - base_xyz_abs[14]
        long_hold_del = r14["long_hold_deleted_pnl"]

        rules = [
            ("14d Δρ ≥ +0.04", d14 >= 0.04, f"{d14:+.4f}"),
            ("7d ρ doesn't drop >0.02", d7 >= -0.02, f"{d7:+.4f}"),
            ("AAVE Δ|residual| < $25", aave_delta_abs < 25.0, f"{aave_delta_abs:+.2f}"),
            (
                "top-10 abs residual improves ≥ $50",
                top10_delta <= -50.0,
                f"{top10_delta:+.2f}",
            ),
            (
                "xyz equity bucket residual improves",
                xyz_delta < 0.0,
                f"{xyz_delta:+.2f}",
            ),
            (
                "longhold deleted counterfactual PnL not strongly positive (≤ +$10)",
                long_hold_del <= 10.0,
                f"{long_hold_del:+.2f}",
            ),
        ]
        verdict = "ACCEPT" if all(ok for _, ok, _ in rules) else "REJECT"
        print(f"\n  -- {name} --")
        for label, ok, val in rules:
            print(f"    [{'PASS' if ok else 'FAIL'}]  {label:60s}  {val}")
        print(f"    → {verdict}")

    out = ROOT / "autoresearch_gated" / "bucketed_cooldown_matrix.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    serial = {}
    for name, by_w in results.items():
        serial[name] = {}
        for w, r in by_w.items():
            rr = {k: v for k, v in r.items() if k not in ("trades_by_sym",)}
            serial[name][str(w)] = rr
    out.write_text(json.dumps(serial, indent=2, default=str))
    print(f"\n# wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
