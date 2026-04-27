#!/usr/bin/env python3
"""Per-symbol delta between baseline and cooldown_3600s replay.

Goal: explain why AAVE residual got $132 worse under cooldown_3600s
even though portfolio ρ improved by +0.049.

Method:
  1. Run baseline (RATCHET_EXIT_MODEL=full, MIN_REENTRY_COOLDOWN_S=0).
  2. Run cooldown_3600 (MIN_REENTRY_COOLDOWN_S=3600).
  3. Both with REPLAY_TRADES_OUT to get per-trade records.
  4. Match trades by (symbol, entry_ts). Trades only in baseline = deleted.
  5. Per focus / top-10 symbol: deltas, deleted_pnl_sum, kept_pnl_sum.
  6. AAVE: dump every deleted trade with hold_s, side, reason, pnl.

Decision rules:
  Accept cooldown_3600s as flagged candidate IF:
    AAVE regression caused by 1-2 deleted outlier trades
    AND top-10 aggregate residual still improves
    AND no other top live-PnL symbol worsens materially
  Reject IF:
    AAVE regression from systematically deleted profitable re-entries
    OR 3+ top syms worsen by >$50
    OR top-10 residual_abs_sum worsens
  Promote to symbol-bucketed cooldown IF:
    cooldown helps churn-symbols but hurts long-hold symbols
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
from scripts.validate_replay_fit import parse_hl_closed_pnl  # noqa: E402

FOCUS_SYMS = ["AAVE", "ZEC", "xyz:MSTR", "ETH", "xyz:INTC"]
WINDOW_DAYS = 14


def _norm(s: str) -> str:
    return (s or "").replace("/USD", "").replace("/USDC", "")


def run_replay(env_overrides: dict, from_ms: int, to_ms: int):
    env = os.environ.copy()
    env.update(env_overrides)
    env["REPLAY_FROM_MS"] = str(from_ms)
    env["REPLAY_TO_MS"] = str(to_ms)
    env["RATCHET_EXIT_MODEL"] = env.get("RATCHET_EXIT_MODEL", "full")
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
    # parse gate-counts JSON line
    gate_counts: dict[str, int] = {}
    for line in r.stdout.splitlines():
        if line.startswith("REPLAY_GATE_COUNTS_JSON"):
            try:
                gate_counts = json.loads(line.split(" ", 1)[1])["gate_counts"]
            except Exception:
                pass
            break
    Path(pnl_path).unlink(missing_ok=True)
    Path(trades_path).unlink(missing_ok=True)
    return pnl, dict(trades_by_sym), gate_counts


def trade_key(t: dict) -> tuple:
    return (t["entry_ts"], t["side"])


def main():
    to_ms = int(dt.datetime.now(tz=dt.timezone.utc).timestamp() * 1000)
    from_ms = to_ms - WINDOW_DAYS * 86_400_000

    print(f"# window {WINDOW_DAYS}d", file=sys.stderr)
    print("# pulling HL closedPnl ...", file=sys.stderr)
    hl_pnl, _per_day, _hl_fees = parse_hl_closed_pnl(from_ms, to_ms)

    print("# running baseline replay ...", file=sys.stderr)
    base_pnl, base_trades, base_gates = run_replay(
        {"MIN_REENTRY_COOLDOWN_S": "0"}, from_ms, to_ms
    )
    print("# running cooldown_3600s replay ...", file=sys.stderr)
    cd_pnl, cd_trades, cd_gates = run_replay(
        {"MIN_REENTRY_COOLDOWN_S": "3600"}, from_ms, to_ms
    )

    print(f"\n# gate_counts baseline:  {base_gates}")
    print(f"# gate_counts cooldown:  {cd_gates}")
    blocked = cd_gates.get("reentry_cooldown", 0)
    print(f"# cooldown blocked {blocked} entries window-wide")

    shared = sorted(set(base_pnl) & set(cd_pnl) & set(hl_pnl))
    rows = []
    for s in shared:
        bt = base_trades.get(s, [])
        ct = cd_trades.get(s, [])
        b_keys = {trade_key(t): t for t in bt}
        c_keys = {trade_key(t): t for t in ct}
        deleted = [b_keys[k] for k in b_keys if k not in c_keys]
        kept = [b_keys[k] for k in b_keys if k in c_keys]

        b_resid = hl_pnl[s] - base_pnl[s]
        c_resid = hl_pnl[s] - cd_pnl[s]
        delta_resid = c_resid - b_resid

        deleted_pnl_sum = sum(t["pnl"] for t in deleted)
        kept_pnl_sum = sum(t["pnl"] for t in kept)
        deleted_holds = [(t["exit_ts"] - t["entry_ts"]) / 1000.0 for t in deleted]
        deleted_mean_age_s = (
            sum(deleted_holds) / len(deleted_holds) if deleted_holds else 0.0
        )
        long_n = sum(1 for t in deleted if t["side"] > 0)
        short_n = sum(1 for t in deleted if t["side"] < 0)

        b_total_hold = sum((t["exit_ts"] - t["entry_ts"]) / 1000.0 for t in bt)
        c_total_hold = sum((t["exit_ts"] - t["entry_ts"]) / 1000.0 for t in ct)

        rows.append(
            {
                "sym": s,
                "hl_pnl": hl_pnl[s],
                "baseline_replay": base_pnl[s],
                "cooldown_replay": cd_pnl[s],
                "baseline_residual": b_resid,
                "cooldown_residual": c_resid,
                "delta_residual": delta_resid,
                "abs_baseline_residual": abs(b_resid),
                "baseline_n_opens": len(bt),
                "cooldown_n_opens": len(ct),
                "deleted_n": len(deleted),
                "deleted_pnl_sum": deleted_pnl_sum,
                "kept_pnl_sum": kept_pnl_sum,
                "deleted_mean_age_s": deleted_mean_age_s,
                "deleted_long_n": long_n,
                "deleted_short_n": short_n,
                "baseline_total_hold_s": b_total_hold,
                "cooldown_total_hold_s": c_total_hold,
                "baseline_mean_hold_s": (b_total_hold / len(bt)) if bt else 0.0,
                "cooldown_mean_hold_s": (c_total_hold / len(ct)) if ct else 0.0,
            }
        )
    rows.sort(key=lambda r: -r["abs_baseline_residual"])

    # Focus + top-10 union (deduplicated)
    top10_syms = [r["sym"] for r in rows[:10]]
    syms_to_show = list(dict.fromkeys(FOCUS_SYMS + top10_syms))

    print("\n=== per-symbol delta (focus + top-10) ===")
    print(
        f"  {'sym':<14s}  {'hl$':>9s}  {'b_rep':>9s}  {'c_rep':>9s}  "
        f"{'b_res':>8s}  {'c_res':>8s}  {'Δres':>8s}  "
        f"{'b_n':>4s} {'c_n':>4s} {'del_n':>5s}  "
        f"{'del_pnl':>8s}  {'kept_pnl':>8s}  {'del_age_h':>9s}"
    )
    for s in syms_to_show:
        r = next((x for x in rows if x["sym"] == s), None)
        if r is None:
            continue
        print(
            f"  {r['sym']:<14s}  ${r['hl_pnl']:>+8.2f}  ${r['baseline_replay']:>+8.2f}  "
            f"${r['cooldown_replay']:>+8.2f}  "
            f"${r['baseline_residual']:>+7.2f}  ${r['cooldown_residual']:>+7.2f}  "
            f"${r['delta_residual']:>+7.2f}  "
            f"{r['baseline_n_opens']:>4d} {r['cooldown_n_opens']:>4d} {r['deleted_n']:>5d}  "
            f"${r['deleted_pnl_sum']:>+7.2f}  ${r['kept_pnl_sum']:>+7.2f}  "
            f"{r['deleted_mean_age_s'] / 3600:>9.2f}"
        )

    # Aggregate top-10 residual
    top10_baseline_abs = sum(abs(r["baseline_residual"]) for r in rows[:10])
    top10_cooldown_abs = sum(abs(r["cooldown_residual"]) for r in rows[:10])
    print(
        f"\n  top-10 abs residual:  baseline ${top10_baseline_abs:.2f}  "
        f"cooldown ${top10_cooldown_abs:.2f}  "
        f"Δ ${top10_cooldown_abs - top10_baseline_abs:+.2f}"
    )

    # Count "worsened by >$50" symbols within top-15
    worsened = [
        r
        for r in rows[:15]
        if abs(r["cooldown_residual"]) - abs(r["baseline_residual"]) > 50.0
    ]
    print(f"\n  top-15 symbols worsened by >$50: {len(worsened)}")
    for r in worsened:
        print(
            f"    {r['sym']:<14s}  Δ|res|=${abs(r['cooldown_residual']) - abs(r['baseline_residual']):+.2f}"
        )

    # AAVE deep-dive
    aave = next((x for x in rows if x["sym"] == "AAVE"), None)
    if aave is not None and aave["deleted_n"] > 0:
        print(f"\n=== AAVE deleted trades (cooldown removed {aave['deleted_n']}) ===")
        bt = base_trades.get("AAVE", [])
        ct = cd_trades.get("AAVE", [])
        b_keys = {trade_key(t): t for t in bt}
        c_keys = {trade_key(t): t for t in ct}
        deleted = [b_keys[k] for k in b_keys if k not in c_keys]
        deleted.sort(key=lambda t: t["entry_ts"])
        print(
            f"  {'#':>3s}  {'entry_iso':<19s}  {'side':>4s}  "
            f"{'hold_h':>6s}  {'pnl':>7s}  {'reason':<14s}"
        )
        for i, t in enumerate(deleted, 1):
            iso = dt.datetime.fromtimestamp(
                t["entry_ts"] / 1000, tz=dt.timezone.utc
            ).strftime("%Y-%m-%d %H:%M:%S")
            hold_h = (t["exit_ts"] - t["entry_ts"]) / 3_600_000
            print(
                f"  {i:>3d}  {iso}  {t['side']:>+4d}  "
                f"{hold_h:>6.2f}  ${t['pnl']:>+6.2f}  {t['reason']:<14s}"
            )

    # Symbol-bucket analysis: where does cooldown help vs hurt?
    helped = sorted(
        [r for r in rows if r["delta_residual"] != 0],
        key=lambda r: r["delta_residual"],
    )  # sign-corrected: negative delta_residual = cooldown lowered (improved) residual toward zero?
    # Actually "delta_residual = cooldown - baseline"; if baseline residual is +X
    # and cooldown is closer to 0, |delta| < |baseline|. Use abs delta.
    sym_helped = sorted(
        rows,
        key=lambda r: abs(r["cooldown_residual"]) - abs(r["baseline_residual"]),
    )
    print("\n=== top symbols where cooldown helped (|residual| dropped) ===")
    print(
        f"  {'sym':<14s}  {'baseline|res|':>12s}  {'cooldown|res|':>13s}  {'Δ':>8s}  del_n"
    )
    for r in sym_helped[:10]:
        diff = abs(r["cooldown_residual"]) - abs(r["baseline_residual"])
        if diff >= 0:
            break
        print(
            f"  {r['sym']:<14s}  ${abs(r['baseline_residual']):>11.2f}  "
            f"${abs(r['cooldown_residual']):>12.2f}  ${diff:>+7.2f}  {r['deleted_n']}"
        )

    print("\n=== top symbols where cooldown hurt (|residual| rose) ===")
    print(
        f"  {'sym':<14s}  {'baseline|res|':>12s}  {'cooldown|res|':>13s}  {'Δ':>8s}  del_n"
    )
    sym_hurt = list(reversed(sym_helped))
    for r in sym_hurt[:10]:
        diff = abs(r["cooldown_residual"]) - abs(r["baseline_residual"])
        if diff <= 0:
            break
        print(
            f"  {r['sym']:<14s}  ${abs(r['baseline_residual']):>11.2f}  "
            f"${abs(r['cooldown_residual']):>12.2f}  ${diff:>+7.2f}  {r['deleted_n']}"
        )

    out = ROOT / "autoresearch_gated" / "cooldown_aave_delta.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "rows": rows,
                "top10_baseline_abs": top10_baseline_abs,
                "top10_cooldown_abs": top10_cooldown_abs,
            },
            indent=2,
            default=str,
        )
    )
    print(f"\n# wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
