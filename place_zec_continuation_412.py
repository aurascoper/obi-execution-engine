#!/usr/bin/env python3
"""Continuation tranche of staggered ZEC discretionary add — Book B only.

Plan (from copilot session, 2026-05-04):
  Total add target (continuation thesis): 7 ZEC across two tranches
  Tranche 1 (this script): 3 ZEC working bid in the 412.20-412.80 pullback zone
                           — fires ONLY after ACCEPTANCE_READY trigger (mid >=413
                             held >=15min, no break <412, OBI nonnegative)
  Tranche 2 (NOT in this script): 4 ZEC only on confirmed 416 break-and-hold
                                  — fresh discretionary decision, not a follow-on

This script is the continuation analogue to place_zec_add_409.py (which
covers the support-retest case). They are mutually exclusive in spirit:
  - 409 retest path → use place_zec_add_409.py
  - 413 acceptance path → use this script
The watcher (scripts/monitor_zec_entry_triggers.py) tells you which one
the tape has chosen.

Pre-condition before placing: ACCEPTANCE_READY must have fired in
logs/zec_entry_alerts.jsonl. Do NOT place this in the absence of that
trigger; it would just be a chase entry into supply.

NOT engine-book. NOT Gate 3/4 evidence. Lives in master/Book B.

Cloid uses the manual-order tagging convention (0xdead0001 prefix) and
appends a sidecar record to logs/manual_orders.jsonl per
scripts/lib/manual_order.py.

Defaults to --dry-run. Execute requires --execute --i-confirm-book-b-add.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from config.settings import load as load_settings
from execution.hl_manager import HyperliquidOrderManager
from scripts.lib.manual_order import make_manual_cloid, log_manual_order


COIN = "ZEC"
QTY = 3.0
LIMIT_PX_DEFAULT = 412.50   # middle of 412.20-412.80 pullback zone
TIF = "Alo"                 # post-only — refuses if it would cross
REDUCE_ONLY = False         # this is an ADD, not a close
LEVERAGE = 10               # match place_zec_entry.py convention; isolated

# Add-only invalidation per the continuation plan — NOT used as an
# attached stop here (Alo doesn't carry one), but recorded in the
# sidecar so the audit trail captures intended invalidation.
# Tighter than the 409-retest invalidation because the entry is higher
# and the thesis is "413 is accepted support" — losing 410.5 invalidates.
ADD_ONLY_STOP_15M_CLOSE_BELOW = 410.50
ADD_ONLY_TACTICAL_ALERT = 410.00


def render_dry_run(cloid: str, args: argparse.Namespace) -> None:
    intent = {
        "symbol": COIN,
        "side": "buy",
        "qty": args.qty,
        "limit_px": args.limit_px,
        "tif": TIF,
        "reduce_only": REDUCE_ONLY,
        "cloid": cloid,
    }
    sidecar = {
        "cloid": cloid,
        "symbol": COIN,
        "side": "buy",
        "size": args.qty,
        "intent": "discretionary_continuation_add_book_b",
        "script": "place_zec_continuation_412.py",
        "reason": (
            "tranche_1_of_7_continuation_thesis; entry zone 412.20-412.80 on "
            "pullback after 413 acceptance; add-only invalidation 15m close "
            "<410.50; tactical alert 410.00; first trim 415.5-416; tranche 2 "
            "(4 ZEC) deliberately NOT scripted — fires only on 416 break+hold "
            "as a fresh discretionary decision"
        ),
        "tranche": "1_of_2_continuation",
        "total_target_qty": 7.0,
        "tranche_2_trigger": "416 breaks AND holds (fresh decision, not scripted)",
        "pre_condition": (
            "ACCEPTANCE_READY trigger must have fired in "
            "logs/zec_entry_alerts.jsonl — do not place absent that signal"
        ),
        "core_position_unaffected": "18 ZEC long from ~388.92, stops 389/371",
        "book": "B (master account)",
        "thesis": "413 acceptance / continuation, NOT 409 support retest",
        "do_not_chase": "415.5-416 is visible supply zone; entry at 412.x only",
    }
    print("=" * 72)
    print("DRY RUN — no order placed")
    print("=" * 72)
    print("Order intent (passed to mgr.submit_order):")
    print(json.dumps(intent, indent=2))
    print("\nSidecar entry (logs/manual_orders.jsonl):")
    print(json.dumps(sidecar, indent=2))
    print(f"\nLeverage set ahead of order: {LEVERAGE}x isolated on {COIN}")
    print("\nTo execute for real, re-run with:")
    print("  --execute --i-confirm-book-b-add")
    print("\nOptional flag overrides:")
    print(f"  --limit-px <PX>   (default {LIMIT_PX_DEFAULT}, valid range 412.20-412.80)")
    print(f"  --qty <Q>         (default {QTY})")
    print()
    print("Pre-condition reminder: ACCEPTANCE_READY trigger should have fired.")
    print("If you don't see it in logs/zec_entry_alerts.jsonl, this is a chase entry.")
    print()
    print("After fill: watch for either 415.5-416 trim zone or 410.50 invalidation")
    print("Tranche 2 (4 ZEC) is NOT scripted — fires only on 416 break+hold")


async def execute(cloid: str, args: argparse.Namespace) -> int:
    cfg = load_settings()
    mgr = HyperliquidOrderManager(
        cfg,
        strategy_tag="manual_zec_cont",
        default_leverage=LEVERAGE,
        coins=[COIN],
        is_cross=False,
    )

    lev_resp = await asyncio.to_thread(
        mgr._exchange.update_leverage, LEVERAGE, COIN, False
    )
    print(f"leverage set: {lev_resp}")

    log_manual_order(
        cloid=cloid,
        symbol=COIN,
        side="buy",
        size=args.qty,
        intent="discretionary_continuation_add_book_b",
        script="place_zec_continuation_412.py",
        reason=(
            "tranche_1_of_7_continuation_thesis; entry zone 412.20-412.80 "
            "on pullback after 413 acceptance; add-only invalidation 15m "
            "close <410.50; alert 410.00"
        ),
    )

    intent = {
        "symbol": COIN,
        "side": "buy",
        "qty": args.qty,
        "limit_px": args.limit_px,
        "tif": TIF,
        "reduce_only": REDUCE_ONLY,
        "cloid": cloid,
    }
    print(f"submitting: {json.dumps(intent)}")
    resp = await mgr.submit_order(intent)
    print(f"submit resp: {resp}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--execute", action="store_true",
                   help="actually place the order (default: dry-run)")
    p.add_argument("--i-confirm-book-b-add", action="store_true",
                   dest="i_confirm",
                   help="required confirmation flag for --execute")
    p.add_argument("--limit-px", type=float, default=LIMIT_PX_DEFAULT,
                   help=f"limit price (default {LIMIT_PX_DEFAULT}; valid 412.20-412.80)")
    p.add_argument("--qty", type=float, default=QTY,
                   help=f"order qty in ZEC (default {QTY})")
    args = p.parse_args()

    if args.limit_px < 412.20 or args.limit_px > 412.80:
        print(f"ERROR: --limit-px {args.limit_px} outside the planned 412.20-412.80 "
              f"pullback zone. Refusing — order intent is 'pullback after 413 "
              f"acceptance', not 'somewhere near 412'.", file=sys.stderr)
        return 2
    if args.qty <= 0 or args.qty > 5.0:
        print(f"ERROR: --qty {args.qty} outside sane range (0, 5.0]. Refusing.",
              file=sys.stderr)
        return 2

    cloid = make_manual_cloid()

    if not args.execute:
        render_dry_run(cloid, args)
        return 0

    if not args.i_confirm:
        print("ERROR: --execute requires --i-confirm-book-b-add", file=sys.stderr)
        print("       (refusing to place real order without explicit confirm)",
              file=sys.stderr)
        return 2

    return asyncio.run(execute(cloid, args))


if __name__ == "__main__":
    sys.exit(main())
