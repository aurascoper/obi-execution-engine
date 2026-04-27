#!/usr/bin/env python3
"""Auto-topup VWA oracle — single-cause diagnostic.

Question: if replay scaled per-symbol PnL by the live exposure multiplier
created by auto_topup adds, how much ρ could improve?

Method (structural; no replay-side changes):
  1. Parse /tmp/auto_topup.log for FIRE events:
        2026-04-25T04:11:02Z FIRE ZEC long UNIFIED margin=$10 lev=2x ...
     → per-symbol: n_fires, topup_notional_sum (margin × lev × n_fires).
  2. Mark a symbol as "topup-active" iff n_fires >= 1.
  3. Exposure multiplier per topup-active symbol:
        m = (initial_replay_notional + topup_notional_sum)
            / initial_replay_notional
     where initial_replay_notional = NOTIONAL_PER_TRADE * n_replay_opens.
  4. Capped variants: m_cap = min(m, cap) for cap in {1.25, 1.5, 2.0, ∞}.
  5. Oracle PnL: replay_pnl × m_cap on topup-active symbols only.
     Other symbols unchanged.
  6. Recompute Pearson ρ vs HL closedPnl on shared set.

Falsifier (per the spec):
  - topup-active syms explain <10% of abs residual              → deprioritize
  - capped multiplier oracle Δρ <+0.03                          → deprioritize
  - topup_notional_sum is small vs replay notional on top syms  → deprioritize

Proceed to implementation if:
  - topup-active syms ≥ 15% of abs residual
  AND capped multiplier 14d Δρ ≥ +0.04
  AND ZEC residual direction improves under multiplier logic

Both 14d and 7d windows reported.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOPUP_LOG = Path("/tmp/auto_topup.log")

sys.path.insert(0, str(ROOT))
from scripts.validate_replay_fit import parse_hl_closed_pnl, pearson  # noqa: E402

# Same NOTIONAL_PER_TRADE as the replay
NOTIONAL_PER_TRADE = 750.0

CAPS = (1.25, 1.50, 2.00, float("inf"))


def _norm(s: str) -> str:
    return (s or "").replace("/USD", "").replace("/USDC", "")


# ── Parsing ───────────────────────────────────────────────────────────────
FIRE_RE = re.compile(
    r"^(?P<ts>\S+)\s+FIRE\s+(?P<sym>[A-Za-z0-9:_/]+)\s+(?P<side>long|short)\s+\S+"
    r"\s+margin=\$(?P<margin>\d+(?:\.\d+)?)\s+lev=(?P<lev>\d+(?:\.\d+)?)x"
)


def parse_topup_events(from_ms: int, to_ms: int):
    """Walk /tmp/auto_topup.log and return per-symbol:
    n_fires, topup_notional_sum (USD), first_ts, last_ts.
    """
    n_fires = defaultdict(int)
    topup_notional = defaultdict(float)
    first_ts: dict[str, int] = {}
    last_ts: dict[str, int] = {}
    if not TOPUP_LOG.exists():
        return dict(n_fires), dict(topup_notional), first_ts, last_ts
    with TOPUP_LOG.open() as fh:
        for line in fh:
            m = FIRE_RE.match(line)
            if not m:
                continue
            try:
                ts_ms = int(
                    dt.datetime.fromisoformat(
                        m.group("ts").replace("Z", "+00:00")
                    ).timestamp()
                    * 1000
                )
            except Exception:
                continue
            if ts_ms < from_ms or ts_ms >= to_ms:
                continue
            sym = _norm(m.group("sym"))
            try:
                margin = float(m.group("margin"))
                lev = float(m.group("lev"))
            except (TypeError, ValueError):
                continue
            n_fires[sym] += 1
            topup_notional[sym] += margin * lev
            if sym not in first_ts:
                first_ts[sym] = ts_ms
            last_ts[sym] = ts_ms
    return dict(n_fires), dict(topup_notional), first_ts, last_ts


# Approximate initial_replay_notional per symbol:
#   = NOTIONAL_PER_TRADE * n_opens_in_replay
# We get n_opens via REPLAY_OPENS_OUT from the replay run.
def replay_per_sym_with_opens(from_ms: int, to_ms: int):
    env = os.environ.copy()
    env["REPLAY_FROM_MS"] = str(from_ms)
    env["REPLAY_TO_MS"] = str(to_ms)
    env["RATCHET_EXIT_MODEL"] = "full"  # baseline
    with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False) as tmp_pnl:
        env["REPLAY_PERSYM_OUT"] = tmp_pnl.name
        pnl_path = tmp_pnl.name
    with tempfile.NamedTemporaryFile("w+", suffix=".jsonl", delete=False) as tmp_op:
        env["REPLAY_OPENS_OUT"] = tmp_op.name
        opens_path = tmp_op.name
    cmd = [sys.executable, str(ROOT / "scripts" / "z_entry_replay_gated.py")]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, cwd=ROOT)
    if r.returncode != 0:
        print(r.stderr[-1000:], file=sys.stderr)
        raise SystemExit(f"replay failed rc={r.returncode}")
    pnl = json.loads(Path(pnl_path).read_text())
    n_opens = defaultdict(int)
    with open(opens_path) as fh:
        for line in fh:
            try:
                o = json.loads(line)
            except Exception:
                continue
            sym = _norm(o.get("symbol", ""))
            if sym:
                n_opens[sym] += 1
    Path(pnl_path).unlink(missing_ok=True)
    Path(opens_path).unlink(missing_ok=True)
    return pnl, dict(n_opens)


def run_window(window_days: int):
    to_ms = int(dt.datetime.now(tz=dt.timezone.utc).timestamp() * 1000)
    from_ms = to_ms - window_days * 86_400_000

    print(f"\n## window {window_days}d  [{from_ms}..{to_ms})", file=sys.stderr)

    print("# pulling HL closedPnl ...", file=sys.stderr)
    hl_pnl, _per_day, _hl_fees = parse_hl_closed_pnl(from_ms, to_ms)

    print("# parsing auto_topup events ...", file=sys.stderr)
    n_fires, topup_notional, first_ts, last_ts = parse_topup_events(from_ms, to_ms)

    print("# running baseline replay (RATCHET_EXIT_MODEL=full) ...", file=sys.stderr)
    sim_pnl, n_opens = replay_per_sym_with_opens(from_ms, to_ms)

    shared = sorted(set(sim_pnl) & set(hl_pnl))
    base_rho = pearson([sim_pnl[s] for s in shared], [hl_pnl[s] for s in shared])

    rows = []
    abs_residual_total = 0.0
    abs_residual_topup = 0.0
    for s in shared:
        residual = hl_pnl[s] - sim_pnl[s]
        topup_active = n_fires.get(s, 0) >= 1
        notional_initial = NOTIONAL_PER_TRADE * max(n_opens.get(s, 1), 1)
        notional_topup = topup_notional.get(s, 0.0)
        multiplier = (
            (notional_initial + notional_topup) / notional_initial
            if topup_active
            else 1.0
        )
        rows.append(
            {
                "sym": s,
                "hl": hl_pnl[s],
                "sim": sim_pnl[s],
                "residual": residual,
                "abs_residual": abs(residual),
                "topup_active": topup_active,
                "n_fires": n_fires.get(s, 0),
                "topup_notional_sum": notional_topup,
                "n_replay_opens": n_opens.get(s, 0),
                "initial_replay_notional": notional_initial,
                "topup_to_replay_ratio": (notional_topup / notional_initial)
                if notional_initial > 0
                else 0.0,
                "multiplier_uncapped": multiplier,
                "first_topup": first_ts.get(s),
                "last_topup": last_ts.get(s),
            }
        )
        abs_residual_total += abs(residual)
        if topup_active:
            abs_residual_topup += abs(residual)

    # Oracle ρ at each cap
    oracle_rho = {}
    oracle_replay_total = {}
    zec_oracle = {}
    for cap in CAPS:
        adjusted = {}
        for r in rows:
            s = r["sym"]
            base = sim_pnl[s]
            if r["topup_active"]:
                m_cap = min(r["multiplier_uncapped"], cap)
                adjusted[s] = base * m_cap
            else:
                adjusted[s] = base
        rho = pearson([adjusted[s] for s in shared], [hl_pnl[s] for s in shared])
        oracle_rho[cap] = rho
        oracle_replay_total[cap] = sum(adjusted.values())
        if "ZEC" in adjusted:
            zec_oracle[cap] = adjusted["ZEC"]

    return {
        "window_days": window_days,
        "from_ms": from_ms,
        "to_ms": to_ms,
        "shared_n": len(shared),
        "n_topup_active": sum(1 for r in rows if r["topup_active"]),
        "abs_residual_total": abs_residual_total,
        "abs_residual_topup": abs_residual_topup,
        "topup_residual_share": (
            abs_residual_topup / abs_residual_total if abs_residual_total > 0 else 0.0
        ),
        "base_rho": base_rho,
        "oracle_rho": oracle_rho,
        "oracle_replay_total": oracle_replay_total,
        "live_total": sum(hl_pnl.values()),
        "zec_hl": hl_pnl.get("ZEC"),
        "zec_sim_base": sim_pnl.get("ZEC"),
        "zec_oracle_by_cap": zec_oracle,
        "rows": rows,
    }


def fmt(rho):
    return f"{rho:+.4f}" if rho is not None else "  N/A "


def report(r: dict):
    w = r["window_days"]
    print(f"\n=== window {w}d ===")
    print(
        f"  shared symbols: {r['shared_n']}  "
        f"topup-active: {r['n_topup_active']}  "
        f"residual share (topup/total): "
        f"{r['abs_residual_topup'] / max(r['abs_residual_total'], 1):.1%} "
        f"(${r['abs_residual_topup']:.0f} / ${r['abs_residual_total']:.0f})"
    )
    base = r["base_rho"]
    print(f"  baseline ρ: {fmt(base)}")
    print("  oracle ρ at exposure-multiplier caps:")
    for cap in CAPS:
        rho = r["oracle_rho"][cap]
        d = (rho - base) if (rho is not None and base is not None) else None
        d_s = f"Δ{d:+.4f}" if d is not None else "Δ  N/A"
        cap_s = "uncapped" if cap == float("inf") else f"≤{cap:.2f}"
        print(
            f"    cap={cap_s:>9s}  ρ={fmt(rho)}  {d_s}  "
            f"oracle_replay=${r['oracle_replay_total'][cap]:+9.2f}  "
            f"live=${r['live_total']:+.2f}"
        )
    if r["zec_hl"] is not None:
        print(f"\n  ZEC: hl=${r['zec_hl']:+.2f}  sim_base=${r['zec_sim_base']:+.2f}")
        for cap in CAPS:
            if cap in r["zec_oracle_by_cap"]:
                cap_s = "uncapped" if cap == float("inf") else f"≤{cap:.2f}"
                v = r["zec_oracle_by_cap"][cap]
                resid = (r["zec_hl"] - v) if r["zec_hl"] is not None else None
                base_resid = (
                    r["zec_hl"] - r["zec_sim_base"] if r["zec_hl"] is not None else 0
                )
                imp = (
                    1.0 - abs(resid) / abs(base_resid)
                    if base_resid not in (0, None) and resid is not None
                    else 0.0
                )
                print(
                    f"    cap={cap_s:>9s}  zec_oracle=${v:+.2f}  "
                    f"residual=${resid:+.2f}  "
                    f"|residual| improves by {imp:.1%}"
                )

    # Top topup-active rows by abs residual
    topup_rows = [r2 for r2 in r["rows"] if r2["topup_active"]]
    topup_rows.sort(key=lambda x: -x["abs_residual"])
    print("\n  topup-active symbols ranked by |residual|:")
    print(
        f"    {'sym':<14s}  {'hl$':>9s}  {'sim$':>9s}  {'residual':>9s}  "
        f"{'fires':>5s}  {'top$':>7s}  {'replay$':>8s}  {'mult':>6s}"
    )
    for x in topup_rows[:15]:
        print(
            f"    {x['sym']:<14s}  ${x['hl']:>+8.2f}  ${x['sim']:>+8.2f}  "
            f"${x['residual']:>+8.2f}  {x['n_fires']:>5d}  "
            f"${x['topup_notional_sum']:>6.0f}  ${x['initial_replay_notional']:>7.0f}  "
            f"{x['multiplier_uncapped']:>5.2f}x"
        )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--windows", default="14,7")
    ap.add_argument(
        "--out", default=str(ROOT / "autoresearch_gated" / "topup_vwap_oracle.json")
    )
    args = ap.parse_args()

    windows = [int(w) for w in args.windows.split(",") if w]
    results = [run_window(w) for w in windows]
    for r in results:
        report(r)

    print("\n=== verdict per spec ===")
    for r in results:
        w = r["window_days"]
        base = r["base_rho"]
        share = r["topup_residual_share"]
        # Use cap=2.00 as the "realistic structural" reference
        rho_2 = r["oracle_rho"][2.00]
        d_2 = (rho_2 - base) if (rho_2 is not None and base is not None) else None
        zec_resid = (
            (r["zec_hl"] - r["zec_oracle_by_cap"][2.00])
            if (r["zec_hl"] is not None and 2.00 in r["zec_oracle_by_cap"])
            else None
        )
        zec_base_resid = (
            r["zec_hl"] - r["zec_sim_base"]
            if (r["zec_hl"] is not None and r["zec_sim_base"] is not None)
            else None
        )
        zec_imp = (
            1.0 - abs(zec_resid) / abs(zec_base_resid)
            if zec_resid is not None and zec_base_resid not in (0, None)
            else None
        )

        flags = []
        if share < 0.10:
            flags.append(f"residual share {share:.1%} < 10% (deprioritize)")
        elif share >= 0.15:
            flags.append(f"residual share {share:.1%} ≥ 15% (proceed-side)")
        else:
            flags.append(f"residual share {share:.1%} marginal")

        if d_2 is None:
            flags.append("Δρ@cap2 N/A")
        elif d_2 < 0.03:
            flags.append(f"Δρ@cap2 {d_2:+.4f} <+0.03 (deprioritize)")
        elif d_2 >= 0.04:
            flags.append(f"Δρ@cap2 {d_2:+.4f} ≥+0.04 (proceed-side)")
        else:
            flags.append(f"Δρ@cap2 {d_2:+.4f} marginal")

        if zec_imp is not None:
            flags.append(f"ZEC |residual| improves {zec_imp:.1%}")

        print(f"  {w}d: " + "; ".join(flags))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    serial = []
    for r in results:
        rr = dict(r)
        rr["oracle_rho"] = {str(k): v for k, v in r["oracle_rho"].items()}
        rr["oracle_replay_total"] = {
            str(k): v for k, v in r["oracle_replay_total"].items()
        }
        rr["zec_oracle_by_cap"] = {str(k): v for k, v in r["zec_oracle_by_cap"].items()}
        serial.append(rr)
    out.write_text(json.dumps(serial, indent=2, default=str))
    print(f"\n# wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
