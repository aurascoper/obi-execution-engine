#!/usr/bin/env python3
"""Phase B audit — live PnL ground-truth integrity.

Three independent measurements of live realized PnL per symbol over the
last N days:

    A — exit_signal pnl_est * qty * entry_px / 100   (percent-derived)
    B — exit_signal side * qty * (exit_px - entry_px) (price-derived)
    C — HL fills user_fills_by_time, sum of closedPnl  (venue authoritative)

Decision rules per the GPT-5.5 plan:
    corr(A, C) >= 0.95 → use exit_signal/pct as ground truth
    corr(B, C) >= 0.95 → use exit_signal/px as ground truth
    both < 0.90 → validation cannot be used for promotion gating yet

Outputs per-symbol comparison + portfolio-level ρ between each pair.

Usage:
    HL_WALLET_ADDRESS=0x... venv/bin/python3 scripts/audit_live_pnl_ground_truth.py [--window-days 14]
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

sys.path.insert(0, str(ROOT))
from scripts.validate_replay_fit import pearson  # noqa: E402


def _parse_ts_ms(ts) -> int:
    if isinstance(ts, (int, float)):
        return int(ts * 1000) if ts < 1e12 else int(ts)
    if isinstance(ts, str):
        try:
            if ts.endswith("Z"):
                ts = ts[:-1] + "+00:00"
            return int(dt.datetime.fromisoformat(ts).timestamp() * 1000)
        except Exception:
            return 0
    return 0


def _norm(s: str) -> str:
    return (s or "").replace("/USD", "").replace("/USDC", "")


def measure_a_b_from_exit_signals(from_ms: int, to_ms: int):
    """Walk exit_signal events. Per event compute:
        A = pnl_est * qty * entry_px / 100   (percent-derived dollars)
        B = side * qty * (exit_px - entry_px) (price-derived dollars)
    Aggregate per symbol.
    Returns (per_sym_a, per_sym_b, n_events, n_diff_outliers).
    """
    per_sym_a: dict[str, float] = defaultdict(float)
    per_sym_b: dict[str, float] = defaultdict(float)
    n_events = 0
    n_diff_outliers = 0  # |A - B| / max(|A|,|B|,1) > 0.10
    diffs: list[tuple[str, float, float, float]] = []  # (sym, a, b, ratio)
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
            ts = _parse_ts_ms(o.get("timestamp", ""))
            if ts < from_ms or ts >= to_ms:
                continue
            sym = _norm(o.get("symbol") or o.get("coin") or "")
            if not sym:
                continue
            try:
                pct = o.get("pnl_est")
                qty = o.get("qty")
                entry_px = o.get("entry_px")
                exit_px = o.get("exit_px")
                direction = (o.get("direction") or "").lower()
                if pct is None or qty is None or entry_px is None or exit_px is None:
                    continue
                pct = float(pct)
                qty = float(qty)
                entry_px = float(entry_px)
                exit_px = float(exit_px)
                # Filter NaN/Inf values that pollute aggregates
                if any(
                    math.isnan(v) or math.isinf(v)
                    for v in (pct, qty, entry_px, exit_px)
                ):
                    continue
                side = 1 if direction == "long" else (-1 if direction == "short" else 0)
                if side == 0 or entry_px <= 0 or qty <= 0:
                    continue
            except (TypeError, ValueError):
                continue
            a = pct / 100.0 * qty * entry_px
            b = side * qty * (exit_px - entry_px)
            per_sym_a[sym] += a
            per_sym_b[sym] += b
            n_events += 1
            denom = max(abs(a), abs(b), 1.0)
            if abs(a - b) / denom > 0.10:
                n_diff_outliers += 1
                diffs.append((sym, a, b, abs(a - b) / denom))
    return dict(per_sym_a), dict(per_sym_b), n_events, n_diff_outliers, diffs


def measure_c_from_hl_fills(from_ms: int, to_ms: int):
    """Pull user_fills_by_time from HL native + xyz; sum closedPnl per symbol."""
    addr = os.environ.get("HL_WALLET_ADDRESS")
    if not addr:
        # try reading .env directly
        env_path = ROOT / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("HL_WALLET_ADDRESS="):
                    addr = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not addr:
        raise SystemExit("HL_WALLET_ADDRESS not in env or .env")

    from hyperliquid.info import Info
    from hyperliquid.utils import constants

    # Pull from native + every builder DEX we trade on (xyz, vntl, hyna, flx, km, cash, para).
    native = Info(constants.MAINNET_API_URL, skip_ws=True)
    builder_dexes = ("xyz", "vntl", "hyna", "flx", "km", "cash", "para")
    builder_infos = []
    for dex in builder_dexes:
        try:
            builder_infos.append(
                (Info(constants.MAINNET_API_URL, skip_ws=True, perp_dexs=[dex]), dex)
            )
        except Exception as e:
            print(f"# warn: builder {dex} init failed: {e}", file=sys.stderr)

    per_sym: dict[str, float] = defaultdict(float)
    fees: dict[str, float] = defaultdict(float)
    n_fills = 0

    sources = [(native, "native")] + builder_infos
    for info, label in sources:
        try:
            fills = (
                info.user_fills_by_time(addr, from_ms, to_ms, aggregate_by_time=False)
                or []
            )
        except Exception as e:
            print(f"# warn: {label} user_fills_by_time failed: {e}", file=sys.stderr)
            continue
        for f in fills:
            sym = _norm(f.get("coin", ""))
            if not sym:
                continue
            try:
                pnl = float(f.get("closedPnl", 0) or 0)
                fee = float(f.get("fee", 0) or 0)
            except (TypeError, ValueError):
                continue
            per_sym[sym] += pnl
            fees[sym] += fee
            n_fills += 1

    return dict(per_sym), dict(fees), n_fills


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-days", type=int, default=14)
    args = ap.parse_args()

    to_ms = int(dt.datetime.now(tz=dt.timezone.utc).timestamp() * 1000)
    from_ms = to_ms - args.window_days * 86_400_000

    print(f"# window: {args.window_days}d")

    print("# computing A (pnl_est-derived dollars) and B (price-derived dollars)...")
    a, b, n_events, n_outliers, diffs = measure_a_b_from_exit_signals(from_ms, to_ms)
    print(f"#   exit_signal events: {n_events}")
    print(
        f"#   |A-B|/max > 10% per-event: {n_outliers}  ({n_outliers / max(n_events, 1):.1%})"
    )

    print("# computing C (HL closedPnl from user_fills_by_time)...")
    c, fees, n_fills = measure_c_from_hl_fills(from_ms, to_ms)
    print(f"#   HL fills: {n_fills}, symbols: {len(c)}")

    print()
    print("=== Aggregate dollars per source ===")
    print(
        f"  sum(A)  exit_signal pnl_est * qty * entry / 100  = ${sum(a.values()):+.2f}"
    )
    print(
        f"  sum(B)  side * qty * (exit_px - entry_px)         = ${sum(b.values()):+.2f}"
    )
    print(
        f"  sum(C)  HL closedPnl                              = ${sum(c.values()):+.2f}"
    )
    print(
        f"  fees(C) sum                                        = ${sum(fees.values()):+.2f}"
    )
    print(
        f"  net(C - fees)                                      = ${sum(c.values()) - sum(fees.values()):+.2f}"
    )

    # Pearson correlations on shared symbols
    shared_ab = sorted(set(a) & set(b))
    shared_ac = sorted(set(a) & set(c))
    shared_bc = sorted(set(b) & set(c))

    rho_ab = (
        pearson([a[s] for s in shared_ab], [b[s] for s in shared_ab])
        if len(shared_ab) >= 2
        else None
    )
    rho_ac = (
        pearson([a[s] for s in shared_ac], [c[s] for s in shared_ac])
        if len(shared_ac) >= 2
        else None
    )
    rho_bc = (
        pearson([b[s] for s in shared_bc], [c[s] for s in shared_bc])
        if len(shared_bc) >= 2
        else None
    )

    print()
    print("=== Pearson ρ between methods ===")
    print(
        f"  ρ(A, B)   exit_signal pct vs px self-consistency  = {rho_ab:.4f}  (n={len(shared_ab)})"
    )
    print(
        f"  ρ(A, C)   pct-derived vs HL truth                  = {rho_ac:.4f}  (n={len(shared_ac)})"
    )
    print(
        f"  ρ(B, C)   price-derived vs HL truth                = {rho_bc:.4f}  (n={len(shared_bc)})"
    )

    # Per-symbol top-15 by |A|
    print()
    print("=== top-15 symbols by |A| ===")
    print(
        f"  {'sym':<14s}  {'A=pct$':>9s}  {'B=px$':>9s}  {'C=HL$':>9s}  {'A-B':>8s}  {'A-C':>8s}  {'B-C':>8s}"
    )
    all_syms = sorted(set(a) | set(b) | set(c), key=lambda s: -abs(a.get(s, 0)))
    for s in all_syms[:15]:
        a_v = a.get(s, 0.0)
        b_v = b.get(s, 0.0)
        c_v = c.get(s, 0.0)
        print(
            f"  {s:<14s}  {a_v:>+9.2f}  {b_v:>+9.2f}  {c_v:>+9.2f}  "
            f"{a_v - b_v:>+8.2f}  {a_v - c_v:>+8.2f}  {b_v - c_v:>+8.2f}"
        )

    # Symbols where A and B disagree most
    print()
    print("=== top-10 |A − B| outliers (per-symbol disagreement) ===")
    by_ab_diff = sorted(
        [(s, a.get(s, 0.0), b.get(s, 0.0)) for s in set(a) | set(b)],
        key=lambda t: -abs(t[1] - t[2]),
    )
    for s, av, bv in by_ab_diff[:10]:
        print(f"  {s:<14s}  A={av:>+9.2f}  B={bv:>+9.2f}  diff={av - bv:>+9.2f}")

    print()
    print("=== decision ===")
    if rho_ac is None or rho_bc is None:
        print("  insufficient data for ρ comparison")
        return
    if rho_ac >= 0.95:
        print(
            f"  ρ(A, C) = {rho_ac:.3f} >= 0.95 → use exit_signal pnl_est-derived as ground truth"
        )
    elif rho_bc >= 0.95:
        print(
            f"  ρ(B, C) = {rho_bc:.3f} >= 0.95 → use exit_signal price-derived as ground truth"
        )
    elif max(rho_ac, rho_bc) >= 0.90:
        better = "A" if rho_ac > rho_bc else "B"
        print(
            f"  best ρ = {max(rho_ac, rho_bc):.3f} ({better} vs C); marginal — usable with caveat"
        )
    else:
        print(f"  ρ(A, C) = {rho_ac:.3f}, ρ(B, C) = {rho_bc:.3f} — both < 0.90")
        print("  → validation cannot be used for promotion gating yet.")
        print("  → exit_signal does not reliably reflect HL realized PnL.")
        print(
            "  → Diagnose: partial fills, multi-fill exits, sign-flip handling, fee accounting."
        )


if __name__ == "__main__":
    main()
