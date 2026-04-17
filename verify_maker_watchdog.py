#!/usr/bin/env python3
"""
verify_maker_watchdog.py — Spike-C plumbing verification (no venue contact).

Exercises the watchdog decision points on a stubbed HLEngine instance:
  1. Reprice when the market moves "behind the queue" (buy: best_bid rises
     above our resting px; sell: best_ask falls below ours).
  2. Partial-fill bookkeeping — first partial must NOT clear pending; the
     terminal fill must.
  3. Lifetime-exceeded triggers _cancel_pending + rollback_entry.
  4. Cancel race — if cancel_by_cloid returns non-success (fill won),
     _reprice_pending leaves pending alone instead of re-submitting.

All HL interactions are mocked. This runs in < 1s.
"""

from __future__ import annotations

import asyncio
import math
import sys
import time
import types


# ── Stubs: replace heavy imports before hl_engine loads ─────────────────────


def _install_stubs() -> None:
    """Make hl_engine import without Alpaca/HL SDK / env vars."""
    # Settings stub
    settings_mod = types.ModuleType("config.settings")

    class ExecutionMode:
        SHADOW = types.SimpleNamespace(value="SHADOW")
        PAPER = types.SimpleNamespace(value="PAPER")
        LIVE = types.SimpleNamespace(value="LIVE")

    class Settings:
        hl_wallet_address = "0x0000000000000000000000000000000000000000"
        hl_private_key = "0x" + "00" * 32
        execution_mode = ExecutionMode.SHADOW

    def load():
        return Settings()

    settings_mod.Settings = Settings
    settings_mod.ExecutionMode = ExecutionMode
    settings_mod.load = load
    sys.modules["config.settings"] = settings_mod

    # LiveFeed / HyperliquidFeed / HyperliquidOrderManager stubs
    live_feed_mod = types.ModuleType("data.feed")

    class LiveFeed:
        def __init__(self, *a, **kw):
            pass

        async def run(self):
            await asyncio.sleep(0)

    live_feed_mod.LiveFeed = LiveFeed
    sys.modules["data.feed"] = live_feed_mod

    hl_feed_mod = types.ModuleType("data.hl_feed")

    class HyperliquidFeed:
        def __init__(self, *a, **kw):
            pass

        async def run(self):
            await asyncio.sleep(0)

        def stop(self):
            pass

    hl_feed_mod.HyperliquidFeed = HyperliquidFeed
    sys.modules["data.hl_feed"] = hl_feed_mod

    hl_mgr_mod = types.ModuleType("execution.hl_manager")

    class HyperliquidOrderManager:
        def __init__(self, *a, **kw):
            pass

    hl_mgr_mod.HyperliquidOrderManager = HyperliquidOrderManager
    sys.modules["execution.hl_manager"] = hl_mgr_mod


_install_stubs()
import hl_engine  # noqa: E402
from hl_engine import HLEngine, MAKER_MAX_LIFETIME_S  # noqa: E402
from strategy.signals import SignalEngine  # noqa: E402


# Test universe — mirrors what HLEngine.__init__ builds from HL_UNIVERSE="BTC,ETH"
# + Info.meta() probe. Hermetic, no network.
_TEST_COINS = ["BTC", "ETH"]
_TEST_SYMBOLS = [f"{c}/USD" for c in _TEST_COINS]
_TEST_C2S = {c: f"{c}/USD" for c in _TEST_COINS}
_TEST_S2C = {v: k for k, v in _TEST_C2S.items()}
_TEST_SZ_DEC = {"BTC": 5, "ETH": 4}
_TEST_DUST_CAPS = {c: 1.5 * (10**-d) for c, d in _TEST_SZ_DEC.items()}


# ── Fakes wired into an HLEngine instance ───────────────────────────────────


class FakeHL:
    def __init__(self):
        self.submits: list[dict] = []
        self.cancels: list[dict] = []
        # Queue of canned responses; each call pops front. If empty, returns
        # the default success.
        self.submit_responses: list = []
        self.cancel_responses: list = []

    async def submit_order(self, order):
        self.submits.append(order)
        if self.submit_responses:
            return self.submit_responses.pop(0)
        return {
            "status": "ok",
            "response": {
                "type": "order",
                "data": {
                    "statuses": [
                        {"resting": {"oid": 1, "cloid": order.get("cloid", "")}}
                    ]
                },
            },
        }

    async def cancel_by_cloid(self, symbol, cloid):
        self.cancels.append({"symbol": symbol, "cloid": cloid})
        if self.cancel_responses:
            return self.cancel_responses.pop(0)
        return {
            "status": "ok",
            "response": {"type": "cancel", "data": {"statuses": ["success"]}},
        }


def _make_engine() -> HLEngine:
    eng = HLEngine.__new__(HLEngine)  # bypass __init__ / HL boot
    eng._cfg = None
    eng._hl = FakeHL()
    eng._hl_coins = list(_TEST_COINS)
    eng._hl_symbols = list(_TEST_SYMBOLS)
    eng._coin_to_symbol = dict(_TEST_C2S)
    eng._symbol_to_coin = dict(_TEST_S2C)
    eng._sz_decimals = dict(_TEST_SZ_DEC)
    eng._dust_caps = dict(_TEST_DUST_CAPS)
    eng._default_leverage = 2
    eng._signals = SignalEngine(
        symbols=_TEST_SYMBOLS,
        strategy_tag=hl_engine.STRATEGY_TAG,
        allow_short=True,
    )
    eng._msg_q = asyncio.Queue()
    eng._hl_raw_q = asyncio.Queue()
    eng._running = True
    eng._pending_resting = {}
    return eng


# ── Tests ───────────────────────────────────────────────────────────────────


def _seed_pending(
    eng: HLEngine,
    *,
    sym: str,
    side: str,
    qty: float,
    px: float,
    age_s: float = 0.0,
    reprice_count: int = 0,
    is_entry: bool = True,
) -> None:
    eng._pending_resting[sym] = {
        "cloid": "0x" + "ab" * 16,
        "cid": "test_cid",
        "side": side,
        "qty": qty,
        "last_px": px,
        "reprice_count": reprice_count,
        "is_entry": is_entry,
        "submit_ts": int(time.time() - age_s),
        "filled_qty": 0.0,
    }


async def test_reprice_when_behind_queue():
    eng = _make_engine()
    sym = "BTC/USD"
    # Our resting buy is at 74000; market has moved to 74050 — we're behind.
    _seed_pending(eng, sym=sym, side="buy", qty=0.01, px=74000.0)
    st = eng._signals._state[sym]
    st.best_bid = 74050.0
    st.best_ask = 74055.0

    await eng._reprice_pending(sym, new_px=74050.0)

    assert len(eng._hl.cancels) == 1, "expected 1 cancel"
    assert len(eng._hl.submits) == 1, "expected 1 resubmit"
    pending = eng._pending_resting[sym]
    assert pending["last_px"] == 74050.0
    assert pending["reprice_count"] == 1
    assert pending["cid"] == "test_cid", "cid must persist across reprices"
    assert pending["cloid"] != "0x" + "ab" * 16, "cloid must be refreshed"
    print("[PASS]  1. reprice when behind queue (BTC buy)")


async def test_partial_fill_bookkeeping():
    eng = _make_engine()
    sym = "ETH/USD"
    qty = 0.01
    _seed_pending(eng, sym=sym, side="buy", qty=qty, px=3000.0)
    cloid = eng._pending_resting[sym]["cloid"]

    # Partial 1: 0.004 of 0.01 — pending stays
    eng._handle_hl_fill(
        {
            "type": "hl_fill",
            "symbol": sym,
            "side": "buy",
            "px": 3000.0,
            "sz": 0.004,
            "cloid": cloid,
        }
    )
    assert sym in eng._pending_resting, "partial 1 must not clear pending"
    assert eng._pending_resting[sym]["filled_qty"] == 0.004

    # Partial 2: 0.004 more — still not terminal
    eng._handle_hl_fill(
        {
            "type": "hl_fill",
            "symbol": sym,
            "side": "buy",
            "px": 3000.0,
            "sz": 0.004,
            "cloid": cloid,
        }
    )
    assert sym in eng._pending_resting, "partial 2 must not clear pending"
    assert eng._pending_resting[sym]["filled_qty"] == 0.008

    # Terminal: remaining 0.002 (floor-of-lot tolerance not required here)
    eng._handle_hl_fill(
        {
            "type": "hl_fill",
            "symbol": sym,
            "side": "buy",
            "px": 3000.0,
            "sz": 0.002,
            "cloid": cloid,
        }
    )
    assert sym not in eng._pending_resting, "terminal fill must clear pending"
    print("[PASS]  2. partial-fill bookkeeping (3 chunks of ETH buy)")


async def test_lifetime_exceeded_cancels_and_rolls_back():
    eng = _make_engine()
    sym = "BTC/USD"
    _seed_pending(
        eng, sym=sym, side="buy", qty=0.01, px=74000.0, age_s=MAKER_MAX_LIFETIME_S + 5
    )
    # Pre-write optimistic entry so rollback has something to undo
    st = eng._signals._state[sym]
    tag = hl_engine.STRATEGY_TAG
    st.positions[tag] = 0.01
    st.entry_prices[tag] = 74000.0

    await eng._cancel_pending(sym, reason="lifetime_exceeded")

    assert len(eng._hl.cancels) == 1
    assert sym not in eng._pending_resting, "pending must be cleared"
    assert st.positions[tag] == 0.0, "entry rollback must zero position"
    assert math.isnan(st.entry_prices[tag])
    print("[PASS]  3. lifetime-exceeded → cancel + rollback_entry")


async def test_reprice_race_leaves_pending_alone():
    eng = _make_engine()
    sym = "BTC/USD"
    _seed_pending(eng, sym=sym, side="buy", qty=0.01, px=74000.0)

    # Cancel reports failure — fill won the race. _reprice_pending must
    # leave pending in place so the userFills WS handler can clear it.
    eng._hl.cancel_responses.append(
        {
            "status": "ok",
            "response": {
                "type": "cancel",
                "data": {"statuses": [{"error": "Order was already filled"}]},
            },
        }
    )
    await eng._reprice_pending(sym, new_px=74050.0)

    assert len(eng._hl.cancels) == 1, "cancel must have been attempted once"
    assert len(eng._hl.submits) == 0, "no re-submit when cancel fails"
    assert sym in eng._pending_resting, "pending must remain for WS handler"
    assert eng._pending_resting[sym]["last_px"] == 74000.0, "state untouched"
    print("[PASS]  4. cancel-race → no resubmit, pending preserved")


async def main() -> int:
    print("─" * 78)
    print("Spike C (maker watchdog) verification")
    print("─" * 78)
    tests = [
        test_reprice_when_behind_queue,
        test_partial_fill_bookkeeping,
        test_lifetime_exceeded_cancels_and_rolls_back,
        test_reprice_race_leaves_pending_alone,
    ]
    failed = 0
    for t in tests:
        try:
            await t()
        except AssertionError as exc:
            failed += 1
            print(f"[FAIL]  {t.__name__}: {exc}")
        except Exception as exc:
            failed += 1
            print(f"[ERROR] {t.__name__}: {type(exc).__name__}: {exc}")
    print("─" * 78)
    print(f"{len(tests) - failed} pass  /  {failed} fail")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
