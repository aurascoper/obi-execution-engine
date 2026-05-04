#!/usr/bin/env python3
"""Doc-based funding test for Book A unified mode.

Per HL docs (https://hyperliquid.gitbook.io/hyperliquid-docs/trading/account-abstraction-modes):
  - Unified mode: ONE balance per collateral asset, unified spot+perp
  - USDC funds: native HL perps + xyz HIP-3 perps + USDC spot
  - USDH funds: km/flx/vntl HIP-3 perps + USDH spot

Book A subaccount is in mode=unifiedAccount with $250 USDC, $0 USDH.
Per docs, that should be trade-capable for native + xyz, and NOT trade-capable
for vntl/km/flx.

This script tests that prediction with three tiny Alo orders far-from-mid:
  - Native BTC perp Alo buy at ~50% below mid → expected ACCEPT (then cancel)
  - xyz:NVDA Alo buy at ~50% below mid       → expected ACCEPT (then cancel)
  - vntl:MAG7 Alo buy at ~50% below mid      → expected REJECT (no USDH)

Alo (post-only) at 50% below mid will not fill — it just rests on the book.
Acceptance proves margin was sufficient at submit time. Any accepted order is
canceled immediately after.

Signs with the Book A agent key from .env.bookA. Routes to Book A via
vault_address. NEVER touches master.

Defaults to --dry-run. Execute requires:
    --execute --i-confirm-tiny-probes
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants


# ── Constants ──────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
ENV_BOOKA = ROOT / ".env.bookA"
BOOKA_ADDR = "0xdae99e77b9859a1526782e3815253e8f09c1f2ef"

# Test universe: one symbol per collateral asset
PROBES = [
    {"coin": "BTC",        "dex": None,   "label": "native HL",
     "expectation": "ACCEPT (USDC funds native)"},
    {"coin": "xyz:NVDA",   "dex": "xyz",  "label": "xyz HIP-3",
     "expectation": "ACCEPT (USDC funds xyz)"},
    {"coin": "vntl:MAG7",  "dex": "vntl", "label": "vntl HIP-3",
     "expectation": "REJECT — no USDH (control)"},
]

NOTIONAL_TARGET_USD = 12.0   # ~$12 per probe, above $10 venue minimum
ALO_OFFSET_FRACTION = 0.50   # buy at 50% below mid → won't fill
RESULT_LOG = ROOT / "logs" / "book_a_probes.jsonl"


# ── Helpers ────────────────────────────────────────────────────────────────

def round_px_for_hl(px: float, sz_decimals: int) -> float:
    """HL price precision: <= 6-szDecimals decimals AND <= 5 sig figs."""
    max_decimals = max(0, 6 - sz_decimals)
    rounded = round(px, max_decimals)
    sig_str = f"{rounded:.5g}"
    return float(sig_str)


def round_sz(sz: float, sz_decimals: int) -> float:
    return round(sz, sz_decimals)


def get_meta_for_dex(info: Info, dex: str | None) -> dict:
    """Return {coin: {szDecimals, ...}} for the given dex (None = native)."""
    if dex:
        meta = info.post("/info", {"type": "meta", "dex": dex})
    else:
        meta = info.meta()
    return {u["name"]: u for u in meta.get("universe", [])}


def get_mid(info: Info, coin: str, dex: str | None) -> float | None:
    """L2 mid for the symbol."""
    try:
        if dex:
            book = info.post("/info", {"type": "l2Book", "coin": coin, "dex": dex})
        else:
            book = info.post("/info", {"type": "l2Book", "coin": coin})
        bids, asks = book["levels"][0], book["levels"][1]
        if not bids or not asks:
            return None
        return (float(bids[0]["px"]) + float(asks[0]["px"])) / 2
    except Exception as e:
        print(f"  WARN: mid fetch err for {coin}: {e}", file=sys.stderr)
        return None


def classify_response(resp: dict) -> tuple[str, str, int | None]:
    """Return (verdict, detail, oid_if_resting)."""
    if not isinstance(resp, dict):
        return ("UNKNOWN", f"non-dict response: {resp!r}", None)
    if resp.get("status") != "ok":
        return ("REJECT", f"top-level status={resp.get('status')}: {resp}", None)
    try:
        statuses = resp["response"]["data"]["statuses"]
    except (KeyError, TypeError):
        return ("UNKNOWN", f"unexpected shape: {resp}", None)
    if not statuses:
        return ("UNKNOWN", "empty statuses", None)
    s = statuses[0]
    if "resting" in s:
        return ("ACCEPT", f"resting oid={s['resting'].get('oid')}", s["resting"].get("oid"))
    if "filled" in s:
        return ("ACCEPT", f"filled (unexpected for far-from-mid Alo): {s['filled']}", None)
    if "error" in s:
        return ("REJECT", s["error"], None)
    return ("UNKNOWN", str(s), None)


def render_dry_run(probes_with_marks: list[dict]) -> None:
    print("=" * 78)
    print("DRY RUN — no orders sent")
    print("=" * 78)
    print(f"\nSigner: Book A agent (from {ENV_BOOKA})")
    print(f"Vault routing: account_address={BOOKA_ADDR}, vault_address={BOOKA_ADDR}")
    print(f"Result log: {RESULT_LOG}")
    print(f"\nIntended probes (each Alo buy ~{NOTIONAL_TARGET_USD:.0f} USD notional, "
          f"~{int(ALO_OFFSET_FRACTION*100)}% below mid → won't fill):\n")
    for p in probes_with_marks:
        print(f"  [{p['label']:12s}] {p['coin']:15s}")
        print(f"    expectation : {p['expectation']}")
        if p.get("error"):
            print(f"    SKIP (mid err): {p['error']}")
            continue
        print(f"    mid         : ${p['mid']:.6g}")
        print(f"    limit_px    : ${p['limit_px']:.6g}  (szDecimals={p['sz_decimals']})")
        print(f"    qty         : {p['qty']}  (notional ≈ ${p['qty']*p['limit_px']:.2f})")
    print()
    print("To execute for real, re-run with:")
    print("  --execute --i-confirm-tiny-probes")


def execute_probes(
    ex: Exchange,
    info: Info,
    probes_with_marks: list[dict],
) -> list[dict]:
    """Place each probe, classify response, cancel if accepted. Return result list."""
    results: list[dict] = []
    for p in probes_with_marks:
        result = {
            "label": p["label"],
            "coin": p["coin"],
            "dex": p["dex"],
            "expectation": p["expectation"],
            "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if p.get("error"):
            result["verdict"] = "SKIP"
            result["detail"] = p["error"]
            results.append(result)
            print(f"  [{p['label']}] SKIP: {p['error']}")
            continue

        result.update({
            "mid": p["mid"],
            "limit_px": p["limit_px"],
            "qty": p["qty"],
        })

        print(f"\n  [{p['label']}] submitting Alo buy {p['qty']} {p['coin']} @ ${p['limit_px']:.6g}")
        try:
            resp = ex.order(
                p["coin"],
                True,                       # is_buy
                p["qty"],                   # sz
                p["limit_px"],              # limit_px
                {"limit": {"tif": "Alo"}},  # post-only
                False,                      # reduce_only
            )
        except Exception as e:
            result["verdict"] = "ERROR"
            result["detail"] = f"SDK exception: {e!r}"
            results.append(result)
            print(f"    ERROR: {e!r}")
            continue

        verdict, detail, oid = classify_response(resp)
        result["verdict"] = verdict
        result["detail"] = detail
        result["raw_response"] = resp

        match_marker = "✓ matches expectation" if (
            (verdict == "ACCEPT" and "ACCEPT" in p["expectation"]) or
            (verdict == "REJECT" and "REJECT" in p["expectation"])
        ) else "✗ does NOT match expectation"
        print(f"    verdict: {verdict}  ({match_marker})")
        print(f"    detail : {detail}")

        # Cancel any accepted order so we don't leave it resting
        if verdict == "ACCEPT" and oid is not None:
            print(f"    canceling resting oid={oid} ...")
            try:
                cancel_resp = ex.cancel(p["coin"], oid)
                print(f"    cancel resp: {cancel_resp}")
                result["cancel_response"] = cancel_resp
            except Exception as e:
                print(f"    WARN: cancel err: {e!r}")
                result["cancel_error"] = repr(e)

        results.append(result)

    return results


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--execute", action="store_true",
                    help="actually submit orders (default: dry-run)")
    ap.add_argument("--i-confirm-tiny-probes", action="store_true",
                    dest="i_confirm",
                    help="required confirmation flag for --execute")
    args = ap.parse_args()

    # Load Book A agent
    if not ENV_BOOKA.exists():
        print(f"ERROR: {ENV_BOOKA} not found", file=sys.stderr)
        return 1
    load_dotenv(dotenv_path=ENV_BOOKA, override=True)
    agent_pk = os.getenv("subaccount_agent_pk")
    if not agent_pk:
        print(f"ERROR: subaccount_agent_pk not in {ENV_BOOKA}", file=sys.stderr)
        return 1
    if not agent_pk.startswith("0x"):
        agent_pk = "0x" + agent_pk
    acct = Account.from_key(agent_pk)
    print(f"# Book A agent address: {acct.address}")
    print(f"# Targeting subaccount : {BOOKA_ADDR}")

    # Compute marks + sizes for each probe
    perp_dexs = [""] + sorted({p["dex"] for p in PROBES if p["dex"]})
    info = Info(constants.MAINNET_API_URL, skip_ws=True, perp_dexs=perp_dexs)

    probes_with_marks: list[dict] = []
    for p in PROBES:
        out = dict(p)
        meta_map = get_meta_for_dex(info, p["dex"])
        u = meta_map.get(p["coin"])
        if not u:
            out["error"] = f"coin {p['coin']} not in {p['dex'] or 'native'} meta"
            probes_with_marks.append(out)
            continue
        sz_dec = int(u["szDecimals"])
        out["sz_decimals"] = sz_dec

        mid = get_mid(info, p["coin"], p["dex"])
        if mid is None:
            out["error"] = "mid fetch failed"
            probes_with_marks.append(out)
            continue
        out["mid"] = mid

        target_px = mid * (1 - ALO_OFFSET_FRACTION)
        limit_px = round_px_for_hl(target_px, sz_dec)
        qty_raw = NOTIONAL_TARGET_USD / limit_px
        qty = round_sz(qty_raw, sz_dec)
        # If qty rounds to zero (very high-priced asset + small notional + low szDecimals),
        # bump to one tick.
        if qty <= 0:
            qty = 10 ** (-sz_dec)
        out["limit_px"] = limit_px
        out["qty"] = qty
        probes_with_marks.append(out)

    if not args.execute:
        render_dry_run(probes_with_marks)
        return 0

    if not args.i_confirm:
        print("ERROR: --execute requires --i-confirm-tiny-probes", file=sys.stderr)
        return 2

    # Build Exchange routed to Book A
    ex = Exchange(
        acct,
        constants.MAINNET_API_URL,
        account_address=BOOKA_ADDR,
        vault_address=BOOKA_ADDR,
        perp_dexs=perp_dexs,
    )

    print("\n" + "=" * 78)
    print("EXECUTING — placing 3 tiny Alo probes (far-from-mid, won't fill)")
    print("=" * 78)
    results = execute_probes(ex, info, probes_with_marks)

    # Persist
    RESULT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with RESULT_LOG.open("a", buffering=1) as fh:
        for r in results:
            fh.write(json.dumps(r, default=str) + "\n")

    # Summary
    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    for r in results:
        marker = "✓" if (
            (r["verdict"] == "ACCEPT" and "ACCEPT" in r["expectation"]) or
            (r["verdict"] == "REJECT" and "REJECT" in r["expectation"])
        ) else ("·" if r["verdict"] == "SKIP" else "✗")
        print(f"  {marker} {r['label']:12s}  expected: {r['expectation']:30s}  got: {r['verdict']}")
    print(f"\nFull results: {RESULT_LOG}")

    # Diagnosis verdict
    n = {"native HL": None, "xyz HIP-3": None, "vntl HIP-3": None}
    for r in results:
        n[r["label"]] = r["verdict"]
    print("\nDoc-based diagnosis verdict:")
    if n["native HL"] == "ACCEPT" and n["xyz HIP-3"] == "ACCEPT" and n["vntl HIP-3"] == "REJECT":
        print("  ✓ ALL THREE MATCH — unified-USDC funds native+xyz, USDH gates vntl")
        print("    Next: relaunch Book A engine with USDC-only universe (native + xyz)")
    elif n["native HL"] == "ACCEPT" and n["xyz HIP-3"] == "ACCEPT":
        print("  ◐ native+xyz both accept (good); vntl behaved unexpectedly")
    elif n["native HL"] == "REJECT":
        print("  ✗ native rejected — doc model is INCOMPLETE for this account")
        print("    DO NOT relaunch. Investigate further.")
    else:
        print("  ?  mixed result — manual review needed")

    return 0


if __name__ == "__main__":
    sys.exit(main())
