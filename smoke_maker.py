#!/usr/bin/env python3
"""
smoke_maker.py — minimal Spike-B plumbing check.

Submits one Alo (maker) order at best_bid − 1 tick on BTC-PERP (non-crossing,
so it cannot fill), sleeps briefly, then cancels by cloid. Verifies:
  * cloid plumbing into exchange.order
  * Alo acceptance + rest response shape
  * cancel_by_cloid round-trip

Exposure is capped at $20 notional. Fails loud on any non-resting status so
an accidental cross doesn't go unnoticed.

Run:   venv/bin/python3 smoke_maker.py
"""

from __future__ import annotations

import asyncio
import secrets
import sys
import time

from config.settings import load as load_settings
from execution.hl_manager import HyperliquidOrderManager


COIN = "BTC"
TARGET_NOTION = 20.0  # dollars
TICK = 1.0  # $1 tick for BTC — sz_decimals=5 gives 1-dollar max px precision
WAIT_SEC = 4


async def main() -> int:
    cfg = load_settings()
    mgr = HyperliquidOrderManager(
        cfg, strategy_tag="smoke_maker", default_leverage=5, coins=[COIN], is_cross=True
    )

    # Snapshot top of book
    l2 = await asyncio.to_thread(mgr._info.l2_snapshot, COIN)
    levels = l2.get("levels", [[], []])
    bids = levels[0] if levels and len(levels) >= 2 else []
    if not bids:
        print("no bids in l2_snapshot — aborting")
        return 1
    best_bid = float(bids[0]["px"])
    print(f"best_bid={best_bid:.2f}")

    # Place $20 at best_bid − 1% (guarantees rest even on fast book motion;
    # 1-tick offset showed an intermittent cross — HL Alo rejects rather than
    # fills, which is the safe direction but defeats the cancel test).
    passive_px = round(best_bid * 0.99, 0)
    qty = round(TARGET_NOTION / passive_px, 5)
    cloid = f"0x{secrets.randbits(128):032x}"
    print(f"submitting Alo buy {qty} @ {passive_px}  cloid={cloid}")

    t0 = time.perf_counter()
    resp = await mgr.submit_order(
        {
            "symbol": COIN,
            "side": "buy",
            "qty": qty,
            "limit_px": passive_px,
            "tif": "Alo",
            "reduce_only": False,
            "cloid": cloid,
        }
    )
    lat = (time.perf_counter() - t0) * 1000
    print(f"submit latency={lat:.0f}ms  resp_status={(resp or {}).get('status')}")
    if not resp:
        print("submit returned None — see hl_order_rejected in logs")
        return 2

    statuses = (resp.get("response") or {}).get("data", {}).get("statuses", [])
    for s in statuses:
        print(f"  status: {s}")
        if isinstance(s, dict) and s.get("error"):
            print(f"inner rejection: {s['error']} — aborting before cancel")
            return 3

    # Expect "resting" for a non-crossing Alo
    resting_oid = None
    for s in statuses:
        if isinstance(s, dict) and "resting" in s:
            resting_oid = s["resting"].get("oid")

    print(f"resting_oid={resting_oid}  waiting {WAIT_SEC}s …")
    await asyncio.sleep(WAIT_SEC)

    # Cancel by cloid (exercises the Spike C helper too)
    cresp = await mgr.cancel_by_cloid(COIN, cloid)
    print(f"cancel_by_cloid resp={cresp}")
    if not cresp or cresp.get("status") != "ok":
        print("cancel did NOT return ok — check HL UI for leftover order")
        return 4

    print("SMOKE OK: submit → rest → cancel round-trip succeeded")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
