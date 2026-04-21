#!/usr/bin/env python3
"""One-off: pin AAVE isolated 10x, place 10 AAVE buy ALO at mid."""

from __future__ import annotations

import asyncio
import secrets
import sys

from config.settings import load as load_settings
from execution.hl_manager import HyperliquidOrderManager

COIN = "AAVE"
QTY = 10.0
LEVERAGE = 10


async def main() -> int:
    cfg = load_settings()
    mgr = HyperliquidOrderManager(
        cfg,
        strategy_tag="manual_aave",
        default_leverage=LEVERAGE,
        coins=[COIN],
        is_cross=False,
    )

    # Pin isolated 10x
    lev_resp = await asyncio.to_thread(
        mgr._exchange.update_leverage, LEVERAGE, COIN, False
    )
    print(f"leverage set: {lev_resp}")

    # Snapshot book
    l2 = await asyncio.to_thread(mgr._info.l2_snapshot, COIN)
    bids = l2["levels"][0]
    asks = l2["levels"][1]
    best_bid = float(bids[0]["px"])
    best_ask = float(asks[0]["px"])
    mid = (best_bid + best_ask) / 2.0
    # AAVE tick = 0.001 on HL; round to 3 dp
    limit_px = round(mid, 3)
    # Ensure non-crossing ALO: must be strictly < ask
    if limit_px >= best_ask:
        limit_px = round(best_ask - 0.001, 3)
    print(f"bid={best_bid} ask={best_ask} mid={mid:.4f} -> limit_px={limit_px}")

    cloid = f"0x{secrets.randbits(128):032x}"
    resp = await mgr.submit_order(
        {
            "symbol": COIN,
            "side": "buy",
            "qty": QTY,
            "limit_px": limit_px,
            "tif": "Alo",
            "reduce_only": False,
            "cloid": cloid,
        }
    )
    print(f"submit resp: {resp}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
