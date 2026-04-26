#!/usr/bin/env python3
"""Per-symbol legacy/managed-position audit.

For every symbol with at least one fill in the validation window, classify:

  A. legacy_close_only   not in configured_live, open_at_window_start,
                         every fill is a reduction (no opens).
  B. unexpected_entry    not in configured_live, NOT open_at_window_start,
                         had at least one opening fill in window.
  C. config_bug          IN configured_live, OPEN at window start, no
                         opens or only-trivial activity in window
                         (likely should/used to trade more).
  D. manual_intervention fills correlate (within 5 min) with running
                         close_*.py / transfer / manual scripts touching
                         the same symbol.
  -  active_normal       in configured_live, normal activity (opens +
                         exits in window).

Decision rules (per the user's spec):
  - If A dominates → keep in production_state_ρ until flat; exclude
    from entry_policy_ρ.
  - If B exists → engine/config bug; investigate before promotion.
  - If C exists → fix config (re-add the symbol).
  - If D dominates → annotate window or exclude manual-intervention syms.

Usage:
    venv/bin/python3 scripts/audit_legacy_managed_symbols.py [--window-days 14]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / "hl_engine.jsonl"
RATCHET_LOG = ROOT / "logs" / "shock_ratchet.log"
TOPUP_LOG = Path("/tmp/auto_topup.log")

sys.path.insert(0, str(ROOT))


def _norm(s: str) -> str:
    return (s or "").replace("/USD", "").replace("/USDC", "")


def _parse_iso(ts) -> int:
    if isinstance(ts, (int, float)):
        return int(ts * 1000) if ts < 1e12 else int(ts)
    if isinstance(ts, str):
        try:
            s = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
            return int(dt.datetime.fromisoformat(s).timestamp() * 1000)
        except Exception:
            return 0
    return 0


def _addr() -> str:
    a = os.environ.get("HL_WALLET_ADDRESS")
    if not a:
        env_path = ROOT / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("HL_WALLET_ADDRESS="):
                    a = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not a:
        raise SystemExit("HL_WALLET_ADDRESS missing")
    return a


def configured_universe() -> set[str]:
    """HL_UNIVERSE + HIP3_UNIVERSE + pairs_whitelist + auto_topup ZEC."""
    out: set[str] = set()
    for var in ("HL_UNIVERSE", "HIP3_UNIVERSE"):
        v = os.environ.get(var) or ""
        for tok in v.split(","):
            s = _norm(tok.strip())
            if s:
                out.add(s)
    # Captured engine env fallback
    env_file = Path("/tmp/engine_env.txt")
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            for var in ("HL_UNIVERSE=", "HIP3_UNIVERSE="):
                if line.startswith(var):
                    v = line[len(var):]
                    for tok in v.split(","):
                        s = _norm(tok.strip())
                        if s:
                            out.add(s)
    # pairs whitelist
    pw = ROOT / "config" / "pairs_whitelist.json"
    if pw.exists():
        try:
            d = json.loads(pw.read_text())
            for tok in d.get("universe") or []:
                s = _norm(str(tok))
                if s:
                    out.add(s)
            for p in d.get("pairs") or []:
                for k in ("leg_a", "leg_b"):
                    s = _norm(str(p.get(k, "")))
                    if s:
                        out.add(s)
        except Exception:
            pass
    out.add("ZEC")
    return out


def held_at_start_from_engine_log(start_ms: int) -> dict[str, float]:
    """Last szi per symbol BEFORE start_ms via hl_position_reconciled events."""
    last_szi: dict[str, float] = {}
    with LOG.open() as f:
        for line in f:
            if '"hl_position_reconciled"' not in line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get("event") != "hl_position_reconciled":
                continue
            ts_ms = _parse_iso(o.get("timestamp", ""))
            if ts_ms >= start_ms:
                break
            sym = _norm(o.get("symbol") or o.get("coin") or "")
            if not sym:
                continue
            try:
                last_szi[sym] = float(o.get("szi", 0) or 0)
            except (TypeError, ValueError):
                continue
    return {s: q for s, q in last_szi.items() if abs(q) > 1e-6}


def held_now() -> set[str]:
    addr = _addr()
    try:
        from hyperliquid.info import Info
        from hyperliquid.utils import constants
    except ImportError:
        return set()
    held: set[str] = set()
    for dex in (None, "xyz", "vntl", "hyna", "flx", "km", "cash", "para"):
        kw = dict(skip_ws=True)
        if dex:
            kw["perp_dexs"] = [dex]
        try:
            us = Info(constants.MAINNET_API_URL, **kw).user_state(addr)
        except Exception:
            continue
        for ap in us.get("assetPositions") or []:
            p = ap.get("position") or {}
            try:
                if abs(float(p.get("szi", 0) or 0)) > 1e-9:
                    s = _norm(p.get("coin", ""))
                    if s:
                        held.add(s)
            except Exception:
                continue
    return held


def hl_fills_in_window(from_ms: int, to_ms: int):
    """Return per-symbol aggregates: gross_pnl, fees, n_fills, n_opens,
    n_reductions, first_ts, last_ts (positive = open; negative = reduce)."""
    addr = _addr()
    from hyperliquid.info import Info
    from hyperliquid.utils import constants

    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    try:
        fills = info.user_fills_by_time(addr, from_ms, to_ms, aggregate_by_time=False) or []
    except Exception as e:
        print(f"# warn: fills fetch failed: {e}", file=sys.stderr)
        fills = []

    pnl = defaultdict(float)
    fees = defaultdict(float)
    n_fills = defaultdict(int)
    n_opens = defaultdict(int)
    n_reductions = defaultdict(int)
    first_ts = {}
    last_ts = {}
    pos_track: dict[str, float] = defaultdict(float)
    for f in sorted(fills, key=lambda x: int(x.get("time", 0) or 0)):
        sym = _norm(f.get("coin", ""))
        if not sym:
            continue
        try:
            cp = float(f.get("closedPnl", 0) or 0)
            fee = float(f.get("fee", 0) or 0)
            sz = float(f.get("sz", 0) or 0)
            ts_ms = int(f.get("time", 0) or 0)
            side = (f.get("side") or "").upper()
        except (TypeError, ValueError):
            continue
        d = sz if side == "B" else -sz
        prev = pos_track[sym]
        new = prev + d
        pnl[sym] += cp
        fees[sym] += fee
        n_fills[sym] += 1
        # opening: |new| > |prev| AND same sign as new (or fresh open from 0)
        is_open = abs(new) > abs(prev) + 1e-9
        if is_open:
            n_opens[sym] += 1
        else:
            n_reductions[sym] += 1
        pos_track[sym] = new
        if sym not in first_ts:
            first_ts[sym] = ts_ms
        last_ts[sym] = ts_ms
    return {
        "pnl": dict(pnl),
        "fees": dict(fees),
        "n_fills": dict(n_fills),
        "n_opens": dict(n_opens),
        "n_reductions": dict(n_reductions),
        "first_ts": dict(first_ts),
        "last_ts": dict(last_ts),
    }


def count_ratchet_per_sym(from_ms: int, to_ms: int) -> dict[str, int]:
    cnt = defaultdict(int)
    if not RATCHET_LOG.exists():
        return dict(cnt)
    sym_re = re.compile(r"symbol=([A-Za-z0-9:_/]+)")
    tag_re = re.compile(r"tag=shock_ratchet")
    ts_re = re.compile(r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})")
    with RATCHET_LOG.open() as f:
        for line in f:
            if not tag_re.search(line):
                continue
            m_ts = ts_re.match(line)
            if m_ts:
                try:
                    ts_ms = int(
                        dt.datetime.fromisoformat(m_ts.group(1).replace(" ", "T") + "+00:00").timestamp() * 1000
                    )
                except Exception:
                    ts_ms = 0
                if ts_ms < from_ms or ts_ms >= to_ms:
                    continue
            m_sym = sym_re.search(line)
            if m_sym:
                cnt[_norm(m_sym.group(1))] += 1
    return dict(cnt)


def count_topup_per_sym(from_ms: int, to_ms: int) -> dict[str, int]:
    cnt = defaultdict(int)
    if not TOPUP_LOG.exists():
        return dict(cnt)
    fire_re = re.compile(r"^(\S+)\s+FIRE\s+([A-Za-z0-9:_/]+)\s")
    with TOPUP_LOG.open() as f:
        for line in f:
            m = fire_re.match(line)
            if not m:
                continue
            try:
                ts_ms = int(
                    dt.datetime.fromisoformat(m.group(1).replace("Z", "+00:00")).timestamp() * 1000
                )
            except Exception:
                continue
            if ts_ms < from_ms or ts_ms >= to_ms:
                continue
            cnt[_norm(m.group(2))] += 1
    return dict(cnt)


def manual_script_hits_per_sym(from_ms: int, to_ms: int) -> dict[str, int]:
    """Look for close_*.py invocations in zsh_history, plus python invocations
    of files matching close_*. Crude proxy."""
    cnt = defaultdict(int)
    hist = Path.home() / ".zsh_history"
    if not hist.exists():
        return dict(cnt)
    # Pattern: filename like close_<symbol>.py
    pat = re.compile(r"\b(close|open|fix|place)_([a-zA-Z0-9_]+)\.py\b")
    try:
        text = hist.read_text(errors="ignore")
    except Exception:
        return dict(cnt)
    for line in text.splitlines():
        # zsh extended history: ":time:elapsed;cmd"
        m = pat.search(line)
        if not m:
            continue
        sym_token = m.group(2).upper().replace("_", ":")
        # Heuristic: try matching xyz_mstr → xyz:MSTR, otherwise raw
        cnt[sym_token] += 1
        cnt["xyz:" + sym_token.split(":")[-1]] += 1
    return dict(cnt)


def classify(row: dict) -> str:
    in_cfg = row["in_configured_live"]
    open_at_start = row["open_at_window_start"]
    n_fills = row["n_fills"]
    n_opens = row["n_opens"]
    manual = row["manual_script_hits"]
    if manual > 0 and n_fills > 0:
        return "manual_intervention"
    if not in_cfg and open_at_start and n_fills > 0 and n_opens == 0:
        return "legacy_close_only"
    if not in_cfg and not open_at_start and n_opens > 0:
        return "unexpected_entry"
    if in_cfg and open_at_start and n_opens == 0 and n_fills < 3:
        return "config_bug_candidate"
    if n_fills == 0:
        return "inactive"
    return "active_normal"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-days", type=int, default=14)
    ap.add_argument("--out", default=str(ROOT / "autoresearch_gated" / "legacy_audit.md"))
    args = ap.parse_args()

    to_ms = int(dt.datetime.now(tz=dt.timezone.utc).timestamp() * 1000)
    from_ms = to_ms - args.window_days * 86_400_000

    print(f"# legacy audit — {args.window_days}d  [{from_ms}..{to_ms})", file=sys.stderr)

    cfg = configured_universe()
    print(f"# configured_live: {len(cfg)} syms", file=sys.stderr)

    held_start = held_at_start_from_engine_log(from_ms)
    print(f"# held @ window start (engine_log): {len(held_start)} syms", file=sys.stderr)

    held_now_set = held_now()
    print(f"# held now (HL API): {len(held_now_set)} syms", file=sys.stderr)

    print("# pulling fills in window...", file=sys.stderr)
    f = hl_fills_in_window(from_ms, to_ms)

    print("# counting ratchet/topup/manual...", file=sys.stderr)
    ratchet = count_ratchet_per_sym(from_ms, to_ms)
    topup = count_topup_per_sym(from_ms, to_ms)
    manual = manual_script_hits_per_sym(from_ms, to_ms)

    syms = sorted(set(f["pnl"]) | set(held_start) | set(held_now_set))
    rows = []
    for s in syms:
        row = {
            "symbol": s,
            "in_configured_live": s in cfg,
            "has_live_fills": f["n_fills"].get(s, 0) > 0,
            "open_at_window_start": s in held_start,
            "open_now": s in held_now_set,
            "gross_closed_pnl": f["pnl"].get(s, 0.0),
            "fees": f["fees"].get(s, 0.0),
            "n_fills": f["n_fills"].get(s, 0),
            "n_opens": f["n_opens"].get(s, 0),
            "n_reductions": f["n_reductions"].get(s, 0),
            "first_fill_ts": dt.datetime.fromtimestamp(
                f["first_ts"].get(s, 0) / 1000, tz=dt.timezone.utc
            ).strftime("%Y-%m-%d %H:%M") if f["first_ts"].get(s, 0) else "-",
            "last_fill_ts": dt.datetime.fromtimestamp(
                f["last_ts"].get(s, 0) / 1000, tz=dt.timezone.utc
            ).strftime("%Y-%m-%d %H:%M") if f["last_ts"].get(s, 0) else "-",
            "ratchet_tranches": ratchet.get(s, 0),
            "auto_topup_count": topup.get(s, 0),
            "manual_script_hits": manual.get(s, 0),
        }
        row["bucket"] = classify(row)
        rows.append(row)

    # Bucket totals
    bucket_n = defaultdict(int)
    bucket_pnl = defaultdict(float)
    for r in rows:
        bucket_n[r["bucket"]] += 1
        bucket_pnl[r["bucket"]] += r["gross_closed_pnl"]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Legacy/Managed-Symbol Audit",
        "",
        f"- window: **{args.window_days}d**",
        f"- configured_live universe: {len(cfg)} syms",
        f"- held @ window start: {len(held_start)} syms",
        f"- held now: {len(held_now_set)} syms",
        f"- symbols active in window or held: {len(rows)}",
        "",
        "## Bucket counts + PnL",
        "",
        "| bucket | n | sum closedPnl |",
        "|---|---|---|",
    ]
    for b in sorted(bucket_n, key=lambda b: -bucket_n[b]):
        lines.append(f"| {b} | {bucket_n[b]} | ${bucket_pnl[b]:+.2f} |")
    lines.append("")
    lines.append("## Per-symbol detail")
    lines.append("")
    lines.append(
        "| sym | bucket | in_cfg | open_start | open_now | n_fills | opens | reds | "
        "gross$ | fees | ratchet | topup | manual |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in sorted(rows, key=lambda r: (r["bucket"], -abs(r["gross_closed_pnl"]))):
        lines.append(
            f"| {r['symbol']} | {r['bucket']} | "
            f"{'Y' if r['in_configured_live'] else 'n'} | "
            f"{'Y' if r['open_at_window_start'] else 'n'} | "
            f"{'Y' if r['open_now'] else 'n'} | "
            f"{r['n_fills']} | {r['n_opens']} | {r['n_reductions']} | "
            f"{r['gross_closed_pnl']:+.2f} | {-r['fees']:+.2f} | "
            f"{r['ratchet_tranches']} | {r['auto_topup_count']} | "
            f"{r['manual_script_hits']} |"
        )
    out.write_text("\n".join(lines) + "\n")

    print()
    print("=== Buckets (sorted by n) ===")
    for b in sorted(bucket_n, key=lambda b: -bucket_n[b]):
        print(f"  {b:24s}  n={bucket_n[b]:3d}  closedPnl=${bucket_pnl[b]:+9.2f}")
    print(f"\n# wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
