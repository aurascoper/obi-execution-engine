#!/usr/bin/env python3
"""book_b_pretrade_calc.py — pre-trade sizing + ZEC-pull helper for Book B
manual hedge layering (CRWV long + XYZ100 short).

READ-ONLY by default. Pulls live marks for ZEC, CRWV, XYZ100; computes contract
sizes from target margin/leverage rounded to szDec; projects ZEC buffer
post-pull and post-trade free spot; exits non-zero if any threshold breached.

The ONLY write action this helper can perform is the ZEC margin pull, and only
when invoked with `--commit-zec-pull`. It will NEVER place orders for CRWV or
XYZ100 — those are operator-driven via the existing close_*.py-style tooling.

Usage (read-only sizing report):
    venv/bin/python3 scripts/book_b_pretrade_calc.py

Usage (execute the ZEC margin pull):
    venv/bin/python3 scripts/book_b_pretrade_calc.py --commit-zec-pull \
        --i-confirm-book-b

Plan defaults match plans/elegant-churning-tide.md:
    ZEC pull           = $150
    CRWV long          = $40 margin @ 10x → ~$400 notional
    XYZ100 short       = $58 margin @ 10x → ~$578 notional
    reserve floor      = $50
    ZEC YELLOW threshold = 15% buffer
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

# ── Plan defaults (from plans/elegant-churning-tide.md) ────────────────────
DEFAULT_ZEC_PULL_USD = 150.0
DEFAULT_CRWV_MARGIN = 40.0
DEFAULT_XYZ100_MARGIN = 58.0
DEFAULT_LEVERAGE = 10
RESERVE_FLOOR = 50.0
ZEC_YELLOW_THRESHOLD_PCT = 15.0
NEW_LINE_MIN_BUFFER_PCT = 9.0  # Strict Iso opens near 1/leverage = 10% minus haircut

BOOK_B_MASTER = "0x32D178fc6BC4CCC7AFBDB7Db78317cF2Bbd6C048"


def _liq_buffer_pct(mark: float, liq: float) -> float | None:
    if liq is None or liq == 0:
        return None
    return abs(mark - liq) / mark * 100.0


def _round_size(notional_usd: float, mark: float, sz_decimals: int) -> float:
    raw = notional_usd / mark
    factor = 10**sz_decimals
    # round DOWN to lot floor so we never overshoot the target margin
    import math

    return math.floor(raw * factor) / factor


def _projected_zec_buffer(margin_used_now: float, buf_now_pct: float, pull_usd: float) -> float:
    """Project new buffer % after pulling pull_usd from ZEC isolated margin.

    For HL-native isolated, liq_price = entry - margin_available / size / (1 - l).
    Pulling X dollars decreases margin_available by X, which increases liqPx
    proportionally for a long. The buffer % scales linearly with margin_available.
    Approximation: new_buf_pct ≈ buf_now * (margin_used_now - pull_usd) / margin_used_now.
    """
    if margin_used_now <= 0:
        return 0.0
    return buf_now_pct * (margin_used_now - pull_usd) / margin_used_now


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--zec-pull",
        type=float,
        default=DEFAULT_ZEC_PULL_USD,
        help=f"USD margin to pull from ZEC (default {DEFAULT_ZEC_PULL_USD})",
    )
    parser.add_argument(
        "--crwv-margin",
        type=float,
        default=DEFAULT_CRWV_MARGIN,
        help=f"USD margin for CRWV long (default {DEFAULT_CRWV_MARGIN})",
    )
    parser.add_argument(
        "--xyz100-margin",
        type=float,
        default=DEFAULT_XYZ100_MARGIN,
        help=f"USD margin for XYZ100 short (default {DEFAULT_XYZ100_MARGIN})",
    )
    parser.add_argument(
        "--leverage",
        type=int,
        default=DEFAULT_LEVERAGE,
        help=f"Leverage for new lines (default {DEFAULT_LEVERAGE})",
    )
    parser.add_argument(
        "--reserve-floor",
        type=float,
        default=RESERVE_FLOOR,
        help=f"Min free spot post-trade (default {RESERVE_FLOOR})",
    )
    parser.add_argument(
        "--commit-zec-pull",
        action="store_true",
        help="Actually execute the ZEC margin pull. No effect on CRWV/XYZ100.",
    )
    parser.add_argument(
        "--i-confirm-book-b",
        action="store_true",
        help="Required confirmation flag when --commit-zec-pull is set.",
    )
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    addr = os.environ.get("HL_WALLET_ADDRESS")
    if not addr:
        print("ERROR: HL_WALLET_ADDRESS not set in .env", file=sys.stderr)
        return 1
    if addr.lower() != BOOK_B_MASTER.lower():
        print(
            f"ERROR: HL_WALLET_ADDRESS={addr} is not the Book B master ({BOOK_B_MASTER}). "
            "Refusing to run.",
            file=sys.stderr,
        )
        return 1

    from hyperliquid.info import Info

    info = Info("https://api.hyperliquid.xyz", skip_ws=True)

    # ── Pull current state ────────────────────────────────────────────────
    us = info.user_state(addr)
    us_xyz = info.user_state(addr, dex="xyz")

    # ZEC current
    zec_pos = next(
        (p["position"] for p in us.get("assetPositions", []) if p["position"]["coin"] == "ZEC"),
        None,
    )
    if not zec_pos:
        print("ERROR: no ZEC position on Book B — aborting", file=sys.stderr)
        return 1
    native_ctxs = info.meta_and_asset_ctxs()
    zec_idx = next(
        i for i, u in enumerate(native_ctxs[0]["universe"]) if u["name"] == "ZEC"
    )
    zec_mark = float(native_ctxs[1][zec_idx]["markPx"])
    zec_liq = float(zec_pos["liquidationPx"])
    zec_marg = float(zec_pos["marginUsed"])
    zec_buf_now = _liq_buffer_pct(zec_mark, zec_liq) or 0.0
    zec_buf_proj = _projected_zec_buffer(zec_marg, zec_buf_now, args.zec_pull)

    # xyz marks for CRWV and XYZ100
    xyz_meta_ctx = info.post("/info", {"type": "metaAndAssetCtxs", "dex": "xyz"})
    xyz_universe = xyz_meta_ctx[0]["universe"]
    xyz_ctxs = xyz_meta_ctx[1]

    def _xyz_lookup(coin_short: str):
        for i, u in enumerate(xyz_universe):
            if u["name"] in (f"xyz:{coin_short}", coin_short) or u["name"].endswith(
                f":{coin_short}"
            ):
                return u, xyz_ctxs[i]
        raise KeyError(f"xyz:{coin_short} not found in universe")

    crwv_u, crwv_ctx = _xyz_lookup("CRWV")
    xyz100_u, xyz100_ctx = _xyz_lookup("XYZ100")
    crwv_mark = float(crwv_ctx["markPx"])
    xyz100_mark = float(xyz100_ctx["markPx"])
    crwv_szdec = int(crwv_u["szDecimals"])
    xyz100_szdec = int(xyz100_u["szDecimals"])
    crwv_max_lev = int(crwv_u.get("maxLeverage", 10))
    xyz100_max_lev = int(xyz100_u.get("maxLeverage", 30))

    # Sizing
    crwv_notional = args.crwv_margin * args.leverage
    xyz100_notional = args.xyz100_margin * args.leverage
    crwv_size = _round_size(crwv_notional, crwv_mark, crwv_szdec)
    xyz100_size = _round_size(xyz100_notional, xyz100_mark, xyz100_szdec)
    crwv_actual_notional = crwv_size * crwv_mark
    xyz100_actual_notional = xyz100_size * xyz100_mark
    crwv_actual_margin = crwv_actual_notional / args.leverage
    xyz100_actual_margin = xyz100_actual_notional / args.leverage

    # Spot reserve projection
    sp = info.spot_user_state(addr)
    usdc = next((b for b in sp.get("balances", []) if b["coin"] == "USDC"), None)
    spot_total = float(usdc["total"]) if usdc else 0.0
    spot_hold = float(usdc["hold"]) if usdc else 0.0
    spot_free_now = spot_total - spot_hold
    margin_for_new = crwv_actual_margin + xyz100_actual_margin
    # ZEC pull adds to free spot; new lines consume from it
    spot_free_proj = spot_free_now + args.zec_pull - margin_for_new

    # ── Render report ─────────────────────────────────────────────────────
    print("=" * 70)
    print("Book B pre-trade calculator (read-only by default)")
    print("=" * 70)
    print(f"wallet: {addr}")
    print()
    print("CURRENT STATE")
    print(f"  ZEC:    {float(zec_pos['szi']):.3f} contracts  marg=${zec_marg:.2f}  "
          f"liqPx=${zec_liq:.2f}  mark=${zec_mark:.2f}  buf={zec_buf_now:.2f}%")
    print(f"  spot USDC: total=${spot_total:.2f}  hold=${spot_hold:.2f}  "
          f"free=${spot_free_now:.4f}")
    print()
    print("PROPOSED ACTIONS")
    print(f"  1. Pull ${args.zec_pull:.2f} from ZEC isolated margin")
    print(f"     → ZEC marg: ${zec_marg:.2f} → ${zec_marg - args.zec_pull:.2f}")
    print(f"     → ZEC buf:  {zec_buf_now:.2f}% → ~{zec_buf_proj:.2f}%")
    print()
    print(f"  2. Open xyz:XYZ100 SHORT  (Normal Iso, max {xyz100_max_lev}x)")
    print(f"     mark=${xyz100_mark:.2f}  szDec={xyz100_szdec}")
    print(f"     target margin=${args.xyz100_margin:.2f} @ {args.leverage}x → "
          f"target notional=${xyz100_notional:.2f}")
    print(f"     size = {xyz100_size:.4f} contracts → "
          f"actual notional=${xyz100_actual_notional:.2f}  "
          f"actual margin=${xyz100_actual_margin:.2f}")
    print()
    print(f"  3. Open xyz:CRWV LONG  (Strict Iso, max {crwv_max_lev}x)")
    print(f"     mark=${crwv_mark:.2f}  szDec={crwv_szdec}")
    print(f"     target margin=${args.crwv_margin:.2f} @ {args.leverage}x → "
          f"target notional=${crwv_notional:.2f}")
    print(f"     size = {crwv_size:.2f} contracts → "
          f"actual notional=${crwv_actual_notional:.2f}  "
          f"actual margin=${crwv_actual_margin:.2f}")
    print()
    print("PROJECTED POST-TRADE")
    print(f"  spot free: ${spot_free_now:.2f} + ZEC pull ${args.zec_pull:.2f} "
          f"- new margin ${margin_for_new:.2f} = ${spot_free_proj:.2f}")
    print(f"  reserve floor: ${args.reserve_floor:.2f}")
    print()

    # ── Threshold checks ──────────────────────────────────────────────────
    failures = []
    if zec_buf_proj < ZEC_YELLOW_THRESHOLD_PCT:
        failures.append(
            f"ZEC buffer post-pull {zec_buf_proj:.2f}% < {ZEC_YELLOW_THRESHOLD_PCT}% YELLOW threshold"
        )
    if spot_free_proj < args.reserve_floor:
        failures.append(
            f"projected free spot ${spot_free_proj:.2f} < ${args.reserve_floor:.2f} reserve floor"
        )
    if crwv_size <= 0:
        failures.append(f"CRWV size rounds to 0 — increase --crwv-margin")
    if xyz100_size <= 0:
        failures.append(
            f"XYZ100 size rounds to 0 — increase --xyz100-margin"
        )

    if failures:
        print("THRESHOLD FAILURES:")
        for f in failures:
            print(f"  ❌ {f}")
        print()
        print("Refusing to commit any action. Adjust args and retry.")
        return 2
    else:
        print("All thresholds OK ✅")
        print()

    # ── Optional ZEC pull execution ───────────────────────────────────────
    if args.commit_zec_pull:
        if not args.i_confirm_book_b:
            print(
                "ERROR: --commit-zec-pull requires --i-confirm-book-b confirmation flag",
                file=sys.stderr,
            )
            return 1

        from eth_account import Account
        from hyperliquid.exchange import Exchange
        from hyperliquid.utils import constants

        pk = os.environ.get("HL_PRIVATE_KEY")
        if not pk:
            print("ERROR: HL_PRIVATE_KEY not set in .env", file=sys.stderr)
            return 1
        account = Account.from_key(pk)
        exchange = Exchange(
            account,
            constants.MAINNET_API_URL,
            account_address=addr,
        )

        print(
            f"COMMITTING ZEC pull: -${args.zec_pull:.2f} via "
            f"update_isolated_margin('ZEC', is_buy=True, amount=-{args.zec_pull})"
        )
        # SDK signature: update_isolated_margin(name, amount), where positive=add,
        # negative=remove. The is_buy flag in some SDK versions signals position
        # side; consult the installed SDK.
        try:
            result = exchange.update_isolated_margin(-args.zec_pull, "ZEC")
        except TypeError:
            # Older SDK signature variant
            result = exchange.update_isolated_margin("ZEC", -args.zec_pull)
        print(f"  result: {result}")
        if isinstance(result, dict) and result.get("status") == "ok":
            print("  ✅ ZEC pull executed")
            return 0
        else:
            print("  ❌ ZEC pull did NOT report status=ok — review result above")
            return 3
    else:
        print("Read-only mode: nothing executed.")
        print()
        print("Next steps (manual):")
        print("  - Verify XYZ100 margin mode in trade.xyz live UI (Normal Iso vs Cross)")
        print("  - If Normal Iso: re-run this script with --commit-zec-pull "
              "--i-confirm-book-b")
        print("  - Then place XYZ100 short FIRST, then CRWV long, via the existing")
        print("    close_*.py-style tooling with manual_order tagging")
        print("  - Then run scripts/book_b_posttrade_verify.py")
        return 0


if __name__ == "__main__":
    sys.exit(main())
