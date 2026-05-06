#!/usr/bin/env python3
"""book_b_posttrade_verify.py — pure read-only verification helper for
Book B after manual hedge layering (CRWV long + XYZ100 short).

Re-queries Book B state across native HL + xyz HIP-3 + spot. Asserts:
  - ZEC buffer ≥ 15% (still GREEN)
  - CRWV buffer ≥ 9% on entry at 10x leverage
  - XYZ100 buffer ≥ 9% on entry at 10x leverage
  - free spot USDC ≥ $50

Prints a labeled state table identical to the session "how about now?"
snapshots. Exits non-zero on any failed assertion so it can be wired into a
manual checklist.

NO writes. Ever.

Usage:
    venv/bin/python3 scripts/book_b_posttrade_verify.py
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

# ── Thresholds (load-bearing — match plans/elegant-churning-tide.md) ──────
ZEC_GREEN_THRESHOLD_PCT = 15.0
NEW_LINE_BUFFER_THRESHOLD_PCT = 9.0
RESERVE_FLOOR = 50.0

BOOK_B_MASTER = "0x32D178fc6BC4CCC7AFBDB7Db78317cF2Bbd6C048"
NORMAL_XYZ_SET = {"XYZ100", "GOLD", "SILVER"}


def _liq_buffer_pct(mark: float, liq: float) -> float | None:
    if liq is None or liq == 0:
        return None
    return abs(mark - liq) / mark * 100.0


def _label(buf: float | None, native_or_normal_iso: bool) -> str:
    if buf is None:
        return "⚫ BLACK"
    if native_or_normal_iso:
        if buf < 8:
            return "🔴 RED"
        if buf < 15:
            return "🟡 YEL"
        return "🟢 GRN"
    if buf < 10:
        return "🔴 RED"
    if buf < 15:
        return "🟡 YEL"
    return "🟢 GRN"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reserve-floor",
        type=float,
        default=RESERVE_FLOOR,
        help=f"Min free spot to require (default {RESERVE_FLOOR})",
    )
    parser.add_argument(
        "--zec-threshold",
        type=float,
        default=ZEC_GREEN_THRESHOLD_PCT,
        help=f"Min ZEC buffer %% (default {ZEC_GREEN_THRESHOLD_PCT})",
    )
    parser.add_argument(
        "--new-line-threshold",
        type=float,
        default=NEW_LINE_BUFFER_THRESHOLD_PCT,
        help=f"Min CRWV/XYZ100 buffer %% (default {NEW_LINE_BUFFER_THRESHOLD_PCT})",
    )
    parser.add_argument(
        "--require-new-lines",
        action="store_true",
        help="Fail if CRWV or XYZ100 are not present (default: warn-only).",
    )
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    addr = os.environ.get("HL_WALLET_ADDRESS")
    if not addr:
        print("ERROR: HL_WALLET_ADDRESS not set in .env", file=sys.stderr)
        return 1
    if addr.lower() != BOOK_B_MASTER.lower():
        print(
            f"ERROR: HL_WALLET_ADDRESS={addr} is not Book B master ({BOOK_B_MASTER}).",
            file=sys.stderr,
        )
        return 1

    from hyperliquid.info import Info

    info = Info("https://api.hyperliquid.xyz", skip_ws=True)

    # Native marks
    native_meta_ctx = info.meta_and_asset_ctxs()
    native_ctxs = {
        native_meta_ctx[0]["universe"][i]["name"]: native_meta_ctx[1][i]
        for i in range(len(native_meta_ctx[1]))
    }

    # xyz marks
    xyz_meta_ctx = info.post("/info", {"type": "metaAndAssetCtxs", "dex": "xyz"})
    xyz_ctxs = {
        xyz_meta_ctx[0]["universe"][i]["name"]: xyz_meta_ctx[1][i]
        for i in range(len(xyz_meta_ctx[1]))
    }

    print("=" * 100)
    print(f"Book B post-trade verifier  ({addr})")
    print("=" * 100)
    print(
        f"{'pos':<14} {'sz':>8} {'entry':>10} {'mark':>10} {'liqPx':>10} "
        f"{'buf%':>7} {'upnl':>10} {'marg':>9}  state"
    )
    print("-" * 100)

    # Track positions of interest
    zec_buf = None
    crwv_buf = None
    xyz100_buf = None

    # Native lane
    us = info.user_state(addr)
    for p in us.get("assetPositions", []):
        pos = p["position"]
        coin = pos["coin"]
        mark = float(native_ctxs.get(coin, {}).get("markPx", 0))
        liq = float(pos.get("liquidationPx", 0)) if pos.get("liquidationPx") else 0
        buf = _liq_buffer_pct(mark, liq)
        marg = float(pos.get("marginUsed", 0))
        szi = float(pos["szi"])
        upnl = float(pos["unrealizedPnl"])
        entry = float(pos.get("entryPx", 0))
        state = _label(buf, native_or_normal_iso=True)
        print(
            f"{coin:<14} {szi:>8.3f} {entry:>10.2f} {mark:>10.2f} {liq:>10.2f} "
            f"{(buf or 0):>6.2f}% {upnl:>+10.2f} {marg:>9.2f}  {state}"
        )
        if coin == "ZEC":
            zec_buf = buf

    # xyz HIP-3 lane
    us_xyz = info.user_state(addr, dex="xyz")
    for p in us_xyz.get("assetPositions", []):
        pos = p["position"]
        coin = pos["coin"]
        short = coin.replace("xyz:", "")
        mark = float(xyz_ctxs.get(coin, {}).get("markPx", 0))
        liq = float(pos.get("liquidationPx", 0)) if pos.get("liquidationPx") else 0
        buf = _liq_buffer_pct(mark, liq)
        marg = float(pos.get("marginUsed", 0))
        szi = float(pos["szi"])
        upnl = float(pos["unrealizedPnl"])
        entry = float(pos.get("entryPx", 0))
        is_normal_iso = short in NORMAL_XYZ_SET
        state = _label(buf, native_or_normal_iso=is_normal_iso)
        print(
            f"xyz:{short:<10} {szi:>8.3f} {entry:>10.2f} {mark:>10.2f} {liq:>10.2f} "
            f"{(buf or 0):>6.2f}% {upnl:>+10.2f} {marg:>9.2f}  {state}"
        )
        if short == "CRWV":
            crwv_buf = buf
        elif short == "XYZ100":
            xyz100_buf = buf

    # Spot
    sp = info.spot_user_state(addr)
    usdc = next((b for b in sp.get("balances", []) if b["coin"] == "USDC"), None)
    spot_total = float(usdc["total"]) if usdc else 0.0
    spot_hold = float(usdc["hold"]) if usdc else 0.0
    spot_free = spot_total - spot_hold

    print()
    print(f"spot USDC: total=${spot_total:.2f}  hold=${spot_hold:.2f}  free=${spot_free:.4f}")
    print()

    # ── Assertions ────────────────────────────────────────────────────────
    failures = []
    warnings = []

    if zec_buf is None:
        failures.append("ZEC position not found")
    elif zec_buf < args.zec_threshold:
        failures.append(
            f"ZEC buffer {zec_buf:.2f}% < {args.zec_threshold}% threshold (post-pull)"
        )

    if crwv_buf is None:
        msg = "xyz:CRWV not present (expected after step 4)"
        (failures if args.require_new_lines else warnings).append(msg)
    elif crwv_buf < args.new_line_threshold:
        failures.append(
            f"xyz:CRWV buffer {crwv_buf:.2f}% < {args.new_line_threshold}% threshold"
        )

    if xyz100_buf is None:
        msg = "xyz:XYZ100 not present (expected after step 3)"
        (failures if args.require_new_lines else warnings).append(msg)
    elif xyz100_buf < args.new_line_threshold:
        failures.append(
            f"xyz:XYZ100 buffer {xyz100_buf:.2f}% < {args.new_line_threshold}% threshold"
        )

    if spot_free < args.reserve_floor:
        failures.append(
            f"free spot ${spot_free:.4f} < ${args.reserve_floor:.2f} reserve floor"
        )

    print("ASSERTIONS")
    if not failures and not warnings:
        print("  ✅ all checks pass")
        return 0
    if warnings:
        for w in warnings:
            print(f"  ⚠️  {w}")
    if failures:
        for f in failures:
            print(f"  ❌ {f}")
        return 2
    print("  (warnings only, no failures)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
