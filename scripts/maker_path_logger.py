#!/usr/bin/env python3
"""Maker-path lifecycle aggregator (Commit 1, Roadmap item #1).

Read-only replay tool. Joins per-cloid events from logs/hl_engine.jsonl
into one structured lifecycle record per maker order, then writes them
to logs/maker_lifecycle.jsonl.

Joined event shape (per cloid):
    hl_maker_intent              pre-submission record
    hl_maker_result              submission response (resting oid)
    hl_fill_received             zero or more fills (closed_pnl, fee, hash)
    hl_order_cancelled_by_cloid  explicit cancel (latency)
    hl_maker_giveup              timed-out maker (age, reprice_count)
    hl_maker_giveup_cancel_unconfirmed  edge case

For taker (IOC) orders, cloid is null in hl_order_submitted; those are
handled separately by joining hl_order_submitted with the most recent
hl_fill_received on (symbol, side, oid).

Output schema (one JSON per record):
    cloid, oid, symbol, side, intent (maker|taker), tif, tag,
    submit_ts_ms, fill_ts_ms (first), last_fill_ts_ms, cancel_ts_ms,
    giveup_ts_ms, lifetime_ms, intended_qty, intended_px, notional,
    fills: [{ts_ms, px, sz, fee, closed_pnl, hash, crossed}],
    filled_qty, canceled_qty, total_fee, total_closed_pnl,
    fill_count, cancel_reason, reprice_count,
    terminal_state (filled | canceled | giveup | partial_filled | open),
    is_entry, is_hip3

Top-5 LOB depth + queue-rank proxy + queue imbalance + cancel/add
ratios are NOT in this scaffold — they require either L2 snapshot
logging (paired Commit 1b) or rolling counts (post-aggregation).

Usage:
    venv/bin/python3 scripts/maker_path_logger.py
    venv/bin/python3 scripts/maker_path_logger.py --since 2026-04-26T23:00:00Z
    venv/bin/python3 scripts/maker_path_logger.py --out /tmp/lifecycle.jsonl
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / "hl_engine.jsonl"
DEFAULT_OUT = ROOT / "logs" / "maker_lifecycle.jsonl"


def _parse_ts(s) -> int:
    if isinstance(s, (int, float)):
        return int(s * 1000) if s < 1e12 else int(s)
    if isinstance(s, str):
        try:
            x = s[:-1] + "+00:00" if s.endswith("Z") else s
            return int(dt.datetime.fromisoformat(x).timestamp() * 1000)
        except Exception:
            return 0
    return 0


def _norm(s: str) -> str:
    return (s or "").replace("/USD", "").replace("/USDC", "")


def _resting_oid(result: dict) -> int | None:
    """Pull oid from the resting/filled status nested in hl_maker_result."""
    try:
        statuses = (
            result.get("response", {}).get("data", {}).get("statuses", [])
            if isinstance(result, dict)
            else []
        )
        for st in statuses:
            for key in ("resting", "filled"):
                if key in st and "oid" in st[key]:
                    return int(st[key]["oid"])
    except Exception:
        pass
    return None


# ── Event interest set ────────────────────────────────────────────────────
WANTED_EVENTS = frozenset(
    [
        "hl_maker_intent",
        "hl_maker_result",
        "hl_order_submitted",
        "hl_order_cancelled_by_cloid",
        "hl_fill_received",
        "hl_maker_giveup",
        "hl_maker_giveup_cancel_unconfirmed",
    ]
)


def aggregate(log_path: Path, since_ms: int = 0) -> tuple[list[dict], dict]:
    """Walk log forward, join events per cloid (and per-oid for taker fills).

    Returns (records, stats).
    """
    by_cloid: dict[str, dict] = defaultdict(
        lambda: {
            "fills": [],
            "events_seen": [],
        }
    )
    # For taker IOC orders without cloid, key by oid (resolved at fill time)
    pending_taker_submits: list[dict] = []
    fills_no_cloid: list[dict] = []

    n_total = 0
    n_skipped_no_event = 0
    n_skipped_pre_since = 0

    with log_path.open() as fh:
        for line in fh:
            # Cheap pre-filter — most events don't include any of these names
            if not any(
                e in line
                for e in (
                    '"hl_maker_intent"',
                    '"hl_maker_result"',
                    '"hl_order_submitted"',
                    '"hl_order_cancelled_by_cloid"',
                    '"hl_fill_received"',
                    '"hl_maker_giveup',
                )
            ):
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            ev = o.get("event")
            if ev not in WANTED_EVENTS:
                n_skipped_no_event += 1
                continue
            ts_ms = _parse_ts(o.get("timestamp", ""))
            if ts_ms < since_ms:
                n_skipped_pre_since += 1
                continue
            n_total += 1
            cloid = o.get("cloid")
            sym = _norm(o.get("symbol") or o.get("coin") or "")

            if ev == "hl_maker_intent":
                if not cloid:
                    continue
                rec = by_cloid[cloid]
                rec.update(
                    {
                        "cloid": cloid,
                        "client_order_id": o.get("client_order_id"),
                        "symbol": sym,
                        "side": o.get("side"),
                        "intent": "maker",
                        "intended_qty": o.get("qty"),
                        "intended_px": o.get("limit_px"),
                        "notional": o.get("notional"),
                        "submit_ts_ms": ts_ms,
                        "tag": o.get("client_order_id", "").split("_")[0]
                        if o.get("client_order_id")
                        else None,
                    }
                )
                rec["events_seen"].append(("hl_maker_intent", ts_ms))
            elif ev == "hl_maker_result":
                if not cloid:
                    continue
                rec = by_cloid[cloid]
                rec["oid"] = _resting_oid(o.get("result") or {})
                rec["result_status"] = (o.get("result") or {}).get("status")
                rec["events_seen"].append(("hl_maker_result", ts_ms))
            elif ev == "hl_order_submitted":
                if cloid:
                    rec = by_cloid[cloid]
                    rec.setdefault("symbol", sym)
                    rec.setdefault("side", o.get("side"))
                    rec.setdefault("submit_ts_ms", ts_ms)
                    rec["intent"] = (
                        "maker" if str(o.get("tif", "")).lower() == "alo" else "taker"
                    )
                    rec["tif"] = o.get("tif")
                    rec["tag"] = o.get("tag")
                    rec.setdefault("intended_qty", o.get("qty"))
                    rec.setdefault("intended_px", o.get("limit_px"))
                    rec["reduce_only"] = o.get("reduce_only")
                    rec["events_seen"].append(("hl_order_submitted", ts_ms))
                else:
                    # taker IOC — match later via (sym, side, time) heuristic
                    pending_taker_submits.append(
                        {
                            "ts_ms": ts_ms,
                            "symbol": sym,
                            "side": o.get("side"),
                            "qty": o.get("qty"),
                            "limit_px": o.get("limit_px"),
                            "tif": o.get("tif"),
                            "tag": o.get("tag"),
                            "reduce_only": o.get("reduce_only"),
                        }
                    )
            elif ev == "hl_fill_received":
                fill = {
                    "ts_ms": ts_ms,
                    "px": o.get("px"),
                    "sz": o.get("sz"),
                    "fee": o.get("fee"),
                    "closed_pnl": o.get("closed_pnl"),
                    "hash": o.get("hash"),
                    "crossed": o.get("crossed"),
                    "oid": o.get("oid"),
                }
                if cloid:
                    rec = by_cloid[cloid]
                    rec.setdefault("symbol", sym)
                    rec.setdefault("side", o.get("side"))
                    rec["fills"].append(fill)
                    rec["events_seen"].append(("hl_fill_received", ts_ms))
                else:
                    fills_no_cloid.append(
                        {**fill, "symbol": sym, "side": o.get("side")}
                    )
            elif ev == "hl_order_cancelled_by_cloid":
                if not cloid:
                    continue
                rec = by_cloid[cloid]
                rec["cancel_ts_ms"] = ts_ms
                rec["cancel_latency_ms"] = o.get("latency_ms")
                rec["events_seen"].append(("hl_order_cancelled_by_cloid", ts_ms))
            elif ev in ("hl_maker_giveup", "hl_maker_giveup_cancel_unconfirmed"):
                if not cloid:
                    continue
                rec = by_cloid[cloid]
                rec["giveup_ts_ms"] = ts_ms
                rec["cancel_reason"] = o.get("reason") or ev
                rec["reprice_count"] = o.get("reprice_count")
                rec["age_s"] = o.get("age_s")
                rec.setdefault("is_entry", o.get("is_entry"))
                rec.setdefault("is_hip3", o.get("is_hip3"))
                rec["events_seen"].append((ev, ts_ms))

    # Match cloid-less taker fills to taker submits by (sym, side, near time)
    # Coarse heuristic — fine for shadow analysis, not a hard join.
    for fill in fills_no_cloid:
        match = None
        match_dt = 60_000  # 60s window
        for sub in pending_taker_submits:
            if sub.get("symbol") != fill.get("symbol"):
                continue
            if sub.get("side") != fill.get("side"):
                continue
            d = abs(fill["ts_ms"] - sub["ts_ms"])
            if d <= match_dt:
                match = sub
                match_dt = d
        synth_key = f"taker_{fill.get('symbol')}_{fill.get('oid') or fill['ts_ms']}"
        rec = by_cloid[synth_key]
        rec.setdefault("cloid", None)
        rec.setdefault("oid", fill.get("oid"))
        rec.setdefault("symbol", fill.get("symbol"))
        rec.setdefault("side", fill.get("side"))
        rec.setdefault("intent", "taker")
        if match:
            rec.setdefault("submit_ts_ms", match["ts_ms"])
            rec.setdefault("intended_qty", match.get("qty"))
            rec.setdefault("intended_px", match.get("limit_px"))
            rec.setdefault("tif", match.get("tif"))
            rec.setdefault("tag", match.get("tag"))
            rec.setdefault("reduce_only", match.get("reduce_only"))
        rec["fills"].append(fill)
        rec["events_seen"].append(("hl_fill_received", fill["ts_ms"]))

    # Finalize each record: derived fields + terminal state.
    records = []
    n_filled = n_canceled = n_giveup = n_partial = n_open = 0
    for key, r in by_cloid.items():
        # Backfill cloid from dict key when no hl_maker_intent populated it.
        # Synthetic taker_* keys stay None (legitimate cloid-less taker fill).
        if not r.get("cloid") and not key.startswith("taker_"):
            r["cloid"] = key
        fills = r.get("fills") or []
        # Intent inference: if any fill is crossed=True and we have no
        # explicit hl_maker_intent / hl_order_submitted (tif=Alo), this is
        # a taker order (shock_ratchet daemon, manual close-half script,
        # or any external place_order). Default "maker" was wrong for
        # these — fix here.
        if r.get("intent") in (None, "maker") and fills:
            tif = (r.get("tif") or "").lower()
            if tif != "alo":
                if any(f.get("crossed") is True for f in fills):
                    r["intent"] = "taker"
        filled_qty = 0.0
        total_fee = 0.0
        total_closed_pnl = 0.0
        last_fill_ts_ms = 0
        for f in fills:
            try:
                filled_qty += float(f.get("sz", 0) or 0)
                total_fee += float(f.get("fee", 0) or 0)
                total_closed_pnl += float(f.get("closed_pnl", 0) or 0)
                last_fill_ts_ms = max(last_fill_ts_ms, int(f.get("ts_ms", 0) or 0))
            except (TypeError, ValueError):
                pass
        intended = float(r.get("intended_qty") or 0)
        canceled_qty = (
            max(0.0, intended - filled_qty)
            if r.get("cancel_ts_ms") or r.get("giveup_ts_ms")
            else 0.0
        )

        # Terminal state classification
        if r.get("cancel_ts_ms") or r.get("giveup_ts_ms"):
            if filled_qty == 0:
                terminal = "canceled" if r.get("cancel_ts_ms") else "giveup"
            else:
                terminal = "partial_filled"
        elif filled_qty > 0 and intended > 0 and filled_qty >= intended * 0.999:
            terminal = "filled"
        elif filled_qty > 0:
            terminal = "partial_filled"
        else:
            terminal = "open"
        if terminal == "filled":
            n_filled += 1
        elif terminal == "canceled":
            n_canceled += 1
        elif terminal == "giveup":
            n_giveup += 1
        elif terminal == "partial_filled":
            n_partial += 1
        else:
            n_open += 1

        # Lifetime
        submit_ts = r.get("submit_ts_ms")
        end_ts = (
            r.get("cancel_ts_ms") or r.get("giveup_ts_ms") or last_fill_ts_ms or None
        )
        lifetime_ms = (end_ts - submit_ts) if (submit_ts and end_ts) else None

        record = {
            "cloid": r.get("cloid"),
            "oid": r.get("oid") or (fills[0].get("oid") if fills else None),
            "symbol": r.get("symbol"),
            "side": r.get("side"),
            "intent": r.get("intent", "maker"),
            "tif": r.get("tif"),
            "tag": r.get("tag"),
            "client_order_id": r.get("client_order_id"),
            "is_entry": r.get("is_entry"),
            "is_hip3": r.get("is_hip3"),
            "reduce_only": r.get("reduce_only"),
            "submit_ts_ms": submit_ts,
            "fill_ts_ms_first": fills[0]["ts_ms"] if fills else None,
            "last_fill_ts_ms": last_fill_ts_ms or None,
            "cancel_ts_ms": r.get("cancel_ts_ms"),
            "giveup_ts_ms": r.get("giveup_ts_ms"),
            "lifetime_ms": lifetime_ms,
            "intended_qty": r.get("intended_qty"),
            "intended_px": r.get("intended_px"),
            "notional": r.get("notional"),
            "filled_qty": round(filled_qty, 8),
            "canceled_qty": round(canceled_qty, 8),
            "total_fee": round(total_fee, 6),
            "total_closed_pnl": round(total_closed_pnl, 6),
            "fill_count": len(fills),
            "fills": fills,
            "result_status": r.get("result_status"),
            "cancel_reason": r.get("cancel_reason"),
            "cancel_latency_ms": r.get("cancel_latency_ms"),
            "reprice_count": r.get("reprice_count"),
            "age_s": r.get("age_s"),
            "terminal_state": terminal,
            "n_events_seen": len(r.get("events_seen") or []),
        }
        records.append(record)

    records.sort(key=lambda r: r.get("submit_ts_ms") or r.get("fill_ts_ms_first") or 0)
    stats = {
        "n_records": len(records),
        "n_events_processed": n_total,
        "n_skipped_no_event": n_skipped_no_event,
        "n_skipped_pre_since": n_skipped_pre_since,
        "by_terminal_state": {
            "filled": n_filled,
            "partial_filled": n_partial,
            "canceled": n_canceled,
            "giveup": n_giveup,
            "open": n_open,
        },
    }
    return records, stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=str(LOG))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument(
        "--since",
        default=None,
        help="ISO8601 cutoff (e.g. 2026-04-26T23:00:00Z); default = process all",
    )
    args = ap.parse_args()

    log_path = Path(args.log)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    since_ms = _parse_ts(args.since) if args.since else 0

    print(
        f"# reading {log_path} (size {log_path.stat().st_size:,} bytes)",
        file=sys.stderr,
    )
    print(f"# since {args.since or '(beginning)'}", file=sys.stderr)

    records, stats = aggregate(log_path, since_ms=since_ms)

    with out_path.open("w") as fh:
        for r in records:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")

    print()
    print(f"# wrote {len(records)} lifecycle records to {out_path}")
    print()
    print("=== aggregate stats ===")
    print(f"  events processed     : {stats['n_events_processed']:,}")
    print(f"  records emitted      : {stats['n_records']:,}")
    print("  by terminal state    :")
    for k, v in stats["by_terminal_state"].items():
        pct = (v / stats["n_records"] * 100) if stats["n_records"] else 0
        print(f"    {k:16s}  {v:6d}  {pct:5.1f}%")

    # Summary metrics
    if records:
        n_maker = sum(1 for r in records if r.get("intent") == "maker")
        n_taker = sum(1 for r in records if r.get("intent") == "taker")
        total_fee = sum(r.get("total_fee", 0) or 0 for r in records)
        total_closed_pnl = sum(r.get("total_closed_pnl", 0) or 0 for r in records)
        lifetimes = [r["lifetime_ms"] for r in records if r.get("lifetime_ms")]
        med_lifetime_ms = sorted(lifetimes)[len(lifetimes) // 2] if lifetimes else None
        print()
        print("=== top-line metrics ===")
        print(f"  maker orders         : {n_maker}")
        print(f"  taker orders         : {n_taker}")
        print(f"  total fees           : ${total_fee:.4f}")
        print(f"  total closed_pnl     : ${total_closed_pnl:+.4f}")
        print(f"  net realized         : ${total_closed_pnl - total_fee:+.4f}")
        if med_lifetime_ms is not None:
            print(f"  median lifetime_ms   : {med_lifetime_ms:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
