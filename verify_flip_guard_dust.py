#!/usr/bin/env python3
"""
verify_flip_guard_dust.py — unit-test the dust-tolerance patch in
HLEngine._flip_guard_ok without hitting the network.

We monkey-bind the coroutine onto a stub `self` that provides:
  - self._hl.get_positions()        (async, returns a scripted list)
  - self._signals._state[sym]       (with .open_qty(tag) + .pending_exits)
  - self._signals.reconcile_hl_positions / rollback_entry / rollback_exit

Each row in CASES asserts the guard's expected decision and, on a block,
which rollback hook fired. Run: python verify_flip_guard_dust.py
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from hl_engine import HLEngine, STRATEGY_TAG

# Test-local universe map. Matches what HLEngine.__init__ would build from
# HL_UNIVERSE="BTC,ETH,SOL" + a live meta() probe. Pre-baking it here keeps the
# test hermetic (no network on import).
_TEST_SZ_DECIMALS: dict[str, int] = {"BTC": 5, "ETH": 4, "SOL": 2}
_TEST_COIN_TO_SYMBOL: dict[str, str] = {
    c: f"{c}/USD" for c in _TEST_SZ_DECIMALS
}
_TEST_SYMBOL_TO_COIN: dict[str, str] = {
    v: k for k, v in _TEST_COIN_TO_SYMBOL.items()
}
_TEST_DUST_CAPS: dict[str, float] = {
    c: 1.5 * (10 ** -d) for c, d in _TEST_SZ_DECIMALS.items()
}


# ── Stubs ─────────────────────────────────────────────────────────────────────
class FakeHL:
    def __init__(self, positions):
        self._positions = positions

    async def get_positions(self):
        return self._positions


class FakeState:
    def __init__(self, mem_szi: float, pending_exit: bool):
        self._mem = mem_szi
        self.pending_exits = {STRATEGY_TAG: pending_exit}

    def open_qty(self, tag):
        return self._mem if tag == STRATEGY_TAG else 0.0


class FakeSignals:
    def __init__(self, sym: str, mem_szi: float, pending_exit: bool):
        self._state = {sym: FakeState(mem_szi, pending_exit)}
        self.calls = []  # log which hooks ran

    def reconcile_hl_positions(self, positions, coin_map, dust_caps=None):
        self.calls.append(("reconcile", len(positions)))

    def rollback_entry(self, sym):
        self.calls.append(("rollback_entry", sym))

    def rollback_exit(self, sym):
        self.calls.append(("rollback_exit", sym))


def run_case(case: dict) -> dict:
    sym   = case["symbol"]
    coin  = _TEST_SYMBOL_TO_COIN[sym]

    positions = []
    if case["live_szi"] != 0.0:
        positions.append({"coin": coin, "szi": case["live_szi"]})

    stub = SimpleNamespace(
        _hl             = FakeHL(positions),
        _signals        = FakeSignals(sym, case["mem_szi"], case["pending_exit"]),
        _coin_to_symbol = _TEST_COIN_TO_SYMBOL,
        _symbol_to_coin = _TEST_SYMBOL_TO_COIN,
        _sz_decimals    = _TEST_SZ_DECIMALS,
        _dust_caps      = _TEST_DUST_CAPS,
    )

    sig = {"symbol": sym}

    # Bind the unbound coroutine to our stub.
    coro = HLEngine._flip_guard_ok(stub, sig)
    result = asyncio.run(coro)

    return {
        "allowed": result,
        "hooks":   stub._signals.calls,
    }


# ── Test matrix ───────────────────────────────────────────────────────────────
CASES = [
    # --- ENTRY branch (pending_exit=False) ---
    dict(name="ETH entry, on-chain flat, mem pre-written",
         symbol="ETH/USD", live_szi=0.0, mem_szi=-0.0427,
         pending_exit=False,
         expect_allowed=True, expect_rollback=None),

    dict(name="ETH entry, -0.0001 dust on chain (the deadlock case)",
         symbol="ETH/USD", live_szi=-0.0001, mem_szi=-0.0427,
         pending_exit=False,
         expect_allowed=True, expect_rollback=None),  # patch allows this

    dict(name="ETH entry, -0.0002 (2 lots, clearly above 1.5x cap)",
         symbol="ETH/USD", live_szi=-0.0002, mem_szi=-0.0427,
         pending_exit=False,
         expect_allowed=False, expect_rollback="rollback_entry"),

    dict(name="ETH entry, -0.0010 genuine short on chain → block",
         symbol="ETH/USD", live_szi=-0.0010, mem_szi=-0.0427,
         pending_exit=False,
         expect_allowed=False, expect_rollback="rollback_entry"),

    dict(name="BTC entry, 1e-5 dust (1 lot) → allow",
         symbol="BTC/USD", live_szi=0.00001, mem_szi=-0.00134,
         pending_exit=False,
         expect_allowed=True, expect_rollback=None),

    dict(name="BTC entry, 2e-5 (2 lots, above 1.5x) → block",
         symbol="BTC/USD", live_szi=0.00002, mem_szi=-0.00134,
         pending_exit=False,
         expect_allowed=False, expect_rollback="rollback_entry"),

    # --- SOL entries (szDecimals=2, dust_cap=1.5e-2) ---
    dict(name="SOL entry, on-chain flat → allow",
         symbol="SOL/USD", live_szi=0.0, mem_szi=-0.5,
         pending_exit=False,
         expect_allowed=True, expect_rollback=None),

    dict(name="SOL entry, 1-lot dust 0.01 (under 1.5x cap) → allow",
         symbol="SOL/USD", live_szi=0.01, mem_szi=-0.5,
         pending_exit=False,
         expect_allowed=True, expect_rollback=None),

    dict(name="SOL entry, 0.02 (2 lots, above 1.5x cap) → block",
         symbol="SOL/USD", live_szi=0.02, mem_szi=-0.5,
         pending_exit=False,
         expect_allowed=False, expect_rollback="rollback_entry"),

    dict(name="SOL entry, 0.5 genuine long on chain → block",
         symbol="SOL/USD", live_szi=0.5, mem_szi=-0.5,
         pending_exit=False,
         expect_allowed=False, expect_rollback="rollback_entry"),

    # --- EXIT branch (pending_exit=True) ---
    dict(name="ETH exit, live matches memory (same sign)",
         symbol="ETH/USD", live_szi=-0.0427, mem_szi=-0.0427,
         pending_exit=True,
         expect_allowed=True, expect_rollback=None),

    dict(name="ETH exit, live dust -0.0001 → block (effectively flat)",
         symbol="ETH/USD", live_szi=-0.0001, mem_szi=-0.0427,
         pending_exit=True,
         expect_allowed=False, expect_rollback="rollback_exit"),

    dict(name="ETH exit, live flat 0.0 → block",
         symbol="ETH/USD", live_szi=0.0, mem_szi=-0.0427,
         pending_exit=True,
         expect_allowed=False, expect_rollback="rollback_exit"),

    dict(name="ETH exit, live is +0.0427 (wrong side) → block",
         symbol="ETH/USD", live_szi=+0.0427, mem_szi=-0.0427,
         pending_exit=True,
         expect_allowed=False, expect_rollback="rollback_exit"),

    dict(name="SOL exit, live dust 0.01 (under cap) → block (effectively flat)",
         symbol="SOL/USD", live_szi=0.01, mem_szi=-0.5,
         pending_exit=True,
         expect_allowed=False, expect_rollback="rollback_exit"),

    dict(name="SOL exit, live matches memory -0.5 → allow",
         symbol="SOL/USD", live_szi=-0.5, mem_szi=-0.5,
         pending_exit=True,
         expect_allowed=True, expect_rollback=None),
]


def main() -> None:
    print(f"{'─'*78}")
    print("flip-guard dust-tolerance verification")
    print(f"{'─'*78}")

    passes = fails = 0
    for i, case in enumerate(CASES, 1):
        out    = run_case(case)
        ok     = out["allowed"] == case["expect_allowed"]
        rb_hit = next(
            (h[0] for h in out["hooks"] if h[0].startswith("rollback_")),
            None,
        )
        if case["expect_rollback"] is not None:
            ok = ok and rb_hit == case["expect_rollback"]
        else:
            ok = ok and rb_hit is None

        tag = "PASS" if ok else "FAIL"
        if ok:
            passes += 1
        else:
            fails += 1

        print(f"[{tag}] {i:>2}. {case['name']}")
        print(f"       allowed={out['allowed']} (expect {case['expect_allowed']})  "
              f"rollback={rb_hit} (expect {case['expect_rollback']})")

    print(f"{'─'*78}")
    print(f"{passes} pass  /  {fails} fail")
    raise SystemExit(0 if fails == 0 else 1)


if __name__ == "__main__":
    main()
