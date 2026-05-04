#!/usr/bin/env python3
"""Tranche 1 of staggered ZEC discretionary add — Book B only.

Plan (from copilot session, 2026-05-03):
  Total add target: 7 ZEC
  Tranche 1 (this script): 3 ZEC working bid in the 409.20-409.80 retest zone
  Tranche 2 (NOT in this script): 4 ZEC only on confirmed 413+ acceptance
                                  (15-30 min hold with pullbacks above 412)

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
LIMIT_PX_DEFAULT = 409.40   # middle of 409.20-409.80 zone
TIF = "Alo"                 # post-only — refuses if it would cross
REDUCE_ONLY = False         # this is an ADD, not a close
LEVERAGE = 10               # match place_zec_entry.py convention; isolated

# Add-only invalidation per the plan — NOT used as an attached stop here
# (Alo doesn't carry a stop), but recorded in the sidecar so the audit
# trail captures intended invalidation.
ADD_ONLY_STOP_15M_CLOSE_BELOW = 407.50
ADD_ONLY_TACTICAL_ALERT = 406.50


def render_dry_run(cloid: str, args: argparse.Namespace) -> None:
    intent = {
        "symbol": COIN,
        "side": "buy",
        "qty": QTY,
        "limit_px": args.limit_px,
        "tif": TIF,
        "reduce_only": REDUCE_ONLY,
        "cloid": cloid,
    }
    sidecar = {
        "cloid": cloid,
        "symbol": COIN,
        "side": "buy",
        "size": QTY,
        "intent": "discretionary_add_book_b",
        "script": "place_zec_add_409.py",
        "reason": (
            "tranche_1_of_7_staggered_add; entry zone 409.20-409.80; "
            "add-only invalidation 15m close <407.50; alert 406.50; "
            "first trim 415.5-416; second target 418.5-420 only on 416 hold"
        ),
        "tranche": "1_of_2",
        "total_target_qty": 7.0,
        "tranche_2_trigger": "413+ accepted hold for 15-30 min, OBI flips positive",
        "core_position_unaffected": "18 ZEC long from ~388.92, stops 389/371",
        "book": "B (master account)",
    }
    print("=" * 72)
    print("DRY RUN — no order placed")
    print("=" * 72)
    print(f"Order intent (passed to mgr.submit_order):")
    print(json.dumps(intent, indent=2))
    print(f"\nSidecar entry (logs/manual_orders.jsonl):")
    print(json.dumps(sidecar, indent=2))
    print(f"\nLeverage set ahead of order: {LEVERAGE}x isolated on {COIN}")
    print(f"\nTo execute for real, re-run with:")
    print(f"  --execute --i-confirm-book-b-add")
    print(f"\nOptional flag overrides:")
    print(f"  --limit-px <PX>   (default {LIMIT_PX_DEFAULT}, valid range 409.20-409.80)")
    print(f"  --qty <Q>         (default {QTY})")
    print()
    print("After fill: monitor for tranche 2 trigger (413+ hold ≥15 min, OBI positive)")
    print("If 408 lost cleanly: cancel any unfilled remainder + exit filled portion")


async def execute(cloid: str, args: argparse.Namespace) -> int:
    cfg = load_settings()
    mgr = HyperliquidOrderManager(
        cfg,
        strategy_tag="manual_zec_add",
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
        intent="discretionary_add_book_b",
        script="place_zec_add_409.py",
        reason=(
            "tranche_1_of_7_staggered_add; entry zone 409.20-409.80; "
            "add-only invalidation 15m close <407.50; alert 406.50"
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
                   help=f"limit price (default {LIMIT_PX_DEFAULT}; valid 409.20-409.80)")
    p.add_argument("--qty", type=float, default=QTY,
                   help=f"order qty in ZEC (default {QTY})")
    args = p.parse_args()

    if args.limit_px < 409.20 or args.limit_px > 409.80:
        print(f"ERROR: --limit-px {args.limit_px} outside the planned 409.20-409.80 retest zone. "
              f"Refusing — order intent is 'support retest', not 'somewhere near 409'.",
              file=sys.stderr)
        return 2
    if args.qty <= 0 or args.qty > 5.0:
        print(f"ERROR: --qty {args.qty} outside sane range (0, 5.0]. Refusing.", file=sys.stderr)
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
