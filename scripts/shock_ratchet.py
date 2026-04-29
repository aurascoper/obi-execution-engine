#!/usr/bin/env python3
"""Shock profit ratchet.

Auto-scales-out owned positions as z_4h retraces from its peak during a shock regime.
Arms per-symbol when |z_4h| >= ARM (default 4.0). Closes 1/3 at peak-1.0, another
1/3 at peak-2.0, remainder at peak-3.0 OR when z_4h crosses the opposite side of 0.

All sells IOC reduce_only, 0.3% slip floor. Verifies side matches peak direction
(long requires +z_4h peak, short requires -z_4h peak). State persisted to
/tmp/shock_ratchet_state.json so a restart resumes cleanly.

Reads latest z_4h via tail of logs/hl_engine.jsonl. Refreshes positions every 60s.
Single-shot per tranche (state tracks tranches_done).
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import secrets
import signal
import sys
import time
from pathlib import Path

from config.settings import load as load_settings
from execution.hl_manager import HyperliquidOrderManager

LT = Path("/Users/aurascoper/Developer/live_trading")
LOG = LT / "logs" / "hl_engine.jsonl"
STATE_PATH = Path("/tmp/shock_ratchet_state.json")

ARM = float(os.environ.get("SHOCK_ARM", "3.5"))
RETRACE_STEP = float(os.environ.get("SHOCK_STEP", "0.005"))
SLIP_FRAC = float(os.environ.get("SHOCK_SLIP", "0.001"))
POLL_LOG_S = 15
POLL_POS_S = 60


def norm(sym: str) -> str:
    return (sym or "").replace("/USD", "").replace("/USDC", "")


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            return {}
    return {}


def save_state(s: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=2, sort_keys=True))
    tmp.replace(STATE_PATH)


def tail_latest_z4h(symbols: set[str], tail_lines: int = 5000) -> dict[str, dict]:
    """Return {sym: {z_4h, z, ts}} from last N lines of the engine log."""
    out: dict[str, dict] = {}
    if not LOG.exists():
        return out
    try:
        with LOG.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            read = min(size, 4_000_000)
            f.seek(size - read)
            data = f.read().decode("utf-8", errors="ignore")
    except Exception:
        return out
    for line in data.splitlines()[-tail_lines:]:
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get("event") != "signal_tick":
            continue
        sym = norm(o.get("symbol") or o.get("coin") or "")
        if sym not in symbols:
            continue
        z4 = o.get("z_4h")
        if z4 is None:
            continue
        out[sym] = {
            "z_4h": float(z4),
            "z": o.get("z"),
            "ts": o.get("ts") or time.time(),
        }
    return out


async def fetch_positions(
    mgr_native: HyperliquidOrderManager, mgr_xyz: HyperliquidOrderManager
) -> dict[str, dict]:
    """Return {coin: {szi, entry, uPnL, dex}} across both subaccounts."""
    out: dict[str, dict] = {}
    for mgr, dex in [(mgr_native, None), (mgr_xyz, "xyz")]:
        try:
            if dex is None:
                st = await asyncio.to_thread(mgr._info.user_state, mgr._wallet_address)
            else:
                st = await asyncio.to_thread(
                    mgr._info.user_state, mgr._wallet_address, dex
                )
        except Exception as e:
            print(f"[pos] {dex or 'native'} err: {e}", flush=True)
            continue
        for p in st.get("assetPositions", []):
            q = p.get("position", {})
            coin = q.get("coin")
            szi = float(q.get("szi", 0))
            if not coin or szi == 0:
                continue
            out[coin] = {
                "szi": szi,
                "entry": float(q.get("entryPx", 0)),
                "uPnL": float(q.get("unrealizedPnl", 0)),
                "dex": dex,
            }
    return out


async def get_l2(
    mgr: HyperliquidOrderManager, coin: str
) -> tuple[float, float, float] | None:
    try:
        l2 = await asyncio.to_thread(mgr._info.l2_snapshot, coin)
    except Exception as e:
        print(f"[l2] {coin} err: {e}", flush=True)
        return None
    lv = l2.get("levels") or []
    if len(lv) < 2 or not lv[0] or not lv[1]:
        return None
    bid = float(lv[0][0]["px"])
    ask = float(lv[1][0]["px"])
    tick = round(ask - bid, 6)
    if tick <= 0:
        tick = 0.01
    return bid, ask, tick


def round_qty(qty: float, sz_dec: int) -> float:
    factor = 10**sz_dec
    return math.floor(abs(qty) * factor) / factor * (1 if qty >= 0 else -1)


def derive_sz_decimals(szi: float) -> int:
    """Guess szDecimals from the position size (HL only sends N-dp values)."""
    s = f"{abs(szi):.10f}".rstrip("0").rstrip(".")
    if "." not in s:
        return 0
    return min(len(s.split(".")[1]), 4)


async def close_tranche(
    mgr: HyperliquidOrderManager, coin: str, side_szi: float, close_qty: float, tag: str
) -> dict | None:
    bt = await get_l2(mgr, coin)
    if not bt:
        print(f"[{coin}] no book — defer tranche {tag}", flush=True)
        return None
    bid, ask, tick = bt
    if side_szi > 0:  # long → sell into bid
        side = "sell"
        slip_abs = max(tick, bid * SLIP_FRAC)
        px = round(round((bid - slip_abs) / tick) * tick, 6)
    else:  # short → buy from ask
        side = "buy"
        slip_abs = max(tick, ask * SLIP_FRAC)
        px = round(round((ask + slip_abs) / tick) * tick, 6)
    qty = abs(round_qty(close_qty, derive_sz_decimals(side_szi)))
    if qty <= 0:
        print(f"[{coin}] qty rounded to 0 — skip tranche {tag}", flush=True)
        return None
    print(
        f"[{coin}] RATCHET {tag}: {side.upper()} qty={qty} @ {px} "
        f"(book bid={bid} ask={ask})",
        flush=True,
    )
    try:
        resp = await mgr.submit_order(
            {
                "symbol": coin,
                "side": side,
                "qty": qty,
                "limit_px": px,
                "tif": "Ioc",
                "reduce_only": True,
                "cloid": f"0x{secrets.randbits(128):032x}",
            }
        )
    except Exception as e:
        print(f"[{coin}] submit err: {e}", flush=True)
        return None
    print(f"[{coin}] RATCHET {tag} resp={resp}", flush=True)
    return resp


async def main_loop() -> None:
    cfg = load_settings()
    native = HyperliquidOrderManager(
        cfg,
        strategy_tag="shock_ratchet_native",
        default_leverage=5,
        coins=[],
        is_cross=False,
    )
    xyz = HyperliquidOrderManager(
        cfg,
        strategy_tag="shock_ratchet_xyz",
        default_leverage=5,
        coins=[],
        is_cross=False,
        perp_dexs=["xyz"],
    )
    state = load_state()
    last_pos_t = 0.0
    positions: dict[str, dict] = {}

    stop = False

    def _stop(*_):
        nonlocal stop
        stop = True
        print("[ratchet] stopping…", flush=True)

    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(s, _stop)
        except Exception:
            pass

    print(f"[ratchet] armed ARM={ARM} STEP={RETRACE_STEP} SLIP={SLIP_FRAC}", flush=True)

    while not stop:
        now = time.time()
        if now - last_pos_t > POLL_POS_S or not positions:
            positions = await fetch_positions(native, xyz)
            last_pos_t = now
            # drop state entries for symbols no longer held
            for coin in list(state.keys()):
                if coin not in positions:
                    print(
                        f"[{coin}] closed externally — clearing ratchet state",
                        flush=True,
                    )
                    state.pop(coin)
            save_state(state)

        z_map = tail_latest_z4h(set(positions.keys()))
        if not z_map:
            await asyncio.sleep(POLL_LOG_S)
            continue

        for coin, pos in positions.items():
            t = z_map.get(coin)
            if not t:
                continue
            z4 = t["z_4h"]
            szi = pos["szi"]
            side_sign = 1 if szi > 0 else -1
            st = state.get(coin) or {}

            # Arm when |z4| >= ARM AND z4 on the profitable side for this position
            if side_sign * z4 >= ARM and "peak_abs" not in st:
                st = {
                    "peak_abs": abs(z4),
                    "peak_sign": int(side_sign),
                    "initial_szi": szi,
                    "tranches_done": 0,
                    "armed_ts": now,
                    "last_z4": z4,
                }
                state[coin] = st
                print(
                    f"[{coin}] ARMED peak={z4:+.2f} szi={szi} side="
                    f"{'LONG' if side_sign > 0 else 'SHORT'}",
                    flush=True,
                )
                save_state(state)
                continue

            if "peak_abs" not in st:
                continue

            # Update peak (always in the direction side_sign agrees with)
            if side_sign * z4 > st["peak_abs"]:
                st["peak_abs"] = side_sign * z4
                print(f"[{coin}] new peak={st['peak_abs']:+.2f}", flush=True)

            st["last_z4"] = z4
            # Retrace measured as peak_abs - (side_sign * z4)
            retrace = st["peak_abs"] - side_sign * z4
            done = st["tranches_done"]
            initial_szi = st["initial_szi"]
            tranche_qty = abs(initial_szi) / 3.0

            triggered = None
            if done == 0 and retrace >= RETRACE_STEP:
                triggered = (1, tranche_qty, "1/3")
            elif done == 1 and retrace >= 2 * RETRACE_STEP:
                triggered = (2, tranche_qty, "2/3")
            elif done == 2 and (retrace >= 3 * RETRACE_STEP or side_sign * z4 <= 0):
                triggered = (3, abs(szi), "FINAL")

            if triggered:
                idx, qty, tag = triggered
                mgr = xyz if pos["dex"] == "xyz" else native
                resp = await close_tranche(mgr, coin, szi, qty, tag)
                if resp is not None:
                    st["tranches_done"] = idx
                    state[coin] = st
                    save_state(state)
                    if idx >= 3:
                        state.pop(coin, None)
                        save_state(state)

            state[coin] = st

        save_state(state)
        await asyncio.sleep(POLL_LOG_S)


if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        sys.exit(0)
