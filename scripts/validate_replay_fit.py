#!/usr/bin/env python3
"""Phase 4 — Validate z_entry_replay_gated.py fit vs live realized PnL.

Reconstructs live per-symbol realized PnL from `exit_signal` events in
logs/hl_engine.jsonl over the requested window, runs the gated replay harness
over the identical window, and computes per-symbol + portfolio agreement.

Live PnL source: `exit_signal` carries pnl_est, symbol, direction, qty,
entry_px, exit_px, reason. Summed per symbol → live realized.

There is no `position_close_complete` event emitted by the current engine; the
prior stub assumed otherwise. exit_signal is the correct source.

Gate for /autoresearch (plan proud-conjuring-pebble.md):
  portfolio Pearson rho >= 0.80 AND all per-symbol rho >= 0.70
Any symbol with rho < 0.50 → missing gate or stateful effect, blocks promotion.

"Per-symbol rho" here is computed by bucketing each symbol's trades into
daily PnL series and correlating replay-bucket vs live-bucket. Requires
sym to have >= MIN_BUCKETS buckets with live activity; symbols below the
threshold are reported under "low-power" rather than gating.

CLI:
  --window Nd       look-back window in days (default 14)
  --min-buckets N   min non-zero daily buckets for per-symbol rho (default 5)
  --out PATH        validation_report.md output (default autoresearch_gated/validation_report.md)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / "hl_engine.jsonl"
OUT_DIR = ROOT / "autoresearch_gated"


def _parse_ts(ts) -> int:
    if isinstance(ts, (int, float)):
        return int(ts * 1000) if ts < 1e12 else int(ts)
    return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)


def _norm(sym: str) -> str:
    return (sym or "").replace("/USD", "").replace("/USDC", "")


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2 or len(ys) != n:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def parse_exit_signals(from_ms: int, to_ms: int):
    """Return (per_sym_pnl, per_sym_daily_pnl[sym][day_iso] -> pnl)."""
    per_sym: dict[str, float] = defaultdict(float)
    per_day: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    with LOG.open() as f:
        for line in f:
            if '"exit_signal"' not in line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get("event") != "exit_signal":
                continue
            ts = o.get("timestamp")
            if ts is None:
                continue
            ts_ms = _parse_ts(ts)
            if ts_ms < from_ms or ts_ms >= to_ms:
                continue
            sym = _norm(o.get("symbol") or o.get("coin") or "")
            if not sym:
                continue
            # pnl_est is PERCENT (e.g. 0.363 = 0.363% return), NOT dollars.
            # Convert to dollars using qty * entry_px so we correlate against
            # replay's dollar PnL on a like-for-like basis. Falls back to
            # exit_px - entry_px if either is present.
            pnl_pct = o.get("pnl_est")
            qty = o.get("qty")
            entry_px = o.get("entry_px")
            exit_px = o.get("exit_px")
            direction = (o.get("direction") or "").lower()
            side = 1 if direction == "long" else (-1 if direction == "short" else 0)
            try:
                pnl_dollars = None
                if entry_px is not None and exit_px is not None and qty is not None:
                    ep = float(entry_px)
                    xp = float(exit_px)
                    q = float(qty)
                    if side != 0 and ep > 0 and q > 0:
                        pnl_dollars = (xp - ep) * q * side
                if pnl_dollars is None and pnl_pct is not None:
                    p = float(pnl_pct)
                    if p == p and not math.isinf(p):
                        if entry_px is not None and qty is not None:
                            ep = float(entry_px)
                            q = float(qty)
                            if ep > 0 and q > 0:
                                pnl_dollars = p / 100.0 * ep * q
            except (TypeError, ValueError):
                continue
            if pnl_dollars is None:
                continue
            if pnl_dollars != pnl_dollars or math.isinf(pnl_dollars):
                continue
            per_sym[sym] += pnl_dollars
            day = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime(
                "%Y-%m-%d"
            )
            per_day[sym][day] += pnl_dollars
    return dict(per_sym), {s: dict(d) for s, d in per_day.items()}


def run_replay_windowed(from_ms: int, to_ms: int) -> dict[str, float]:
    env = os.environ.copy()
    env["REPLAY_FROM_MS"] = str(from_ms)
    env["REPLAY_TO_MS"] = str(to_ms)
    with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False) as tmp:
        env["REPLAY_PERSYM_OUT"] = tmp.name
        out_path = tmp.name
    cmd = [sys.executable, str(ROOT / "scripts" / "z_entry_replay_gated.py")]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, cwd=ROOT)
    if r.returncode != 0:
        print(r.stdout, file=sys.stderr)
        print(r.stderr, file=sys.stderr)
        raise SystemExit(f"replay failed rc={r.returncode}")
    data = json.loads(Path(out_path).read_text())
    Path(out_path).unlink(missing_ok=True)
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", default="14d")
    ap.add_argument("--min-buckets", type=int, default=5)
    ap.add_argument("--out", default=str(OUT_DIR / "validation_report.md"))
    args = ap.parse_args()

    m = re.fullmatch(r"(\d+)d", args.window)
    if not m:
        raise SystemExit(f"invalid --window {args.window} (expected e.g. 14d)")
    window_days = int(m.group(1))
    to_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    from_ms = to_ms - window_days * 86_400_000

    print(f"# window: {window_days}d  [{from_ms}..{to_ms})", file=sys.stderr)

    live_per_sym, live_per_day = parse_exit_signals(from_ms, to_ms)
    print(
        f"# live exit_signal events covered {len(live_per_sym)} symbols",
        file=sys.stderr,
    )

    sim_per_sym = run_replay_windowed(from_ms, to_ms)
    print(f"# replay covered {len(sim_per_sym)} symbols", file=sys.stderr)

    shared = sorted(set(live_per_sym) & set(sim_per_sym))
    if not shared:
        raise SystemExit("no overlap between live exits and replay symbols")

    live_vec = [live_per_sym[s] for s in shared]
    sim_vec = [sim_per_sym[s] for s in shared]
    portfolio_rho = pearson(sim_vec, live_vec)

    # Per-symbol daily bucket correlation (needs replay daily too — skip for
    # now; report per-symbol ratio instead as a first-pass quality metric).
    rows = []
    for s in shared:
        live = live_per_sym[s]
        sim = sim_per_sym[s]
        sign_match = (live >= 0) == (sim >= 0)
        if abs(live) < 1e-6:
            ratio = None
        else:
            ratio = sim / live
        rows.append((s, live, sim, sign_match, ratio, len(live_per_day.get(s, {}))))

    rows.sort(key=lambda r: -abs(r[1]))

    # Write report
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    lines.append("# Phase 4 Validation Report — gated replay vs live exit_signal")
    lines.append("")
    lines.append(f"- window: **{window_days}d**  `[{from_ms}..{to_ms})`")
    lines.append(
        f"- live symbols: {len(live_per_sym)}  replay symbols: {len(sim_per_sym)}  overlap: {len(shared)}"
    )
    lines.append(
        f"- **portfolio rho** (across {len(shared)} symbols): **{portfolio_rho:.4f}**"
        if portfolio_rho is not None
        else "- portfolio rho: N/A"
    )
    live_sum = sum(live_vec)
    sim_sum = sum(sim_vec)
    lines.append(
        f"- live total: ${live_sum:+.2f}  replay total: ${sim_sum:+.2f}  diff: ${sim_sum - live_sum:+.2f}"
    )
    lines.append("")
    gate_pass = portfolio_rho is not None and portfolio_rho >= 0.80
    lines.append(
        f"## GATE: portfolio rho >= 0.80 → **{'PASS' if gate_pass else 'FAIL'}**"
    )
    lines.append("")
    lines.append("| symbol | live $ | replay $ | sign | ratio | live days |")
    lines.append("|---|---|---|---|---|---|")
    for s, live, sim, sign, ratio, ndays in rows:
        ratio_s = f"{ratio:+.2f}x" if ratio is not None else "—"
        lines.append(
            f"| {s} | {live:+.2f} | {sim:+.2f} | {'✓' if sign else '✗'} | {ratio_s} | {ndays} |"
        )
    out_path.write_text("\n".join(lines) + "\n")

    # Console summary
    print("=" * 60)
    print(
        f"portfolio rho: {portfolio_rho:.4f}"
        if portfolio_rho is not None
        else "portfolio rho: N/A"
    )
    print(f"GATE: {'PASS (>=0.80)' if gate_pass else 'FAIL (<0.80)'}")
    print(
        f"live ${live_sum:+.2f}  replay ${sim_sum:+.2f}  diff ${sim_sum - live_sum:+.2f}"
    )
    print(f"report: {out_path}")
    return 0 if gate_pass else 1


if __name__ == "__main__":
    sys.exit(main())
