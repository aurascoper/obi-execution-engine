#!/usr/bin/env python3
"""Per-symbol residual decomposition: replay vs HL closedPnl.

For each symbol active in the window, emit one row with:
  - hl_closed_pnl_gross, hl_fees, hl_closed_pnl_net (from user_fills_by_time)
  - replay_pnl_gross (from z_entry_replay_gated.py)
  - residual_gross = hl_gross - replay_gross
  - abs_residual_rank
  - fill_count, partial_close_count
  - auto_topup_count, shock_ratchet_fill_count
  - funding_pnl_if_available
  - hint_bucket — best-guess explanatory bucket per row

Buckets (from the user's plan):
  1) fees                          residual ≈ -hl_fees
  2) funding                       residual ≈ -funding (if available)
  3) shock_ratchet partial exits   ≥1 ratchet sell fill in window
  4) auto_topup VWA entry drift    ≥1 auto_topup entry in window
  5) missing live-only exits       fill_count(live) > replay-trade-count
  6) replay entry mismatch         replay PnL near zero, live nonzero

Usage:
    venv/bin/python3 scripts/diagnose_replay_hl_residual.py [--window-days 14]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
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


def _ts_ms(s) -> int:
    if isinstance(s, (int, float)):
        return int(s * 1000) if s < 1e12 else int(s)
    if isinstance(s, str):
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            return int(dt.datetime.fromisoformat(s).timestamp() * 1000)
        except Exception:
            return 0
    return 0


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


def hl_closed_pnl(from_ms: int, to_ms: int):
    """Return (per_sym_gross_pnl, per_sym_fees, per_sym_fill_count,
                per_sym_partial_close_count) over a single API pull
    (HL returns all fills across all clearinghouses for one address)."""
    addr = _addr()
    from hyperliquid.info import Info
    from hyperliquid.utils import constants

    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    try:
        fills = (
            info.user_fills_by_time(addr, from_ms, to_ms, aggregate_by_time=False) or []
        )
    except Exception as e:
        print(f"# warn: user_fills_by_time failed: {e}", file=sys.stderr)
        fills = []

    pnl = defaultdict(float)
    fees = defaultdict(float)
    n = defaultdict(int)
    partial = defaultdict(int)
    pos_track: dict[str, float] = defaultdict(float)
    for f in sorted(fills, key=lambda x: int(x.get("time", 0) or 0)):
        sym = _norm(f.get("coin", ""))
        if not sym:
            continue
        try:
            cp = float(f.get("closedPnl", 0) or 0)
            fee = float(f.get("fee", 0) or 0)
            sz = float(f.get("sz", 0) or 0)
            side = (f.get("side") or "").upper()
        except (TypeError, ValueError):
            continue
        pnl[sym] += cp
        fees[sym] += fee
        n[sym] += 1
        # Track position to detect partial-close fills (closedPnl != 0 but
        # not a full flatten).
        d = sz if side == "B" else -sz
        prev = pos_track[sym]
        new = prev + d
        if cp != 0.0 and abs(new) > 1e-9:
            partial[sym] += 1
        pos_track[sym] = new
    return dict(pnl), dict(fees), dict(n), dict(partial)


def replay_per_sym(from_ms: int, to_ms: int) -> dict[str, float]:
    """Run z_entry_replay_gated.py over the window; return per-sym pnl."""
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
    data = json.loads(Path(out_path).read_text())
    Path(out_path).unlink(missing_ok=True)
    return data


def count_event_per_sym(
    path: Path, event_name: str, from_ms: int, to_ms: int
) -> dict[str, int]:
    """Count occurrences of `event_name` per symbol in [from_ms, to_ms)."""
    cnt = defaultdict(int)
    if not path.exists():
        return dict(cnt)
    needle = f'"{event_name}"'
    with path.open() as fh:
        for line in fh:
            if needle not in line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get("event") != event_name:
                continue
            ts = _ts_ms(o.get("timestamp", "")) or _ts_ms(o.get("ts", ""))
            if ts < from_ms or ts >= to_ms:
                continue
            sym = _norm(o.get("symbol") or o.get("coin") or "")
            if not sym:
                continue
            cnt[sym] += 1
    return dict(cnt)


def grep_log_per_sym(
    path: Path, tag_pattern: str, from_ms: int, to_ms: int
) -> dict[str, int]:
    """Grep a non-jsonl log for tag=<pattern> lines and bucket per-symbol."""
    cnt = defaultdict(int)
    if not path.exists():
        return dict(cnt)
    import re

    pat = re.compile(tag_pattern)
    with path.open() as fh:
        for line in fh:
            if not pat.search(line):
                continue
            # Try to extract sym=... or coin=... or first uppercase token
            m = re.search(r"(?:sym|coin|symbol)=([A-Za-z0-9:_/]+)", line)
            if not m:
                continue
            sym = _norm(m.group(1))
            if not sym:
                continue
            cnt[sym] += 1
    return dict(cnt)


def classify(row: dict) -> str:
    """Pick the most likely explanatory bucket for a residual."""
    res = row["residual_gross"]
    if abs(res) < 5.0:
        return "neglig"
    fees = row["hl_fees"]
    if abs(res - (-fees)) / max(abs(res), 1.0) < 0.20:
        return "fees"
    if row["shock_ratchet_fills"] > 0:
        return "ratchet_partial"
    if row["auto_topup_fires"] > 0:
        return "topup_vwa_drift"
    replay = row["replay_pnl_gross"]
    live = row["hl_closed_pnl_gross"]
    if abs(replay) < 5.0 and abs(live) > 20.0:
        return "entry_mismatch"
    if row["partial_close_count"] > 0:
        return "partial_close_misc"
    if row["fill_count"] > 4 and abs(res) > 30.0:
        return "missing_live_exits"
    return "unexplained"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-days", type=int, default=14)
    ap.add_argument(
        "--out", default=str(ROOT / "autoresearch_gated" / "residual_report.md")
    )
    args = ap.parse_args()

    to_ms = int(dt.datetime.now(tz=dt.timezone.utc).timestamp() * 1000)
    from_ms = to_ms - args.window_days * 86_400_000

    print(f"# window: {args.window_days}d  [{from_ms}..{to_ms})", file=sys.stderr)

    print("# pulling HL closedPnl + fills...", file=sys.stderr)
    hl_pnl, hl_fees, hl_fill_n, hl_partial_n = hl_closed_pnl(from_ms, to_ms)

    print("# running replay over identical window...", file=sys.stderr)
    sim_pnl = replay_per_sym(from_ms, to_ms)

    print("# counting auto_topup fires per symbol...", file=sys.stderr)
    # auto_topup.log uses "FIRE <SYM>" lines — extract symbol directly from
    # the FIRE token rather than the generic sym=...= matcher.
    topup = defaultdict(int)
    if TOPUP_LOG.exists():
        import re

        fire_re = re.compile(r"^\S+\s+FIRE\s+([A-Za-z0-9:_/]+)\s")
        with TOPUP_LOG.open() as fh:
            for line in fh:
                m = fire_re.match(line)
                if not m:
                    continue
                # crude time filter: line begins with ISO timestamp like 2026-04-26T04:06:25Z
                ts_match = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z", line)
                if ts_match:
                    try:
                        ts_ms = int(
                            dt.datetime.fromisoformat(
                                ts_match.group(1) + "+00:00"
                            ).timestamp()
                            * 1000
                        )
                    except Exception:
                        ts_ms = 0
                    if ts_ms < from_ms or ts_ms >= to_ms:
                        continue
                sym = _norm(m.group(1))
                if sym:
                    topup[sym] += 1
    topup = dict(topup)

    print("# counting shock_ratchet sell fills per symbol...", file=sys.stderr)
    ratchet = grep_log_per_sym(RATCHET_LOG, r"tag=shock_ratchet", from_ms, to_ms)
    if not ratchet:
        ratchet = count_event_per_sym(LOG, "shock_ratchet_sell", from_ms, to_ms)

    syms = sorted(set(hl_pnl) | set(sim_pnl))
    rows = []
    for sym in syms:
        hl_g = hl_pnl.get(sym, 0.0)
        fees = hl_fees.get(sym, 0.0)
        sim_g = sim_pnl.get(sym, 0.0)
        rows.append(
            {
                "symbol": sym,
                "hl_closed_pnl_gross": hl_g,
                "hl_fees": fees,
                "hl_closed_pnl_net": hl_g - fees,
                "replay_pnl_gross": sim_g,
                "residual_gross": hl_g - sim_g,
                "fill_count": hl_fill_n.get(sym, 0),
                "partial_close_count": hl_partial_n.get(sym, 0),
                "auto_topup_fires": topup.get(sym, 0),
                "shock_ratchet_fills": ratchet.get(sym, 0),
                "funding_pnl": 0.0,  # not yet wired (Info.user_funding missing in installed sdk)
            }
        )

    # rank by absolute residual
    rows.sort(key=lambda r: -abs(r["residual_gross"]))
    for i, r in enumerate(rows):
        r["abs_residual_rank"] = i + 1
        r["hint_bucket"] = classify(r)

    # bucket totals
    bucket_totals = defaultdict(lambda: {"abs": 0.0, "n": 0, "signed": 0.0})
    for r in rows:
        b = bucket_totals[r["hint_bucket"]]
        b["abs"] += abs(r["residual_gross"])
        b["signed"] += r["residual_gross"]
        b["n"] += 1
    total_abs = sum(b["abs"] for b in bucket_totals.values())
    total_signed = sum(b["signed"] for b in bucket_totals.values())

    # Write report
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Residual decomposition — replay vs HL closedPnl",
        "",
        f"- window: **{args.window_days}d**  `[{from_ms}..{to_ms})`",
        f"- symbols: {len(rows)}",
        f"- total signed residual (HL gross − replay gross): **${total_signed:+.2f}**",
        f"- total absolute residual: **${total_abs:.2f}**",
        "",
        "## Bucket breakdown (by abs $)",
        "",
        "| bucket | n | abs $ | % | signed $ |",
        "|---|---|---|---|---|",
    ]
    for b, v in sorted(bucket_totals.items(), key=lambda kv: -kv[1]["abs"]):
        pct = (v["abs"] / total_abs * 100.0) if total_abs > 0 else 0.0
        lines.append(
            f"| {b} | {v['n']} | {v['abs']:.2f} | {pct:.1f}% | {v['signed']:+.2f} |"
        )
    lines.append("")
    lines.append("## Per-symbol detail (top 30 by |residual|)")
    lines.append("")
    lines.append(
        "| # | sym | HL$ gross | fees | net | replay$ | residual | fills | partial | topup | ratchet | bucket |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows[:30]:
        lines.append(
            f"| {r['abs_residual_rank']} | {r['symbol']} | "
            f"{r['hl_closed_pnl_gross']:+.2f} | {-r['hl_fees']:+.2f} | "
            f"{r['hl_closed_pnl_net']:+.2f} | {r['replay_pnl_gross']:+.2f} | "
            f"{r['residual_gross']:+.2f} | {r['fill_count']} | "
            f"{r['partial_close_count']} | {r['auto_topup_fires']} | "
            f"{r['shock_ratchet_fills']} | {r['hint_bucket']} |"
        )
    out.write_text("\n".join(lines) + "\n")

    print()
    print(f"=== Bucket totals (window {args.window_days}d) ===")
    for b, v in sorted(bucket_totals.items(), key=lambda kv: -kv[1]["abs"]):
        pct = (v["abs"] / total_abs * 100.0) if total_abs > 0 else 0.0
        print(
            f"  {b:22s}  n={v['n']:3d}  abs=${v['abs']:9.2f}  {pct:5.1f}%  signed=${v['signed']:+8.2f}"
        )
    print(
        f"  {'TOTAL':22s}  n={len(rows):3d}  abs=${total_abs:9.2f}          signed=${total_signed:+8.2f}"
    )
    print(f"\n# wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
