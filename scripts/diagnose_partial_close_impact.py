#!/usr/bin/env python3
"""Partial-close oracle diagnostic — ceiling estimate before any model.

Question: if replay stopped treating every exit as a full-position close,
how much ρ could improve?

Method (no replay-side changes):
  1. For each symbol, count partial-close evidence (n_partial_closes from
     fills, n_ratchet_tranches, n_auto_topup_fires).
  2. Mark a symbol as "partial-close-path" if any of those > 0.
  3. Oracle-adjusted replay PnL per symbol:
        if partial_close_path:
            replay_oracle = replay + residual * w
        else:
            replay_oracle = replay
  4. Recompute Pearson ρ between (replay_oracle, hl_closedPnl) at
     w ∈ {0.25, 0.50, 0.75, 1.00}.

This estimates the CEILING of partial-close modeling without committing
to a structural model. Promotion thresholds:
  oracle Δρ < +0.04           deprioritize partial-close modeling
  oracle Δρ +0.05..+0.15      implement partial-close replay
  oracle Δρ > +0.15           partial-close is dominant residual driver

Both 14d and 7d windows reported per the user's guardrail.

Usage:
    venv/bin/python3 scripts/diagnose_partial_close_impact.py
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
LOG = ROOT / "logs" / "hl_engine.jsonl"
RATCHET_LOG = ROOT / "logs" / "shock_ratchet.log"
TOPUP_LOG = Path("/tmp/auto_topup.log")

sys.path.insert(0, str(ROOT))
from scripts.validate_replay_fit import (  # noqa: E402
    parse_hl_closed_pnl,
    pearson,
)

ATTRIBUTION_WEIGHTS = (0.25, 0.50, 0.75, 1.00)


def _norm(s: str) -> str:
    return (s or "").replace("/USD", "").replace("/USDC", "")


def _addr() -> str:
    a = os.environ.get("HL_WALLET_ADDRESS")
    if not a:
        env_path = ROOT / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("HL_WALLET_ADDRESS="):
                    a = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not a:
        raise SystemExit("HL_WALLET_ADDRESS missing")
    return a


def fills_inventory(from_ms: int, to_ms: int):
    """Return per-sym fill stats: n_fills, n_opens, n_partial_closes,
    n_full_closes. Walks HL API fills (authoritative)."""
    addr = _addr()
    from hyperliquid.info import Info
    from hyperliquid.utils import constants

    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    try:
        fills = info.user_fills_by_time(addr, from_ms, to_ms, aggregate_by_time=False) or []
    except Exception as e:
        print(f"# warn: fills fetch failed: {e}", file=sys.stderr)
        fills = []
    n_fills = defaultdict(int)
    n_opens = defaultdict(int)
    n_partial = defaultdict(int)
    n_full = defaultdict(int)
    pos_track: dict[str, float] = defaultdict(float)
    for f in sorted(fills, key=lambda x: int(x.get("time", 0) or 0)):
        sym = _norm(f.get("coin", ""))
        if not sym:
            continue
        try:
            sz = float(f.get("sz", 0) or 0)
            cp = float(f.get("closedPnl", 0) or 0)
            side = (f.get("side") or "").upper()
        except (TypeError, ValueError):
            continue
        d = sz if side == "B" else -sz
        prev = pos_track[sym]
        new = prev + d
        n_fills[sym] += 1
        is_open = abs(new) > abs(prev) + 1e-9
        if is_open:
            n_opens[sym] += 1
        elif cp != 0.0:
            # closing fill (full or partial)
            if abs(new) < 1e-9:
                n_full[sym] += 1
            else:
                n_partial[sym] += 1
        pos_track[sym] = new
    return dict(n_fills), dict(n_opens), dict(n_partial), dict(n_full)


def count_ratchet(from_ms: int, to_ms: int) -> dict[str, int]:
    cnt = defaultdict(int)
    if not RATCHET_LOG.exists():
        return dict(cnt)
    sym_re = re.compile(r"symbol=([A-Za-z0-9:_/]+)")
    tag_re = re.compile(r"tag=shock_ratchet")
    ts_re = re.compile(r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})")
    with RATCHET_LOG.open() as fh:
        for line in fh:
            if not tag_re.search(line):
                continue
            m_ts = ts_re.match(line)
            if m_ts:
                try:
                    ts_ms = int(
                        dt.datetime.fromisoformat(
                            m_ts.group(1).replace(" ", "T") + "+00:00"
                        ).timestamp() * 1000
                    )
                except Exception:
                    ts_ms = 0
                if ts_ms < from_ms or ts_ms >= to_ms:
                    continue
            m_sym = sym_re.search(line)
            if m_sym:
                cnt[_norm(m_sym.group(1))] += 1
    return dict(cnt)


def count_topup(from_ms: int, to_ms: int) -> dict[str, int]:
    cnt = defaultdict(int)
    if not TOPUP_LOG.exists():
        return dict(cnt)
    fire_re = re.compile(r"^(\S+)\s+FIRE\s+([A-Za-z0-9:_/]+)\s")
    with TOPUP_LOG.open() as fh:
        for line in fh:
            m = fire_re.match(line)
            if not m:
                continue
            try:
                ts_ms = int(
                    dt.datetime.fromisoformat(m.group(1).replace("Z", "+00:00")).timestamp() * 1000
                )
            except Exception:
                continue
            if ts_ms < from_ms or ts_ms >= to_ms:
                continue
            cnt[_norm(m.group(2))] += 1
    return dict(cnt)


def replay_per_sym(from_ms: int, to_ms: int) -> dict[str, float]:
    env = os.environ.copy()
    env["REPLAY_FROM_MS"] = str(from_ms)
    env["REPLAY_TO_MS"] = str(to_ms)
    with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False) as tmp:
        env["REPLAY_PERSYM_OUT"] = tmp.name
        out_path = tmp.name
    cmd = [sys.executable, str(ROOT / "scripts" / "z_entry_replay_gated.py")]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, cwd=ROOT)
    if r.returncode != 0:
        print(r.stderr[-1000:], file=sys.stderr)
        raise SystemExit(f"replay failed rc={r.returncode}")
    return json.loads(Path(out_path).read_text())


def run_window(window_days: int):
    to_ms = int(dt.datetime.now(tz=dt.timezone.utc).timestamp() * 1000)
    from_ms = to_ms - window_days * 86_400_000

    print(f"## window {window_days}d  [{from_ms}..{to_ms})", file=sys.stderr)

    print("# pulling HL closedPnl + fees...", file=sys.stderr)
    hl_pnl, _hl_per_day, hl_fees = parse_hl_closed_pnl(from_ms, to_ms)

    print("# fills inventory...", file=sys.stderr)
    n_fills, n_opens, n_partial, n_full = fills_inventory(from_ms, to_ms)

    print("# ratchet + topup counts...", file=sys.stderr)
    ratchet = count_ratchet(from_ms, to_ms)
    topup = count_topup(from_ms, to_ms)

    print("# running replay...", file=sys.stderr)
    sim_pnl = replay_per_sym(from_ms, to_ms)

    syms = sorted(set(hl_pnl) | set(sim_pnl))
    rows = []
    for s in syms:
        hl = hl_pnl.get(s, 0.0)
        sim = sim_pnl.get(s, 0.0)
        residual = hl - sim
        path = (n_partial.get(s, 0) > 0) or (ratchet.get(s, 0) > 0) or (topup.get(s, 0) > 0)
        rows.append(
            {
                "symbol": s,
                "hl_closed_pnl": hl,
                "replay_pnl": sim,
                "residual": residual,
                "abs_residual": abs(residual),
                "n_fills": n_fills.get(s, 0),
                "n_opens": n_opens.get(s, 0),
                "n_partial_closes": n_partial.get(s, 0),
                "n_full_closes": n_full.get(s, 0),
                "ratchet_tranches": ratchet.get(s, 0),
                "auto_topup_count": topup.get(s, 0),
                "partial_close_path": path,
            }
        )

    # Baseline: shared symbols, ρ
    shared = sorted(set(hl_pnl) & set(sim_pnl))
    base_rho = pearson([sim_pnl[s] for s in shared], [hl_pnl[s] for s in shared])

    # Oracle: replay_oracle = replay + residual * w (only on partial-close-path symbols)
    oracle_rho = {}
    oracle_replay_total = {}
    for w in ATTRIBUTION_WEIGHTS:
        adjusted: dict[str, float] = {}
        for r in rows:
            s = r["symbol"]
            if s in sim_pnl:
                base = sim_pnl[s]
                if r["partial_close_path"]:
                    adjusted[s] = base + r["residual"] * w
                else:
                    adjusted[s] = base
        rho = pearson([adjusted[s] for s in shared], [hl_pnl[s] for s in shared])
        oracle_rho[w] = rho
        oracle_replay_total[w] = sum(adjusted.values())

    n_path = sum(1 for r in rows if r["partial_close_path"])
    abs_residual_path = sum(r["abs_residual"] for r in rows if r["partial_close_path"])
    abs_residual_other = sum(r["abs_residual"] for r in rows if not r["partial_close_path"])

    return {
        "window_days": window_days,
        "from_ms": from_ms,
        "to_ms": to_ms,
        "n_symbols": len(rows),
        "n_partial_path": n_path,
        "abs_residual_path": abs_residual_path,
        "abs_residual_other": abs_residual_other,
        "shared_n": len(shared),
        "base_rho": base_rho,
        "oracle_rho": oracle_rho,
        "oracle_replay_total": oracle_replay_total,
        "live_total": sum(hl_pnl.values()),
        "rows": rows,
    }


def fmt_rho(rho):
    return f"{rho:+.4f}" if rho is not None else "  N/A "


def report(result: dict):
    w = result["window_days"]
    base = result["base_rho"]
    print(f"\n=== window {w}d ===")
    print(
        f"  symbols: {result['n_symbols']}  partial_close_path: {result['n_partial_path']}  "
        f"shared (replay∩live): {result['shared_n']}"
    )
    print(
        f"  abs residual (path): ${result['abs_residual_path']:.2f}  "
        f"other: ${result['abs_residual_other']:.2f}"
    )
    print(f"  baseline ρ: {fmt_rho(base)}")
    print("  oracle ρ at attribution weights:")
    for w_attr in ATTRIBUTION_WEIGHTS:
        rho = result["oracle_rho"][w_attr]
        d = (rho - base) if (rho is not None and base is not None) else None
        d_s = f"Δ{d:+.4f}" if d is not None else "Δ  N/A"
        rep_total = result["oracle_replay_total"][w_attr]
        print(
            f"    w={w_attr:.2f}  ρ={fmt_rho(rho)}  {d_s}  "
            f"oracle_replay_total=${rep_total:+.2f}  "
            f"live_total=${result['live_total']:+.2f}"
        )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--windows", default="14,7")
    ap.add_argument("--out", default=str(ROOT / "autoresearch_gated" / "partial_close_oracle.json"))
    args = ap.parse_args()

    windows = [int(w) for w in args.windows.split(",") if w]
    results = []
    for w in windows:
        r = run_window(w)
        report(r)
        results.append(r)

    # Verdict per the decision rules
    print("\n=== verdict ===")
    for r in results:
        w = r["window_days"]
        base = r["base_rho"]
        # Use w=1.00 as "ceiling"
        ceiling = r["oracle_rho"].get(1.00)
        if base is None or ceiling is None:
            print(f"  {w}d: insufficient data")
            continue
        d_ceiling = ceiling - base
        if d_ceiling < 0.04:
            verdict = "DEPRIORITIZE — partial-close oracle ceiling too small"
        elif d_ceiling < 0.15:
            verdict = "IMPLEMENT — partial-close replay model worth building"
        else:
            verdict = "DOMINANT — partial-close mismatch is the main residual driver"
        print(
            f"  {w}d: base ρ={fmt_rho(base)}  ceiling (w=1) ρ={fmt_rho(ceiling)}  "
            f"Δρ={d_ceiling:+.4f}  → {verdict}"
        )

    # Save numeric results
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    serial = []
    for r in results:
        rr = dict(r)
        rr["rows"] = [
            {k: v for k, v in row.items() if not isinstance(v, set)}
            for row in r["rows"]
        ]
        rr["oracle_rho"] = {str(k): v for k, v in r["oracle_rho"].items()}
        rr["oracle_replay_total"] = {str(k): v for k, v in r["oracle_replay_total"].items()}
        serial.append(rr)
    out.write_text(json.dumps(serial, indent=2, default=str))
    print(f"\n# wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
