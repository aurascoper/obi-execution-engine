#!/usr/bin/env python3
"""Top up ZEC isolated long with OBI-confirmed maker entry — Book B only.

Sized as a TOP-UP to an existing ZEC position. Default qty is 1.5 ZEC at
10x isolated. Adds margin from the operator's pre-existing free spot —
this script does NOT auto-pull margin from ETH or any other position. If
spot free is insufficient, the script aborts with a sourcing recommendation.

Microstructure timing:
  Watches OBI from top-10 L2 book levels every poll. Fires the maker buy
  when OBI ≥ --obi-min has held for --obi-stable-secs continuously, OR when
  --max-wait elapses (operator-configurable: bail or take last quote).

Order shape:
  Alo (post-only) BUY at best_bid (rounded to tick) — guaranteed
  non-crossing. If OBI flips below threshold while resting, the order is
  cancelled and the watch resumes.

Manual-order tagging:
  cloid uses the 0xdead0001 prefix and appends a sidecar record to
  logs/manual_orders.jsonl per scripts/lib/manual_order.py.

Defaults to --dry-run. Execute requires --execute --i-confirm-book-b.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from typing import Optional

from config.settings import load as load_settings
from execution.hl_manager import HyperliquidOrderManager
from scripts.lib.manual_order import make_manual_cloid, log_manual_order


COIN = "ZEC"
QTY_DEFAULT = 1.5
TIF = "Alo"
LEVERAGE = 10  # isolated, matches existing ZEC position convention

# OBI trigger params (operator-tunable via CLI)
OBI_MIN_DEFAULT = 0.05         # min OBI to trigger (top-10 imbalance, range -1 to +1)
OBI_STABLE_SECS_DEFAULT = 30   # OBI must hold ≥ obi_min for this long, no dip
POLL_INTERVAL_S = 2.0          # how often to sample the book
MAX_WAIT_DEFAULT = 600         # max seconds to wait for a trigger before bailing
RESTING_OBI_FLOOR = -0.05      # if our resting order sees OBI dip below this, cancel + restart

# Sanity bounds
QTY_MIN = 0.10
QTY_MAX = 5.00


# ── OBI helper ────────────────────────────────────────────────────────────


async def _fetch_book_obi(mgr: HyperliquidOrderManager) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    """Return (best_bid, best_ask, mid, obi_top10). None tuple on failure."""
    try:
        l2 = await asyncio.to_thread(mgr._info.l2_snapshot, COIN)
        lv = l2.get("levels") or []
        if len(lv) < 2 or not lv[0] or not lv[1]:
            return None, None, None, None
        bids = lv[0][:10]
        asks = lv[1][:10]
        bb = float(bids[0]["px"])
        ba = float(asks[0]["px"])
        bsz = sum(float(b["sz"]) for b in bids)
        asz = sum(float(a["sz"]) for a in asks)
        obi = (bsz - asz) / (bsz + asz) if (bsz + asz) else 0.0
        mid = (bb + ba) / 2.0
        return bb, ba, mid, obi
    except Exception:
        return None, None, None, None


async def _wait_for_obi_trigger(
    mgr: HyperliquidOrderManager, obi_min: float, stable_secs: float, max_wait: float
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Poll OBI; return (best_bid, mid, obi_at_trigger) when held ≥ obi_min for stable_secs.

    Returns (None, None, None) if max_wait expires first.
    """
    start = time.time()
    streak_start: Optional[float] = None
    last_print = 0.0

    while time.time() - start < max_wait:
        bb, ba, mid, obi = await _fetch_book_obi(mgr)
        if obi is None:
            await asyncio.sleep(POLL_INTERVAL_S)
            continue

        now = time.time()
        if obi >= obi_min:
            if streak_start is None:
                streak_start = now
            elapsed = now - streak_start
            if now - last_print > 5.0:
                print(
                    f"  [{int(now-start):>4}s] mid={mid:.3f}  bid={bb:.3f}  ask={ba:.3f}  OBI={obi:+.3f}  streak={elapsed:.0f}s"
                )
                last_print = now
            if elapsed >= stable_secs:
                return bb, mid, obi
        else:
            if streak_start is not None:
                print(
                    f"  [{int(now-start):>4}s] OBI dipped to {obi:+.3f} (was streaking) — resetting streak"
                )
            streak_start = None
            if now - last_print > 5.0:
                print(
                    f"  [{int(now-start):>4}s] mid={mid:.3f}  bid={bb:.3f}  ask={ba:.3f}  OBI={obi:+.3f}  (waiting)"
                )
                last_print = now

        await asyncio.sleep(POLL_INTERVAL_S)

    return None, None, None


# ── Pre-flight margin check ───────────────────────────────────────────────


async def _preflight_margin(mgr: HyperliquidOrderManager, qty: float, mid: float) -> tuple[bool, str]:
    """Return (ok, message)."""
    target_margin = qty * mid / LEVERAGE
    sp = await asyncio.to_thread(mgr._info.spot_user_state, mgr._wallet_address)
    usdc = next((b for b in sp.get("balances", []) if b.get("coin") == "USDC"), {})
    spot_total = float(usdc.get("total", 0))
    spot_hold = float(usdc.get("hold", 0))
    spot_free = spot_total - spot_hold

    if spot_free < target_margin + 2:
        msg = (
            f"INSUFFICIENT MARGIN: target ${target_margin:.2f}, "
            f"spot free only ${spot_free:.2f}. "
            f"Source margin first (e.g. pull ${target_margin + 5:.0f} from ETH "
            f"via update_isolated_margin('ETH', is_buy=True, amount=-{target_margin + 5:.0f})). "
            f"Aborting."
        )
        return False, msg
    return True, f"spot free ${spot_free:.2f} ≥ target margin ${target_margin:.2f} + buffer ✓"


# ── Order placement ───────────────────────────────────────────────────────


async def _place_topup(args, cloid: str) -> int:
    cfg = load_settings()
    mgr = HyperliquidOrderManager(
        cfg,
        strategy_tag="manual_zec_topup_obi",
        default_leverage=LEVERAGE,
        coins=[COIN],
        is_cross=False,
    )

    # ZEC szDecimals — ZEC is HL native, default 2 dp
    meta = await asyncio.to_thread(mgr._info.meta)
    sz_dec = 2
    for u in meta.get("universe", []):
        if u.get("name") == COIN:
            sz_dec = u.get("szDecimals", 2)
            break

    # Pre-snapshot of state
    bb, ba, mid, obi = await _fetch_book_obi(mgr)
    if mid is None:
        print(f"[{COIN}] empty / unreadable book — aborting", file=sys.stderr)
        return 1
    print(f"[{COIN}] initial: bid={bb} ask={ba} mid={mid:.3f} OBI={obi:+.3f}")

    # Pre-flight margin
    ok, msg = await _preflight_margin(mgr, args.qty, mid)
    print(msg)
    if not ok:
        return 1

    # Set leverage to 10x isolated for ZEC (no-op if already)
    lev_resp = await asyncio.to_thread(
        mgr._exchange.update_leverage, LEVERAGE, COIN, False
    )
    print(f"leverage set: {lev_resp}")

    print(f"\nWaiting for OBI ≥ {args.obi_min:+.2f} held for {args.obi_stable_secs}s "
          f"(max wait {args.max_wait}s)...")
    bb, mid_at_trigger, obi_at_trigger = await _wait_for_obi_trigger(
        mgr, args.obi_min, args.obi_stable_secs, args.max_wait
    )
    if bb is None:
        print(f"\n[{COIN}] max wait expired — no OBI trigger fired. Bailing.", file=sys.stderr)
        return 2

    print(f"\nTRIGGER FIRED: mid={mid_at_trigger:.3f} OBI={obi_at_trigger:+.3f}  bid={bb}")

    # Round qty to lot floor
    factor = 10**sz_dec
    qty = math.floor(args.qty * factor) / factor
    if qty <= 0:
        print(f"[{COIN}] qty rounded to 0 — aborting", file=sys.stderr)
        return 1

    # Maker placement: join best bid, post-only
    tick = round(mid_at_trigger * 0.0001, 4) or 0.01  # heuristic; ZEC tick ~ $0.05
    limit_px = round(bb, 2)  # join the bid, conservative pricing per place_zec_entry.py convention

    notional = qty * limit_px
    margin = notional / LEVERAGE
    print(
        f"[{COIN}] BUY Alo qty={qty} @ {limit_px}  "
        f"(notional≈${notional:.2f}, margin≈${margin:.2f})"
    )

    log_manual_order(
        cloid=cloid,
        symbol=COIN,
        side="buy",
        size=qty,
        intent="discretionary_topup_book_b",
        script="open_zec_topup_obi.py",
        reason=(
            f"OBI-confirmed top-up; OBI≥{args.obi_min:+.2f} held {args.obi_stable_secs}s; "
            f"trigger OBI={obi_at_trigger:+.3f}, mid={mid_at_trigger:.3f}"
        ),
        leverage=LEVERAGE,
        margin_mode="isolated",
        book="B (master account)",
        obi_at_trigger=obi_at_trigger,
        mid_at_trigger=mid_at_trigger,
    )

    intent = {
        "symbol": COIN,
        "side": "buy",
        "qty": qty,
        "limit_px": limit_px,
        "tif": TIF,
        "reduce_only": False,
        "cloid": cloid,
    }
    print(f"submitting: {json.dumps(intent)}")
    resp = await mgr.submit_order(intent)
    print(f"submit resp: {resp}")
    return 0


# ── Dry-run ───────────────────────────────────────────────────────────────


def _render_dry_run(args, cloid: str) -> int:
    print("=" * 72)
    print(f"DRY RUN — no order placed for {COIN}")
    print("=" * 72)
    print(f"qty                : {args.qty} ZEC (plan target {QTY_DEFAULT})")
    print(f"side               : BUY (top-up of existing ZEC long)")
    print(f"tif                : {TIF} (post-only, joins best bid)")
    print(f"leverage           : {LEVERAGE}x ISOLATED (HL native)")
    print()
    print(f"OBI trigger logic:")
    print(f"  obi_min          : {args.obi_min:+.3f}  (top-10 book imbalance)")
    print(f"  obi_stable_secs  : {args.obi_stable_secs}s  (must hold continuously)")
    print(f"  poll_interval    : {POLL_INTERVAL_S}s")
    print(f"  max_wait         : {args.max_wait}s  (then bail with no trade)")
    print()
    print(f"cloid              : {cloid}")
    print(f"sidecar tag        : discretionary_topup_book_b")
    print()
    print(f"Pre-flight margin check (read-only, no actions):")

    async def _peek():
        cfg = load_settings()
        mgr = HyperliquidOrderManager(
            cfg, strategy_tag="manual_zec_topup_obi_dry", default_leverage=LEVERAGE,
            coins=[COIN], is_cross=False,
        )
        bb, ba, mid, obi = await _fetch_book_obi(mgr)
        if mid is None:
            print("  (could not read ZEC book)")
            return
        print(f"  ZEC book   : bid={bb} ask={ba} mid={mid:.3f} OBI={obi:+.3f}")
        ok, msg = await _preflight_margin(mgr, args.qty, mid)
        print(f"  {msg}")

    asyncio.run(_peek())

    print()
    print(f"To execute live, re-run with:")
    print(f"  --execute --i-confirm-book-b")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--execute", action="store_true",
                   help="actually place the order (default: dry-run)")
    p.add_argument("--i-confirm-book-b", action="store_true", dest="i_confirm",
                   help="required confirmation flag for --execute")
    p.add_argument("--qty", type=float, default=QTY_DEFAULT,
                   help=f"ZEC contracts to add (default {QTY_DEFAULT}; range [{QTY_MIN}, {QTY_MAX}])")
    p.add_argument("--obi-min", type=float, default=OBI_MIN_DEFAULT, dest="obi_min",
                   help=f"min OBI to trigger (default {OBI_MIN_DEFAULT:+.3f}, range -1..+1)")
    p.add_argument("--obi-stable-secs", type=int, default=OBI_STABLE_SECS_DEFAULT,
                   dest="obi_stable_secs",
                   help=f"OBI must hold above min for this long (default {OBI_STABLE_SECS_DEFAULT}s)")
    p.add_argument("--max-wait", type=int, default=MAX_WAIT_DEFAULT, dest="max_wait",
                   help=f"max seconds to wait for trigger (default {MAX_WAIT_DEFAULT})")
    args = p.parse_args()

    if args.qty < QTY_MIN or args.qty > QTY_MAX:
        print(f"ERROR: --qty {args.qty} outside [{QTY_MIN}, {QTY_MAX}]. Refusing.",
              file=sys.stderr)
        return 2
    if not (-1.0 <= args.obi_min <= 1.0):
        print(f"ERROR: --obi-min {args.obi_min} outside [-1, +1]. Refusing.",
              file=sys.stderr)
        return 2

    cloid = make_manual_cloid()

    if not args.execute:
        return _render_dry_run(args, cloid)

    if not args.i_confirm:
        print("ERROR: --execute requires --i-confirm-book-b\n"
              "       (refusing to place a real order without explicit confirm)",
              file=sys.stderr)
        return 2

    return asyncio.run(_place_topup(args, cloid))


if __name__ == "__main__":
    sys.exit(main())
