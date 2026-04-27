#!/usr/bin/env python3
"""Regression test for the bucketed re-entry cooldown config.

Asserts that the structural buckets in
config/gates/reentry_cooldown_by_symbol.json remain wired correctly:
  - long-hold natives are exempt (cooldown 0)
  - HIP-3/xyz equity perps and ZEC are at 3600s
  - default cooldown is 0 unless explicitly enabled via env

Run:
    venv/bin/python3 scripts/test_reentry_cooldown_config.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CONFIG = ROOT / "config" / "gates" / "reentry_cooldown_by_symbol.json"


def load_map(path: Path) -> dict[str, int]:
    cfg = json.loads(path.read_text())
    out: dict[str, int] = {}
    for grp in (cfg.get("groups") or {}).values():
        cd = int(grp.get("cooldown_s", 0))
        for sym in grp.get("symbols") or []:
            out[sym] = cd
    for sym, cd in (cfg.get("overrides") or {}).items():
        out[sym] = int(cd)
    return out, int(cfg.get("default", 0))


def main() -> int:
    cooldown, default = load_map(CONFIG)
    failures: list[str] = []

    def check(label: str, cond: bool):
        if not cond:
            failures.append(label)

    # Long-hold natives must be exempt (cooldown 0)
    for sym in ("AAVE", "ETH", "BTC", "SOL", "LDO", "CRV", "DOGE", "LINK"):
        check(
            f"{sym} cooldown=0 (long-hold native)",
            cooldown.get(sym, default) == 0,
        )

    # HIP-3 / xyz equity perps must be 3600s
    for sym in ("xyz:MSTR", "xyz:NVDA", "xyz:AMZN", "xyz:INTC", "xyz:CL", "xyz:CRCL"):
        check(
            f"{sym} cooldown=3600 (HIP-3 equity)",
            cooldown.get(sym, default) == 3600,
        )

    # ZEC must be 3600s (auto_topup watcher)
    check("ZEC cooldown=3600 (auto_topup watcher)", cooldown.get("ZEC", default) == 3600)

    # Default must be 0
    check("default cooldown = 0", default == 0)

    # Module-level default behavior: when REENTRY_COOLDOWN_BY_SYMBOL is unset,
    # the replay's MIN_REENTRY_COOLDOWN_S also defaults to 0 (off).
    saved = {k: os.environ.pop(k, None) for k in
             ("MIN_REENTRY_COOLDOWN_S", "REENTRY_COOLDOWN_BY_SYMBOL", "MAX_OPENS_PER_SYMBOL_PER_DAY")}
    try:
        # Reload module fresh to read defaults
        for mod_name in list(sys.modules):
            if mod_name.startswith("scripts.z_entry_replay_gated"):
                del sys.modules[mod_name]
        import scripts.z_entry_replay_gated as m  # noqa: E402

        check(
            f"MIN_REENTRY_COOLDOWN_S default = 0 (got {m.MIN_REENTRY_COOLDOWN_S})",
            m.MIN_REENTRY_COOLDOWN_S == 0,
        )
        check(
            f"MAX_OPENS_PER_SYMBOL_PER_DAY default = 0 (got {m.MAX_OPENS_PER_SYMBOL_PER_DAY})",
            m.MAX_OPENS_PER_SYMBOL_PER_DAY == 0,
        )
        check(
            "REENTRY_COOLDOWN_BY_SYMBOL_FILE empty by default",
            m.REENTRY_COOLDOWN_BY_SYMBOL_FILE == "",
        )
        check(
            "REENTRY_COOLDOWN_BY_SYMBOL map empty by default",
            len(m.REENTRY_COOLDOWN_BY_SYMBOL) == 0,
        )
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v

    if failures:
        print("FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS — all assertions hold")
    print(f"  symbols configured:   {len(cooldown)}")
    print(f"  default cooldown:     {default}")
    print("  longhold exempt:      AAVE/ETH/BTC/SOL/LDO/CRV/...")
    print("  HIP-3 / xyz / ZEC:    3600s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
