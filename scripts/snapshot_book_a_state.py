#!/usr/bin/env python3
"""Read-only snapshot of Book A subaccount state — for pre/post diff around
unified-account enable (α plan, project_book_a_unified_enable_plan.md).

Captures spot USDC balance + clearinghouse state across native HL and every
HIP-3 builder DEX. Writes the full payload to logs/book_a_snapshots/<ts>.json
and prints a human-readable summary to stdout.

Pure read. No keys signed, no state changes, no orders placed.

Usage:
  Snapshot only:
    venv/bin/python3 scripts/snapshot_book_a_state.py

  Snapshot + diff against a prior snapshot file:
    venv/bin/python3 scripts/snapshot_book_a_state.py --diff logs/book_a_snapshots/2026-05-04T12-30-00.json

The diff mode is the load-bearing one for the α runbook: take a snapshot
before the enable, take another after, diff them. Success criterion:
native perp accountValue goes from $0 → non-zero, ideally reflecting the
$250 spot collateral.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from hyperliquid.info import Info
from hyperliquid.utils import constants


BOOKA_ADDR = "0xdae99e77b9859a1526782e3815253e8f09c1f2ef"
HIP3_DEXS = ["xyz", "flx", "vntl", "hyna", "km", "cash", "para"]

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_DIR = ROOT / "logs" / "book_a_snapshots"


def take_snapshot() -> dict:
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    snap: dict = {
        "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "address": BOOKA_ADDR,
        "spot": None,
        "clearinghouses": {},
        "abstraction": {},
    }

    # Spot balances
    spot = info.spot_user_state(BOOKA_ADDR)
    snap["spot"] = {
        "balances": [
            {"coin": b["coin"], "total": b["total"], "hold": b.get("hold", "0")}
            for b in spot.get("balances", [])
            if float(b.get("total", 0)) > 0
        ],
    }

    # Native + each HIP-3 clearinghouse
    for dex in [None] + HIP3_DEXS:
        label = dex or "native"
        try:
            if dex:
                cs = info.post(
                    "/info", {"type": "clearinghouseState", "user": BOOKA_ADDR, "dex": dex}
                )
            else:
                cs = info.user_state(BOOKA_ADDR)
            ms = cs.get("marginSummary", {}) or {}
            positions = []
            for p in cs.get("assetPositions", []):
                pos = p.get("position", {})
                positions.append({
                    "coin": pos.get("coin"),
                    "szi": pos.get("szi"),
                    "entryPx": pos.get("entryPx"),
                    "unrealizedPnl": pos.get("unrealizedPnl"),
                })
            snap["clearinghouses"][label] = {
                "accountValue": float(ms.get("accountValue", 0) or 0),
                "totalNtlPos": float(ms.get("totalNtlPos", 0) or 0),
                "totalRawUsd": float(ms.get("totalRawUsd", 0) or 0),
                "totalMarginUsed": float(ms.get("totalMarginUsed", 0) or 0),
                "withdrawable": float(cs.get("withdrawable", 0) or 0),
                "n_positions": len(positions),
                "positions": positions,
            }
        except Exception as e:
            snap["clearinghouses"][label] = {"error": str(e)[:200]}

    # Abstraction state — the load-bearing field for unified-account check
    try:
        snap["abstraction"]["mode"] = info.query_user_abstraction_state(BOOKA_ADDR)
    except Exception as e:
        snap["abstraction"]["mode_error"] = str(e)[:200]
    try:
        snap["abstraction"]["dex_abstraction"] = info.query_user_dex_abstraction_state(BOOKA_ADDR)
    except Exception as e:
        snap["abstraction"]["dex_abstraction_error"] = str(e)[:200]

    return snap


def print_summary(snap: dict, label: str = "snapshot") -> None:
    print("=" * 72)
    print(f"BOOK A {label}: {snap['address']}")
    print(f"  taken_at_utc: {snap['timestamp_utc']}")
    print("=" * 72)
    print("\nSpot balances:")
    for b in snap["spot"]["balances"]:
        print(f"  {b['coin']}: total={b['total']} hold={b['hold']}")
    print("\nAbstraction:")
    for k, v in snap["abstraction"].items():
        print(f"  {k}: {v}")
    print("\nClearinghouses (accountValue / withdrawable / positions):")
    for ch, data in snap["clearinghouses"].items():
        if "error" in data:
            print(f"  {ch:7s}: ERR {data['error']}")
        else:
            print(
                f"  {ch:7s}: ${data['accountValue']:>9.4f} / "
                f"${data['withdrawable']:>9.4f} / {data['n_positions']} pos"
            )
    sum_av = sum(
        d.get("accountValue", 0) for d in snap["clearinghouses"].values() if "error" not in d
    )
    print(f"\nSum perp accountValue across all CHs: ${sum_av:.4f}")


def diff_summary(prior: dict, current: dict) -> None:
    print("=" * 72)
    print("DIFF (current minus prior)")
    print("=" * 72)
    print(f"  prior   taken: {prior['timestamp_utc']}")
    print(f"  current taken: {current['timestamp_utc']}")

    # Abstraction diff
    print("\nAbstraction changes:")
    for k in ("mode", "dex_abstraction"):
        p = prior["abstraction"].get(k)
        c = current["abstraction"].get(k)
        marker = "  " if p == c else "→ "
        print(f"  {marker}{k}: {p}  →  {c}")

    # Clearinghouse accountValue diff
    print("\nClearinghouse accountValue changes:")
    all_ch = set(prior["clearinghouses"].keys()) | set(current["clearinghouses"].keys())
    for ch in sorted(all_ch):
        p_data = prior["clearinghouses"].get(ch, {})
        c_data = current["clearinghouses"].get(ch, {})
        p_av = p_data.get("accountValue", 0)
        c_av = c_data.get("accountValue", 0)
        delta = c_av - p_av
        marker = "  " if abs(delta) < 0.01 else "→ "
        print(f"  {marker}{ch:7s}: ${p_av:>9.4f}  →  ${c_av:>9.4f}  (Δ ${delta:+.4f})")

    # Success-condition evaluation for α runbook step 3
    print("\nα runbook Step 3 success condition check:")
    p_native = prior["clearinghouses"].get("native", {}).get("accountValue", 0)
    c_native = current["clearinghouses"].get("native", {}).get("accountValue", 0)
    if c_native > 0 and p_native == 0:
        print(f"  ✓ PASS: native perp accountValue ${p_native:.2f} → ${c_native:.2f}")
        print("          (unified-account enable appears to have taken effect)")
    elif c_native == 0 and p_native == 0:
        print(f"  ✗ FAIL: native perp accountValue still ${c_native:.2f}")
        print("          (enable did not change funding state — STOP, do not relaunch)")
    else:
        print(f"  ?  AMBIGUOUS: native ${p_native:.2f} → ${c_native:.2f}")
        print("          (manual review needed)")

    # HIP-3 status note
    hip3_changed = []
    for ch in HIP3_DEXS:
        p = prior["clearinghouses"].get(ch, {}).get("accountValue", 0)
        c = current["clearinghouses"].get(ch, {}).get("accountValue", 0)
        if c > p:
            hip3_changed.append(f"{ch}: ${p:.2f} → ${c:.2f}")
    if hip3_changed:
        print(f"  HIP-3 clearinghouses also changed: {', '.join(hip3_changed)}")
        print("  (HIP-3 may have inherited unified status — investigate further)")
    else:
        print("  HIP-3 clearinghouses unchanged (still need separate handling — expected)")


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--diff", type=str, default=None,
                   help="path to a prior snapshot JSON file to diff against")
    p.add_argument("--no-write", action="store_true",
                   help="print only, do not write the snapshot to disk")
    args = p.parse_args()

    snap = take_snapshot()
    print_summary(snap, label="snapshot")

    if not args.no_write:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        ts_safe = snap["timestamp_utc"].replace(":", "-").replace(".", "-")
        out = SNAPSHOT_DIR / f"{ts_safe}.json"
        out.write_text(json.dumps(snap, indent=2))
        print(f"\nWritten: {out}")

    if args.diff:
        prior_path = Path(args.diff)
        if not prior_path.exists():
            print(f"\nERROR: --diff path not found: {prior_path}", file=sys.stderr)
            return 2
        prior = json.loads(prior_path.read_text())
        print()
        diff_summary(prior, snap)

    return 0


if __name__ == "__main__":
    sys.exit(main())
