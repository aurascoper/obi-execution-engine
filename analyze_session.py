#!/usr/bin/env python3
"""
analyze_session.py — compute realized session P&L from hl_engine.jsonl.

Log schema reality:
  * fill_recorded has signed qty + side + role, NO price field.
  * hl_order_result carries price at result.response.data.statuses[0].filled.avgPx
    and is logged immediately before fill_recorded (same submission).
  * We join fill → result by walking entries in timestamp order: each
    fill_recorded inherits price from the most recent hl_order_result whose
    client_order_id matches the fill's (symbol, tag) and whose statuses[0]
    has a "filled" key.

Fees: HL taker = 3.5 bps per side → charge abs(notional) * 0.00035 on each fill.

A "round trip" = consecutive entry+exit pair per symbol. If the session ends
with an unclosed entry, it's reported as OPEN and excluded from realized net.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

LOG = Path("/Users/aurascoper/Developer/live_trading/logs/hl_engine.jsonl")
TAKER_FEE_BPS = 3.5  # per side
STRATEGY_TAGS = ("hl_z", "hl_taker_z")


def _fee(notional: float) -> float:
    return abs(notional) * (TAKER_FEE_BPS / 10_000.0)


def load_events() -> list[dict]:
    events = []
    for line in LOG.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return events


def extract_fills(events: list[dict]) -> list[dict]:
    """
    Walk events in order. Whenever we see hl_order_result with a filled status,
    cache (symbol_from_cid, price, totalSz). When the very next fill_recorded
    matches, attach the price.
    """
    fills: list[dict] = []
    pending_result: dict | None = None

    for ev in events:
        et = ev.get("event")

        if et == "hl_order_result":
            cid = ev.get("client_order_id", "")
            statuses = (
                ev.get("result", {})
                .get("response", {})
                .get("data", {})
                .get("statuses", [])
            )
            for s in statuses:
                if isinstance(s, dict) and "filled" in s:
                    f = s["filled"]
                    try:
                        px = float(f.get("avgPx"))
                        sz = float(f.get("totalSz"))
                    except (TypeError, ValueError):
                        continue
                    if sz <= 0:
                        continue
                    # cid format: hl_z_{COIN}_{epoch} (or legacy hl_taker_z_...)
                    pending_result = {
                        "cid": cid,
                        "price": px,
                        "size": sz,
                        "oid": f.get("oid"),
                        "ts": ev.get("timestamp"),
                    }
                    break
            continue

        if et == "fill_recorded" and pending_result is not None:
            # fill_recorded.qty is POST-fill net position (0.0 on exits, signed
            # on entries), not fill size. Trust hl_order_result.totalSz for
            # magnitude and fill_recorded.side for sign.
            side = ev.get("side", "")
            sign = 1.0 if side == "buy" else -1.0
            qty_signed = sign * pending_result["size"]
            fills.append({
                "ts": ev.get("timestamp"),
                "symbol": ev.get("symbol"),
                "tag": ev.get("tag"),
                "role": ev.get("role"),
                "side": side,
                "qty_signed": qty_signed,
                "price": pending_result["price"],
                "oid": pending_result["oid"],
                "cid": pending_result["cid"],
            })
            pending_result = None

    return fills


def pair_round_trips(fills: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Per-symbol FIFO: each entry opens a round trip; the next exit for that
    symbol closes it. Any trailing open entry is reported separately.
    """
    open_by_sym: dict[str, dict] = {}
    trips: list[dict] = []
    unpaired: list[dict] = []

    for f in fills:
        sym = f["symbol"]
        role = f["role"]
        if role == "entry":
            if sym in open_by_sym:
                unpaired.append(open_by_sym[sym])
            open_by_sym[sym] = f
        elif role == "exit":
            entry = open_by_sym.pop(sym, None)
            if entry is None:
                unpaired.append(f)
                continue
            entry_notional = entry["qty_signed"] * entry["price"]
            exit_notional  = f["qty_signed"] * f["price"]
            # Close identity: signed qty flips sign on exit, so summing signed
            # notionals gives realized cash. Short: entry_qty<0 → entry_notional<0
            # (cash received), exit_qty>0 → exit_notional>0 (cash paid).
            # PnL = -(entry_notional + exit_notional).
            gross = -(entry_notional + exit_notional)
            fees  = _fee(entry_notional) + _fee(exit_notional)
            net   = gross - fees
            direction = "long" if entry["qty_signed"] > 0 else "short"
            trips.append({
                "symbol": sym,
                "direction": direction,
                "qty": abs(entry["qty_signed"]),
                "entry_px": entry["price"],
                "exit_px":  f["price"],
                "entry_ts": entry["ts"],
                "exit_ts":  f["ts"],
                "gross": gross,
                "fees":  fees,
                "net":   net,
            })

    return trips, list(open_by_sym.values()) + unpaired


def main() -> None:
    events = load_events()
    fills  = extract_fills(events)
    trips, open_or_unpaired = pair_round_trips(fills)

    print(f"{'─'*78}")
    print(f"HL Engine Session Analyzer")
    print(f"{'─'*78}")
    print(f"Events:  {len(events):>6}")
    print(f"Fills:   {len(fills):>6}")
    print(f"Trips:   {len(trips):>6}  (closed round-trips)")
    print(f"Dangling:{len(open_or_unpaired):>6}  (open entries or orphan exits)")

    if not trips:
        print("\nNo closed round-trips.")
        return

    print(f"\n{'─'*78}")
    print(f"{'#':>3}  {'sym':<8} {'dir':<6} {'qty':>10} "
          f"{'entry':>11} {'exit':>11} {'gross':>9} {'fees':>7} {'net':>9}")
    print(f"{'─'*78}")

    by_sym: dict[str, list[float]] = defaultdict(list)
    cum = 0.0
    wins = losses = 0
    gross_sum = fees_sum = 0.0

    for i, t in enumerate(trips, 1):
        cum += t["net"]
        gross_sum += t["gross"]
        fees_sum  += t["fees"]
        by_sym[t["symbol"]].append(t["net"])
        if t["net"] > 0:
            wins += 1
        else:
            losses += 1
        print(
            f"{i:>3}  {t['symbol']:<8} {t['direction']:<6} "
            f"{t['qty']:>10.4f} "
            f"{t['entry_px']:>11.2f} {t['exit_px']:>11.2f} "
            f"{t['gross']:>+9.3f} {t['fees']:>7.3f} {t['net']:>+9.3f}"
        )

    print(f"{'─'*78}")
    print(f"Totals:")
    print(f"  gross      : ${gross_sum:+.3f}")
    print(f"  fees       : ${fees_sum:.3f}")
    print(f"  NET        : ${cum:+.3f}")
    print(f"  win/loss   : {wins}W / {losses}L  "
          f"(hit={wins/max(1,wins+losses)*100:.1f}%)")
    if trips:
        avg = cum / len(trips)
        print(f"  avg/trip   : ${avg:+.4f}")

    print(f"\nBy symbol:")
    for sym, pnls in sorted(by_sym.items()):
        tot = sum(pnls)
        print(f"  {sym:<8} n={len(pnls):>3}  net=${tot:+.3f}  "
              f"avg=${tot/len(pnls):+.4f}")

    if open_or_unpaired:
        print(f"\nDangling fills (not counted in net):")
        for f in open_or_unpaired:
            print(f"  {f.get('ts','?')}  {f.get('symbol','?'):<8} "
                  f"role={f.get('role','?')} qty={f.get('qty_signed',0):+.4f} "
                  f"@ {f.get('price',0):.2f}")


if __name__ == "__main__":
    main()
