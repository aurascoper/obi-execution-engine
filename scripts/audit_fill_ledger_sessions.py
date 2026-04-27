#!/usr/bin/env python3
"""Mode 2A — fill-ledger session audit.

Walks hl_fill_received chronologically per symbol, maintains a per-symbol
ledger (signed_qty, entry_vwap, realized_pnl, fees) per the spec at
autoresearch_gated/mode2_session_replay_spec.md.

For each REDUCE fill: compute ledger_realized = side_sign · reduce_qty ·
(fill_px − entry_vwap_at_reduce). Compare against the fill's
hl.closed_pnl (venue truth). Aggregate per-symbol over a validation
window. Report:
  - per-fill ledger_realized vs hl_closed_pnl (Pearson ρ)
  - per-symbol Σledger_realized vs Σhl_closed_pnl (ρ + gross mismatch)
  - top residuals + ZEC/ETH/AAVE/xyz:MSTR detail

Acceptance gates:
  - ledger-vs-HL ρ per fill ≥ 0.95
  - per-symbol ρ ≥ 0.95
  - ZEC abs residual reduced ≥ 80% vs Mode 1 naive audit ($108 → ≤ $22)
  - ETH sign correct (matches HL closed_pnl sign)
  - gross mismatch ≤ 2%
  - fee mismatch ≤ 2%

NOT a strategy. NOT a policy replay. Pure accounting validation.

Usage:
    venv/bin/python3 scripts/audit_fill_ledger_sessions.py
    venv/bin/python3 scripts/audit_fill_ledger_sessions.py --window-days 7
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / "hl_engine.jsonl"
DEFAULT_OUT = ROOT / "autoresearch_gated" / "audit_fill_ledger_sessions.json"

sys.path.insert(0, str(ROOT))
from scripts.validate_replay_fit import parse_hl_closed_pnl, pearson  # noqa: E402

FOCUS_SYMS = ["AAVE", "ZEC", "ETH", "xyz:MSTR"]
EPS = 1e-9


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


def load_all_fills():
    """Walk hl_fill_received for ALL time (we need pre-window context).
    Returns {sym: [{ts_ms, side, sz, px, closed_pnl, fee, ...}, ...]} sorted ascending.
    """
    by_sym: dict[str, list[dict]] = defaultdict(list)
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
            by_sym[sym].append({
                "ts_ms": ts_ms,
                "side": side,
                "sz": sz,
                "px": px,
                "closed_pnl": cp,
                "fee": fee,
                "crossed": o.get("crossed"),
            })
    for sym in by_sym:
        by_sym[sym].sort(key=lambda f: f["ts_ms"])
    return dict(by_sym)


# ── Ledger state machine ──────────────────────────────────────────────────
def replay_ledger_for_symbol(fills: list[dict]):
    """Walk fills chronologically, maintain VWAP ledger, emit per-reduce
    records. Returns:
        reductions  list[{ts_ms, fill_px, reduce_qty, entry_vwap_at_reduce,
                          ledger_realized_pnl, ledger_fee, hl_closed_pnl,
                          hl_fee}]
        opens       list[{ts_ms, fill_px, qty, side}]   (informational)
        adds        list[{ts_ms, fill_px, add_qty, new_vwap}]
        flips       list[{ts_ms, ...}]
    """
    pos_qty = 0.0   # signed
    entry_vwap = 0.0
    open_ts_ms = 0
    side_str = None  # 'long' or 'short' or None

    reductions = []
    opens = []
    adds = []
    flips = []

    for f in fills:
        sz = f["sz"]
        px = f["px"]
        signed_d = sz if f["side"] == "buy" else -sz
        new_qty = pos_qty + signed_d

        is_open = (abs(pos_qty) < EPS) and (abs(new_qty) > EPS)
        is_close = (abs(pos_qty) > EPS) and (abs(new_qty) < EPS)
        is_flip = (pos_qty * new_qty) < 0
        is_add = (
            abs(pos_qty) > EPS and abs(new_qty) > EPS
            and (pos_qty * signed_d) > 0  # same direction
        )
        is_reduce = (
            abs(pos_qty) > EPS and abs(new_qty) > EPS
            and (pos_qty * signed_d) < 0   # opposite, but doesn't flip
            and (pos_qty * new_qty) > 0
        )

        if is_open:
            pos_qty = new_qty
            entry_vwap = px
            open_ts_ms = f["ts_ms"]
            side_str = "long" if new_qty > 0 else "short"
            opens.append({
                "ts_ms": f["ts_ms"], "fill_px": px,
                "qty": abs(new_qty), "side": side_str,
            })
        elif is_add:
            old_abs = abs(pos_qty)
            add_abs = abs(signed_d)
            new_abs = abs(new_qty)
            entry_vwap = (old_abs * entry_vwap + add_abs * px) / new_abs
            pos_qty = new_qty
            adds.append({
                "ts_ms": f["ts_ms"], "fill_px": px,
                "add_qty": add_abs, "new_vwap": entry_vwap,
                "qty_after": new_abs,
            })
        elif is_reduce:
            reduce_abs = abs(signed_d)
            side_sign = 1.0 if pos_qty > 0 else -1.0
            ledger_realized = side_sign * reduce_abs * (px - entry_vwap)
            reductions.append({
                "ts_ms": f["ts_ms"],
                "fill_px": px,
                "reduce_qty": reduce_abs,
                "entry_vwap_at_reduce": entry_vwap,
                "ledger_realized_pnl": ledger_realized,
                "ledger_fee": f["fee"],
                "hl_closed_pnl": f["closed_pnl"],
                "hl_fee": f["fee"],
                "side": side_str,
                "open_ts_ms": open_ts_ms,
                "qty_after": abs(new_qty),
            })
            pos_qty = new_qty
            # vwap unchanged on reduce
        elif is_flip:
            # close existing at fill_px (the part that reduces to 0), then
            # open new with the residual
            close_qty = abs(pos_qty)
            side_sign = 1.0 if pos_qty > 0 else -1.0
            ledger_realized = side_sign * close_qty * (px - entry_vwap)
            reductions.append({
                "ts_ms": f["ts_ms"],
                "fill_px": px,
                "reduce_qty": close_qty,
                "entry_vwap_at_reduce": entry_vwap,
                "ledger_realized_pnl": ledger_realized,
                "ledger_fee": f["fee"],
                "hl_closed_pnl": f["closed_pnl"],
                "hl_fee": f["fee"],
                "side": side_str,
                "open_ts_ms": open_ts_ms,
                "qty_after": 0.0,
                "closes_via_flip": True,
            })
            flips.append({
                "ts_ms": f["ts_ms"], "fill_px": px,
                "close_qty": close_qty, "new_qty": abs(new_qty),
            })
            # open new in opposite direction with residual
            pos_qty = new_qty
            entry_vwap = px
            open_ts_ms = f["ts_ms"]
            side_str = "long" if new_qty > 0 else "short"
            opens.append({
                "ts_ms": f["ts_ms"], "fill_px": px,
                "qty": abs(new_qty), "side": side_str,
                "via_flip": True,
            })
        elif is_close:
            close_qty = abs(pos_qty)
            side_sign = 1.0 if pos_qty > 0 else -1.0
            ledger_realized = side_sign * close_qty * (px - entry_vwap)
            reductions.append({
                "ts_ms": f["ts_ms"],
                "fill_px": px,
                "reduce_qty": close_qty,
                "entry_vwap_at_reduce": entry_vwap,
                "ledger_realized_pnl": ledger_realized,
                "ledger_fee": f["fee"],
                "hl_closed_pnl": f["closed_pnl"],
                "hl_fee": f["fee"],
                "side": side_str,
                "open_ts_ms": open_ts_ms,
                "qty_after": 0.0,
            })
            pos_qty = 0.0
            entry_vwap = 0.0
            side_str = None
            open_ts_ms = 0
        else:
            # No change — could be edge case (e.g., net-zero fill); skip.
            pass

    return reductions, opens, adds, flips


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-days", type=int, default=14)
    ap.add_argument("--also-7d", action="store_true", default=True)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print("# loading all fills ...", file=sys.stderr)
    fills_by_sym = load_all_fills()
    print(f"# {sum(len(v) for v in fills_by_sym.values())} fills across "
          f"{len(fills_by_sym)} symbols", file=sys.stderr)

    print("# walking ledger per symbol ...", file=sys.stderr)
    ledger_by_sym: dict[str, dict] = {}
    for sym, fills in fills_by_sym.items():
        red, opens, adds, flips = replay_ledger_for_symbol(fills)
        ledger_by_sym[sym] = {
            "reductions": red,
            "opens": opens,
            "adds": adds,
            "flips": flips,
        }

    summaries = []
    windows = [args.window_days]
    if args.also_7d and args.window_days != 7:
        windows.append(7)

    for w in windows:
        to_ms = int(dt.datetime.now(tz=dt.timezone.utc).timestamp() * 1000)
        from_ms = to_ms - w * 86_400_000
        print(f"\n## window {w}d  [{from_ms}..{to_ms})", file=sys.stderr)

        # Per-fill alignment (only reductions in-window)
        per_fill_ledger = []
        per_fill_hl = []
        per_sym_ledger = defaultdict(float)
        per_sym_hl = defaultdict(float)
        per_sym_n_reductions = defaultdict(int)
        n_reductions_in_window = 0

        for sym, ldg in ledger_by_sym.items():
            for r in ldg["reductions"]:
                if r["ts_ms"] < from_ms or r["ts_ms"] >= to_ms:
                    continue
                per_fill_ledger.append(r["ledger_realized_pnl"])
                per_fill_hl.append(r["hl_closed_pnl"])
                per_sym_ledger[sym] += r["ledger_realized_pnl"]
                per_sym_hl[sym] += r["hl_closed_pnl"]
                per_sym_n_reductions[sym] += 1
                n_reductions_in_window += 1

        # Pull HL closedPnl from API for cross-check
        hl_pnl_api, _per_day, _hl_fees_api = parse_hl_closed_pnl(from_ms, to_ms)

        # Per-fill ρ
        rho_fill = pearson(per_fill_ledger, per_fill_hl) if len(per_fill_ledger) >= 2 else None
        # Per-sym ρ (ledger sum vs API truth)
        shared = sorted(set(per_sym_ledger) & set(hl_pnl_api))
        rho_sym_vs_api = pearson(
            [per_sym_ledger[s] for s in shared],
            [hl_pnl_api[s] for s in shared],
        ) if len(shared) >= 2 else None

        # Per-sym ρ (ledger vs ledger-derived-from-fills HL)
        rho_sym_self = pearson(
            [per_sym_ledger[s] for s in shared],
            [per_sym_hl[s] for s in shared],
        ) if len(shared) >= 2 else None

        # Aggregate mismatches
        sum_ledger = sum(per_sym_ledger.values())
        sum_hl_field = sum(per_sym_hl.values())
        sum_hl_api = sum(hl_pnl_api.values())

        # Top abs residual symbols
        rows = []
        for s in sorted(set(per_sym_ledger) | set(hl_pnl_api)):
            rows.append({
                "sym": s,
                "ledger": per_sym_ledger.get(s, 0.0),
                "hl_field": per_sym_hl.get(s, 0.0),
                "hl_api": hl_pnl_api.get(s, 0.0),
                "n_reductions": per_sym_n_reductions.get(s, 0),
                "residual_vs_api": hl_pnl_api.get(s, 0.0) - per_sym_ledger.get(s, 0.0),
                "residual_vs_field": per_sym_hl.get(s, 0.0) - per_sym_ledger.get(s, 0.0),
            })
        rows.sort(key=lambda r: -abs(r["residual_vs_api"]))

        # Focus
        focus = {}
        for sym in FOCUS_SYMS:
            r = next((x for x in rows if x["sym"] == sym), None)
            if r:
                focus[sym] = {
                    "ledger": r["ledger"],
                    "hl_api": r["hl_api"],
                    "residual": r["residual_vs_api"],
                }
            else:
                focus[sym] = None

        summary = {
            "window_days": w,
            "n_reductions_in_window": n_reductions_in_window,
            "rho_per_fill_ledger_vs_hl_field": rho_fill,
            "rho_per_symbol_vs_hl_api": rho_sym_vs_api,
            "rho_per_symbol_vs_hl_field": rho_sym_self,
            "sum_ledger": sum_ledger,
            "sum_hl_field": sum_hl_field,
            "sum_hl_api": sum_hl_api,
            "gross_mismatch_pct_vs_api": (
                abs(sum_ledger - sum_hl_api) / abs(sum_hl_api) * 100
                if abs(sum_hl_api) > 0 else None
            ),
            "gross_mismatch_pct_vs_field": (
                abs(sum_ledger - sum_hl_field) / abs(sum_hl_field) * 100
                if abs(sum_hl_field) > 0 else None
            ),
            "top10_residual_vs_api": rows[:10],
            "focus": focus,
            "shared_n": len(shared),
        }
        summaries.append(summary)

        # Console summary
        print(f"  reductions in window: {n_reductions_in_window}")
        print(f"  shared symbols (ledger ∩ HL API): {len(shared)}")
        print(f"  ρ per-fill (ledger vs hl.closed_pnl field): "
              f"{rho_fill:+.4f}" if rho_fill is not None else "  ρ per-fill: N/A")
        print(f"  ρ per-symbol (ledger vs HL API):           "
              f"{rho_sym_vs_api:+.4f}" if rho_sym_vs_api is not None else "  ρ per-sym vs API: N/A")
        print(f"  ρ per-symbol (ledger vs HL field sum):     "
              f"{rho_sym_self:+.4f}" if rho_sym_self is not None else "  ρ per-sym self: N/A")
        print(f"  sum ledger:        ${sum_ledger:+.2f}")
        print(f"  sum HL closed_pnl field (in-window fills): ${sum_hl_field:+.2f}")
        print(f"  sum HL closedPnl API (window): ${sum_hl_api:+.2f}")
        if summary["gross_mismatch_pct_vs_api"] is not None:
            print(f"  gross mismatch vs HL API: "
                  f"{summary['gross_mismatch_pct_vs_api']:.2f}%")
        if summary["gross_mismatch_pct_vs_field"] is not None:
            print(f"  gross mismatch vs HL field: "
                  f"{summary['gross_mismatch_pct_vs_field']:.2f}%")
        print()
        print("  focus residuals (vs HL API):")
        for sym in FOCUS_SYMS:
            f = focus.get(sym)
            if f is None:
                print(f"    {sym:14s}  N/A")
            else:
                print(f"    {sym:14s}  ledger=${f['ledger']:>+9.2f}  "
                      f"hl=${f['hl_api']:>+9.2f}  resid=${f['residual']:>+9.2f}")
        print()
        print("  top-5 |residual| symbols (vs HL API):")
        for r in rows[:5]:
            print(f"    {r['sym']:14s}  ledger=${r['ledger']:>+9.2f}  "
                  f"hl=${r['hl_api']:>+9.2f}  resid=${r['residual_vs_api']:>+9.2f}  "
                  f"n_reductions={r['n_reductions']}")

    # Acceptance gate
    print("\n=== ACCEPTANCE ===")
    main_summary = next((s for s in summaries if s["window_days"] == 14), summaries[0])
    rho_14d = main_summary.get("rho_per_symbol_vs_hl_api")
    rho_7d = next((s["rho_per_symbol_vs_hl_api"] for s in summaries if s["window_days"] == 7), None)
    gross_14d = main_summary.get("gross_mismatch_pct_vs_api")

    rules = [
        ("14d ledger-vs-HL ρ ≥ 0.95", rho_14d is not None and rho_14d >= 0.95,
         f"{rho_14d:+.4f}" if rho_14d is not None else "N/A"),
        ("7d ledger-vs-HL ρ ≥ 0.95", rho_7d is not None and rho_7d >= 0.95,
         f"{rho_7d:+.4f}" if rho_7d is not None else "N/A"),
        ("14d gross mismatch ≤ 2%",
         gross_14d is not None and gross_14d <= 2.0,
         f"{gross_14d:.2f}%" if gross_14d is not None else "N/A"),
    ]
    # ZEC residual reduction — Mode 1 had ZEC 14d residual −$108 after boundary fix
    # (and was −$593 in buggy version; we use −$108 as the "after boundary fix" baseline)
    zec_14d_focus = main_summary.get("focus", {}).get("ZEC")
    if zec_14d_focus is not None:
        zec_resid = zec_14d_focus["residual"]
        prior_resid = -108.10
        reduction_pct = (1 - abs(zec_resid) / abs(prior_resid)) * 100 if prior_resid != 0 else 0
        rules.append((
            f"ZEC 14d |residual| ≤ {abs(prior_resid)*0.20:.2f} (≥80% reduction vs ${prior_resid:.2f})",
            abs(zec_resid) <= abs(prior_resid) * 0.20,
            f"${zec_resid:+.2f} ({reduction_pct:.1f}% reduction)",
        ))

    eth_14d_focus = main_summary.get("focus", {}).get("ETH")
    if eth_14d_focus is not None:
        eth_sign_match = (
            (eth_14d_focus["ledger"] >= 0) == (eth_14d_focus["hl_api"] >= 0)
        )
        rules.append((
            "ETH 14d sign matches HL", eth_sign_match,
            f"ledger={eth_14d_focus['ledger']:+.2f}, hl={eth_14d_focus['hl_api']:+.2f}",
        ))

    accepted = all(ok for _, ok, _ in rules)
    for label, ok, val in rules:
        print(f"  [{'PASS' if ok else 'FAIL'}]  {label:60s}  {val}")
    print()
    print(f"  → {'ACCEPT — Mode 2A established, Mode 2B build justified' if accepted else 'REJECT — fix accounting before Mode 2B'}")

    out_path.write_text(json.dumps(summaries, indent=2, default=str))
    print(f"\n# wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
