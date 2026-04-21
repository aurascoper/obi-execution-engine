#!/usr/bin/env python3
"""
mark_to_market.py — value actual on-chain open positions and reconcile against
the engine log's dangling entries. Phantom entries (logged but closed on-exchange
during detach) are reported as reconciliation drift.
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

from analyze_session import extract_fills, load_events, pair_round_trips

HL_INFO = "https://api.hyperliquid.xyz/info"
HIP3_DEXS = ["xyz", "flx", "vntl", "hyna", "km", "cash", "para", "abcd"]


def _load_env() -> None:
    env_path = Path("/Users/aurascoper/Developer/live_trading/.env")
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _post(body: dict) -> dict:
    req = urllib.request.Request(
        HL_INFO,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def fetch_spot_state(addr: str) -> dict:
    """Return unified spot collateral: USDC total + hold + free."""
    try:
        s = _post({"type": "spotClearinghouseState", "user": addr})
    except Exception as e:
        return {"error": str(e), "usdc_total": 0.0, "usdc_hold": 0.0, "usdc_free": 0.0}
    for b in s.get("balances", []):
        if b.get("coin") == "USDC":
            total = float(b.get("total", 0))
            hold = float(b.get("hold", 0))
            return {"usdc_total": total, "usdc_hold": hold, "usdc_free": total - hold}
    return {"usdc_total": 0.0, "usdc_hold": 0.0, "usdc_free": 0.0}


def fetch_portfolio_nav(addr: str) -> float:
    """Return latest unified accountValue from portfolio endpoint."""
    try:
        p = _post({"type": "portfolio", "user": addr})
        for window, data in p:
            if window == "day":
                hist = data.get("accountValueHistory", [])
                if hist:
                    return float(hist[-1][1])
    except Exception:
        pass
    return 0.0


def fetch_account_state(addr: str) -> dict[str, dict]:
    """
    Return {clearinghouse_label: {account_value, positions: {coin: {szi, entry_px, upnl}}}}.
    Native clearinghouse uses empty dex; HIP-3 uses dex name.
    """
    out: dict[str, dict] = {}
    for dex in [""] + HIP3_DEXS:
        try:
            body = {"type": "clearinghouseState", "user": addr}
            if dex:
                body["dex"] = dex
            s = _post(body)
        except Exception as e:
            out[dex or "native"] = {
                "error": str(e),
                "account_value": 0.0,
                "positions": {},
            }
            continue
        positions = {}
        for p in s.get("assetPositions", []):
            pos = p["position"]
            raw_coin = pos["coin"]
            coin_key = (
                raw_coin
                if (not dex or raw_coin.startswith(f"{dex}:"))
                else f"{dex}:{raw_coin}"
            )
            positions[coin_key] = {
                "szi": float(pos["szi"]),
                "entry_px": float(pos["entryPx"]) if pos.get("entryPx") else None,
                "upnl": float(pos.get("unrealizedPnl", 0)),
            }
        out[dex or "native"] = {
            "account_value": float(s.get("marginSummary", {}).get("accountValue", 0)),
            "withdrawable": float(s.get("withdrawable", 0)),
            "positions": positions,
        }
    return out


def main() -> None:
    _load_env()
    addr = os.environ.get("HL_WALLET_ADDRESS") or os.environ.get("HL_ACCOUNT_ADDRESS")
    if not addr:
        raise SystemExit("HL_WALLET_ADDRESS not set")

    state = fetch_account_state(addr)
    spot = fetch_spot_state(addr)
    nav = fetch_portfolio_nav(addr)

    print("=" * 78)
    print("UNIFIED ACCOUNT (authoritative)")
    print("=" * 78)
    print(f"  portfolio NAV       ${nav:>10.2f}")
    print(f"  spot USDC total     ${spot['usdc_total']:>10.2f}")
    print(
        f"  spot USDC hold      ${spot['usdc_hold']:>10.2f}  (margin backing open perps)"
    )
    print(f"  spot USDC free      ${spot['usdc_free']:>10.2f}")

    print()
    print("=" * 78)
    print("PER-DEX MARGIN ATTRIBUTION (sums do NOT equal unified NAV)")
    print("=" * 78)
    for label, info in state.items():
        if "error" in info:
            print(f"  {label:<10} ERROR: {info['error']}")
            continue
        av = info["account_value"]
        print(
            f"  {label:<10} accountValue=${av:>10.2f}  withdrawable=${info['withdrawable']:>10.2f}  positions={len(info['positions'])}"
        )

    print()
    print("=" * 78)
    print("LIVE POSITIONS (on-chain)")
    print("=" * 78)
    live = {}
    for label, info in state.items():
        for coin, pos in info.get("positions", {}).items():
            live[coin] = pos
            print(
                f"  {coin:<22} szi={pos['szi']:>+12.4f}  entry={pos['entry_px'] or 0:>11.4f}  upnl=${pos['upnl']:>+8.2f}"
            )
    total_upnl = sum(p["upnl"] for p in live.values())
    print(f"  {'TOTAL UNREALIZED':<22} ${total_upnl:+.2f}")

    print()
    print("=" * 78)
    print("RECONCILIATION: engine-log dangling entries vs. on-chain state")
    print("=" * 78)
    events = load_events()
    fills = extract_fills(events)
    _, dangling = pair_round_trips(fills)

    logged_entries = {}
    for f in dangling:
        if f.get("role") != "entry":
            continue
        coin = f["symbol"].split("/")[0]
        logged_entries.setdefault(coin, []).append(f)

    phantoms = []
    matched = []
    for coin, entries in logged_entries.items():
        live_pos = live.get(coin)
        total_logged_szi = sum(f["qty_signed"] for f in entries)
        if live_pos is None or abs(live_pos["szi"]) < 1e-9:
            phantoms.append((coin, total_logged_szi, entries))
        else:
            matched.append((coin, total_logged_szi, live_pos["szi"], live_pos))

    if matched:
        print("Matched (logged entry aligns with live position):")
        for coin, log_szi, live_szi, pos in matched:
            drift = live_szi - log_szi
            flag = "" if abs(drift) < 1e-4 else f"  DRIFT={drift:+.4f}"
            print(
                f"  {coin:<22} log_szi={log_szi:>+10.4f}  live_szi={live_szi:>+10.4f}  upnl=${pos['upnl']:+.2f}{flag}"
            )

    if phantoms:
        print()
        print("Phantom entries (logged but no live position — closed during detach):")
        for coin, log_szi, entries in phantoms:
            px = entries[0].get("price")
            print(
                f"  {coin:<22} log_szi={log_szi:>+10.4f}  entry_px={px}  (PnL unknown, exit fill missing from log)"
            )

    # Coins live on-chain but not in logged dangling entries
    unlogged = [c for c in live if c not in logged_entries]
    if unlogged:
        print()
        print("Live positions not in engine log (opened before log window):")
        for coin in unlogged:
            pos = live[coin]
            print(
                f"  {coin:<22} szi={pos['szi']:>+10.4f}  entry={pos['entry_px']}  upnl=${pos['upnl']:+.2f}"
            )


if __name__ == "__main__":
    main()
