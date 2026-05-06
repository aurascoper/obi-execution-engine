#!/usr/bin/env python3
"""Open xyz:XYZ100 SHORT — Book B hedge leg of CRWV+XYZ100 layering.

Plan (plans/elegant-churning-tide.md):
  Target: 0.0205 contracts short @ ~10x isolated, ~$578 notional, ~$58 margin
  Tif:    Alo (post-only) — refuses if it would cross
  Price:  join the best ask (round to tick) — guaranteed non-crossing
  Mode:   isolated (operator confirmed via live UI 2026-05-06)
  Order:  PLACE FIRST, before CRWV long — if CRWV step fails, hedge is in place

NOT engine-book. Lives in master/Book B. cloid uses the manual-order tagging
convention (0xdead0001 prefix) and appends a sidecar record to
logs/manual_orders.jsonl per scripts/lib/manual_order.py.

Defaults to --dry-run. Execute requires --execute --i-confirm-book-b.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys

from config.settings import load as load_settings
from execution.hl_manager import HyperliquidOrderManager
from scripts.lib.manual_order import make_manual_cloid, log_manual_order


COIN = "xyz:XYZ100"
QTY_DEFAULT = 0.0205      # plan target; ~$578 notional / ~$58 margin at $28,222 mark
TIF = "Alo"               # post-only — refuses if it would cross
REDUCE_ONLY = False       # this is an open, not a close
LEVERAGE = 10             # isolated, per plan; operator confirmed XYZ100 in iso mode

# Sanity bounds — refuse silly args
QTY_MIN = 0.0010
QTY_MAX = 0.0500


async def _fetch_market(mgr: HyperliquidOrderManager) -> tuple[float, float, float, int]:
    """Return (best_bid, best_ask, mid, sz_decimals) for COIN on xyz dex."""
    meta = await asyncio.to_thread(mgr._info.meta, dex="xyz")
    sz_dec = 4
    for u in meta.get("universe", []):
        if u.get("name") == COIN:
            sz_dec = u.get("szDecimals", 4)
            break

    l2 = await asyncio.to_thread(mgr._info.l2_snapshot, COIN)
    lv = l2.get("levels") or []
    if len(lv) < 2 or not lv[0] or not lv[1]:
        raise RuntimeError(f"[{COIN}] empty book")
    best_bid = float(lv[0][0]["px"])
    best_ask = float(lv[1][0]["px"])
    mid = (best_bid + best_ask) / 2.0
    return best_bid, best_ask, mid, sz_dec


def _build_intent(qty: float, limit_px: float, cloid: str) -> dict:
    return {
        "symbol": COIN,
        "side": "sell",
        "qty": qty,
        "limit_px": limit_px,
        "tif": TIF,
        "reduce_only": REDUCE_ONLY,
        "cloid": cloid,
    }


def _build_sidecar_meta(qty: float, limit_px: float) -> dict:
    return {
        "intent": "discretionary_hedge_book_b",
        "script": "open_xyz100_short.py",
        "reason": (
            "broad-tech hedge leg for CRWV earnings event flyer; "
            "10x isolated; operator-confirmed XYZ100 in iso mode 2026-05-06"
        ),
        "leg_of": "crwv_xyz100_layering",
        "leg_role": "primary_hedge",
        "leg_order": "1_of_2",
        "next_step": "open_crwv_long.py (event flyer)",
        "target_notional": qty * limit_px,
        "leverage": LEVERAGE,
        "margin_mode": "isolated",
        "book": "B (master account)",
    }


async def _execute(args, cloid: str) -> int:
    cfg = load_settings()
    mgr = HyperliquidOrderManager(
        cfg,
        strategy_tag="manual_xyz100_hedge",
        default_leverage=LEVERAGE,
        coins=[COIN],
        is_cross=False,
        perp_dexs=["xyz"],
    )

    best_bid, best_ask, mid, sz_dec = await _fetch_market(mgr)

    # Pre-flight: free margin on xyz lane
    xyz_state = await asyncio.to_thread(mgr._info.user_state, mgr._wallet_address, "xyz")
    ms = xyz_state.get("marginSummary", {})
    nav = float(ms.get("accountValue", 0))
    used = float(ms.get("totalMarginUsed", 0))
    free = nav - used
    target_margin = args.qty * mid / LEVERAGE
    print(f"[xyz lane] NAV=${nav:.2f}  used=${used:.2f}  free=${free:.2f}")
    print(f"           target margin for this order ≈ ${target_margin:.2f}")
    if free < target_margin + 2:
        print(
            f"[{COIN}] insufficient xyz free margin (${free:.2f} < ${target_margin + 2:.2f}) — aborting",
            file=sys.stderr,
        )
        return 1

    # Maker placement: join best ask, rounded to a sensible tick
    tick = round(best_ask - best_bid, 6) or 0.01
    limit_px = round(round(best_ask / tick) * tick, 6)
    if limit_px < best_ask:
        limit_px = best_ask  # never go below ask on a sell Alo (would cross)

    # Round qty to lot floor
    factor = 10**sz_dec
    qty = math.floor(args.qty * factor) / factor
    if qty <= 0:
        print(f"[{COIN}] qty rounded to 0 — aborting", file=sys.stderr)
        return 1

    notional = qty * limit_px
    margin = notional / LEVERAGE
    print(
        f"[{COIN}] bid={best_bid}  ask={best_ask}  mid={mid:.4f}  -> "
        f"SELL Alo qty={qty} @ {limit_px}  (notional≈${notional:.2f}, margin≈${margin:.2f})"
    )

    # Set leverage to 10x isolated for this coin
    lev_resp = await asyncio.to_thread(
        mgr._exchange.update_leverage, LEVERAGE, COIN, False
    )
    print(f"leverage set: {lev_resp}")

    # Sidecar log
    log_manual_order(
        cloid=cloid,
        symbol=COIN,
        side="sell",
        size=qty,
        **_build_sidecar_meta(qty, limit_px),
    )

    intent = _build_intent(qty, limit_px, cloid)
    print(f"submitting: {json.dumps(intent)}")
    resp = await mgr.submit_order(intent)
    print(f"submit resp: {resp}")
    return 0


def _render_dry_run(args, cloid: str) -> int:
    """Render dry-run plan WITHOUT touching the network."""
    intent = _build_intent(args.qty, "<resolved at execute>", cloid)
    sidecar = {
        "cloid": cloid,
        "symbol": COIN,
        "side": "sell",
        "size": args.qty,
        **_build_sidecar_meta(args.qty, 0.0),  # notional shown as zero in dry-run preview
    }
    print("=" * 72)
    print(f"DRY RUN — no order placed for {COIN}")
    print("=" * 72)
    print(f"qty           : {args.qty} contracts (plan target {QTY_DEFAULT})")
    print(f"side          : SELL (open short)")
    print(f"tif           : {TIF} (post-only, joins best ask, never crosses)")
    print(f"leverage      : {LEVERAGE}x ISOLATED (per operator UI confirmation)")
    print(f"reduce_only   : {REDUCE_ONLY}")
    print()
    print(f"Order intent (passed to mgr.submit_order at execute time):")
    print(json.dumps(intent, indent=2))
    print()
    print(f"Sidecar entry (logs/manual_orders.jsonl on execute):")
    print(json.dumps(sidecar, indent=2, default=str))
    print()
    print(f"To execute for real, re-run with:")
    print(f"  --execute --i-confirm-book-b")
    print()
    print(f"Plan position in sequence: PRIMARY HEDGE LEG, place FIRST")
    print(f"  (open_crwv_long.py is the second leg, place AFTER this fills or proves stuck)")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--execute",
        action="store_true",
        help="actually place the order (default: dry-run)",
    )
    p.add_argument(
        "--i-confirm-book-b",
        action="store_true",
        dest="i_confirm",
        help="required confirmation flag for --execute",
    )
    p.add_argument(
        "--qty",
        type=float,
        default=QTY_DEFAULT,
        help=f"order qty in XYZ100 contracts (default {QTY_DEFAULT}; sane range [{QTY_MIN}, {QTY_MAX}])",
    )
    args = p.parse_args()

    if args.qty < QTY_MIN or args.qty > QTY_MAX:
        print(
            f"ERROR: --qty {args.qty} outside sane range [{QTY_MIN}, {QTY_MAX}]. Refusing.",
            file=sys.stderr,
        )
        return 2

    cloid = make_manual_cloid()

    if not args.execute:
        return _render_dry_run(args, cloid)

    if not args.i_confirm:
        print(
            "ERROR: --execute requires --i-confirm-book-b\n"
            "       (refusing to place a real order without explicit confirm)",
            file=sys.stderr,
        )
        return 2

    return asyncio.run(_execute(args, cloid))


if __name__ == "__main__":
    sys.exit(main())
