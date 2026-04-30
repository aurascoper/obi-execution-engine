#!/usr/bin/env python3
"""scripts/analyze_quoter_shadow.py — analyze Gate 3 shadow telemetry.

Reads `quoter_shadow` events from `logs/hl_engine.jsonl` and produces:

  1. regime distribution by OBI bucket (toxic / neutral / favorable)
  2. shadow-vs-actual concordance: when shadow says CATCHUP/IOC, did
     the engine actually submit a limit (or skip)? When shadow says
     PASSIVE, did the engine actually post-only?
  3. distribution of intended vs shadow clip
  4. realized markout per fill if `hl_fill_received` events are
     followed by future `signal_tick` events for the same symbol
  5. Gate 3 acceptance verdict against the four-criterion bar

Outputs `autoresearch_gated/quoter_shadow_distributions.json` with the
full breakdown, plus a human-readable summary on stdout.

Pure analyzer. Read-only against the log.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception:
        return "unknown"


def _parse_iso(ts: str) -> Optional[float]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _obi_bucket(obi: float, threshold: float = 0.30) -> str:
    if obi > threshold:
        return "toxic_for_buy_pressure"
    if obi < -threshold:
        return "toxic_for_sell_pressure"
    return "neutral"


def _scenario_for_side(obi: float, side: str, threshold: float = 0.30) -> str:
    """Map (OBI, intended side) into the simulator's scenario bucket.
    For a sell, OBI > threshold = toxic. For a buy, OBI < -threshold = toxic.
    Symmetric."""
    if side == "sell":
        if obi > threshold:
            return "toxic"
        if obi < -threshold:
            return "favorable"
        return "neutral"
    if side == "buy":
        if obi < -threshold:
            return "toxic"
        if obi > threshold:
            return "favorable"
        return "neutral"
    return "unknown"


def collect_shadow_events(log_path: Path) -> list[dict]:
    events: list[dict] = []
    with log_path.open() as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("event") == "quoter_shadow":
                events.append(d)
    return events


def collect_fill_and_tick_events(log_path: Path) -> tuple[list[dict], dict[str, list[tuple[float, float]]]]:
    """Return (fill events, {symbol: [(t, mid_proxy)]}). The mid proxy
    is taken from any `signal_tick` rows that carry a `mid` field; if
    not present, falls back to limit_px on entry/exit signals as a
    coarse marker. For markout, the analyzer interpolates over time."""
    fills: list[dict] = []
    mid_by_sym: dict[str, list[tuple[float, float]]] = defaultdict(list)
    with log_path.open() as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            ev = d.get("event")
            sym = d.get("symbol")
            ts = _parse_iso(d.get("timestamp", ""))
            if ev in ("hl_fill_received", "fill_recorded") and ts is not None:
                fills.append({**d, "_t": ts})
            if ev in ("entry_signal", "exit_signal") and sym and ts is not None:
                px = d.get("limit_px") or d.get("close")
                if px is not None:
                    try:
                        mid_by_sym[sym].append((ts, float(px)))
                    except (TypeError, ValueError):
                        pass
    for s in mid_by_sym:
        mid_by_sym[s].sort()
    return fills, mid_by_sym


def _lookup_future_mid(
    series: list[tuple[float, float]], t_fill: float, horizon_s: float
) -> Optional[float]:
    target = t_fill + horizon_s
    if not series or target < series[0][0] or target > series[-1][0]:
        return None
    for i in range(len(series) - 1):
        t_a, p_a = series[i]
        t_b, p_b = series[i + 1]
        if t_a <= target <= t_b:
            if t_b == t_a:
                return p_a
            w = (target - t_a) / (t_b - t_a)
            return p_a + w * (p_b - p_a)
    return None


# ── Soak-window contamination labeller (operator-supplied checksum) ─────


def compute_soak_label(
    log_path: Path,
    soak_start_iso: str,
    soak_end_iso: Optional[str] = None,
    api_fills_path: Optional[Path] = None,
    per_trade_cap_usd: float = 75.0,
    regime_pause_seconds: float = 10800.0,
    toxicity_threshold: float = 0.30,
) -> dict:
    """Three-level (clean / diagnostic / invalid) classifier for whether
    the soak window is promotable for Gate 3.

    Computes four quantities:
      m         = manual-opened PnL share / total realized PnL
      e         = manual-opened exit count / total exit count
      r         = time in regime pause / total soak time
      n_toxic   = fresh engine-opened entries in toxic OBI bucket

    Heuristics for "manual-opened" attribution (tag-only logs available):
      - a symbol is manual-opened in the window if ANY single fill has
        |notional| > 2 × per_trade_cap_usd (default 2 × $75 = $150),
        OR if the fill stream shows fills on a symbol that has zero
        engine-side entry_signal events in the window.

    Acceptance thresholds (operator-supplied):
      clean:      m < 0.10  AND e < 0.15  AND r < 0.30  AND n_toxic ≥ 30
      diagnostic: any of  0.10 ≤ m < 0.35
                          0.15 ≤ e < 0.40
                          0.30 ≤ r < 0.60
                          10 ≤ n_toxic < 30
      invalid:    any of  m ≥ 0.35
                          e ≥ 0.40
                          r ≥ 0.60
                          n_toxic < 10

    Overrides:
      - if m_earnings ≥ 0.25, force at least 'diagnostic'
      - if m ≥ 0.50 OR e ≥ 0.50, force 'invalid'
    """
    soak_start = datetime.fromisoformat(soak_start_iso.replace("Z", "+00:00"))
    soak_end = (
        datetime.fromisoformat(soak_end_iso.replace("Z", "+00:00"))
        if soak_end_iso
        else datetime.now(timezone.utc)
    )
    soak_total_s = max(1.0, (soak_end - soak_start).total_seconds())

    entry_signals_per_symbol: dict[str, int] = defaultdict(int)
    exit_signals_per_symbol: dict[str, list[dict]] = defaultdict(list)
    toxic_bucket_engine_entries = 0
    regime_pause_trips: list[float] = []
    quoter_failed = 0

    with log_path.open() as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            ts = d.get("timestamp", "")
            try:
                t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except Exception:
                continue
            if t < soak_start or t > soak_end:
                continue
            ev = d.get("event")
            sym = d.get("symbol") or d.get("coin") or ""
            if ev == "entry_signal":
                entry_signals_per_symbol[sym] += 1
                obi = d.get("obi")
                if obi is not None:
                    try:
                        obi_f = float(obi)
                    except (TypeError, ValueError):
                        continue
                    if abs(obi_f) >= toxicity_threshold:
                        toxic_bucket_engine_entries += 1
            elif ev == "exit_signal":
                exit_signals_per_symbol[sym].append(d)
            elif ev == "risk_gate_regime_pause":
                regime_pause_trips.append(t.timestamp())
            elif ev == "quoter_shadow_failed":
                quoter_failed += 1

    paused_intervals: list[tuple[float, float]] = []
    pause_until = 0.0
    for trip_t in sorted(regime_pause_trips):
        new_until = trip_t + regime_pause_seconds
        if trip_t >= pause_until:
            if pause_until > 0:
                paused_intervals.append((paused_intervals[-1][0] if paused_intervals else trip_t, pause_until))
            paused_intervals.append((trip_t, new_until))
        else:
            paused_intervals[-1] = (paused_intervals[-1][0], new_until)
        pause_until = new_until
    soak_end_ts = soak_end.timestamp()
    soak_start_ts = soak_start.timestamp()
    pause_seconds = 0.0
    for s_t, e_t in paused_intervals:
        s_clip = max(s_t, soak_start_ts)
        e_clip = min(e_t, soak_end_ts)
        if e_clip > s_clip:
            pause_seconds += e_clip - s_clip

    fills_by_coin: dict[str, list[dict]] = defaultdict(list)
    if api_fills_path and api_fills_path.exists():
        try:
            payload = json.loads(api_fills_path.read_text())
            fills = payload if isinstance(payload, list) else payload.get("fills", [])
            for fill in fills:
                ts_ms = fill.get("time")
                if ts_ms is None:
                    continue
                try:
                    t = datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc)
                except Exception:
                    continue
                if t < soak_start or t > soak_end:
                    continue
                coin = fill.get("coin")
                if coin:
                    fills_by_coin[coin].append(fill)
        except Exception:
            pass

    manual_coins: set[str] = set()
    for coin, fills in fills_by_coin.items():
        for f in fills:
            try:
                sz = abs(float(f.get("sz", 0)))
                px = abs(float(f.get("px", 0)))
                if sz * px > 2 * per_trade_cap_usd:
                    manual_coins.add(coin)
                    break
            except Exception:
                continue

    for coin in fills_by_coin:
        eng_sym_candidates = (coin, coin + "/USD", coin + "/USDC")
        if not any(c in entry_signals_per_symbol for c in eng_sym_candidates):
            manual_coins.add(coin)

    earnings_known = {"xyz:GOOGL", "xyz:META", "xyz:AMZN", "xyz:MSFT", "xyz:LLY", "xyz:AAPL", "xyz:SNDK"}
    earnings_coins = manual_coins & earnings_known

    total_pnl = 0.0
    manual_pnl = 0.0
    earnings_pnl = 0.0
    for coin, fills in fills_by_coin.items():
        coin_pnl = sum(float(f.get("closedPnl", 0)) for f in fills)
        total_pnl += coin_pnl
        if coin in manual_coins:
            manual_pnl += coin_pnl
        if coin in earnings_coins:
            earnings_pnl += coin_pnl

    total_exits = sum(len(v) for v in exit_signals_per_symbol.values())
    manual_exits = sum(
        len(v) for sym, v in exit_signals_per_symbol.items()
        if sym.replace("/USD", "").replace("/USDC", "") in manual_coins
    )

    m = (manual_pnl / total_pnl) if abs(total_pnl) > 1e-9 else 0.0
    m_earnings = (earnings_pnl / total_pnl) if abs(total_pnl) > 1e-9 else 0.0
    e = (manual_exits / total_exits) if total_exits > 0 else 0.0
    r = pause_seconds / soak_total_s
    n_toxic = toxic_bucket_engine_entries

    if abs(m) >= 0.50 or e >= 0.50:
        label = "invalid"
        reason = "override: |m|≥0.50 or e≥0.50"
    elif (
        abs(m) >= 0.35
        or e >= 0.40
        or r >= 0.60
        or n_toxic < 10
    ):
        label = "invalid"
        reason = "any-of: |m|≥0.35 or e≥0.40 or r≥0.60 or n_toxic<10"
    elif (
        abs(m) >= 0.10
        or e >= 0.15
        or r >= 0.30
        or n_toxic < 30
    ):
        label = "diagnostic"
        reason = "any-of: 0.10≤|m|<0.35 or 0.15≤e<0.40 or 0.30≤r<0.60 or 10≤n_toxic<30"
    else:
        label = "clean"
        reason = "all thresholds satisfied"

    if abs(m_earnings) >= 0.25 and label == "clean":
        label = "diagnostic"
        reason += " | earnings override: |m_earnings|≥0.25 forces ≥diagnostic"

    return {
        "label": label,
        "reason": reason,
        "metrics": {
            "m": m,
            "m_earnings": m_earnings,
            "e": e,
            "r": r,
            "n_toxic": n_toxic,
            "quoter_shadow_failed": quoter_failed,
        },
        "raw": {
            "manual_pnl": manual_pnl,
            "earnings_pnl": earnings_pnl,
            "total_pnl": total_pnl,
            "manual_exits": manual_exits,
            "total_exits": total_exits,
            "regime_pause_seconds": pause_seconds,
            "soak_total_seconds": soak_total_s,
            "manual_coins": sorted(manual_coins),
            "earnings_coins": sorted(earnings_coins),
        },
        "thresholds": {
            "clean": "|m|<0.10 AND e<0.15 AND r<0.30 AND n_toxic≥30",
            "diagnostic": "any of 0.10≤|m|<0.35, 0.15≤e<0.40, 0.30≤r<0.60, 10≤n_toxic<30",
            "invalid": "any of |m|≥0.35, e≥0.40, r≥0.60, n_toxic<10",
            "earnings_override": "|m_earnings|≥0.25 forces ≥diagnostic",
            "hard_override": "|m|≥0.50 or e≥0.50 forces invalid",
        },
        "soak_window": {
            "start": soak_start.isoformat(),
            "end": soak_end.isoformat(),
            "duration_s": soak_total_s,
        },
    }


def evaluate_gate_3(report: dict) -> dict:
    """Apply the four-criterion Gate 3 decision rule from the operator's
    Task 25 spec:
      1. toxic windows show more TOUCH/CATCHUP than favorable
      2. shadow markout preserves broad ordering seen in replay/sim
      3. scheduler miss / actual action distributions are sane
      4. no pathological churn / dead zones / contradiction
    Returns a dict with each criterion's verdict + overall.
    """
    by_scen = report.get("regime_by_scenario", {})

    def _esc(s: str) -> Optional[float]:
        b = by_scen.get(s)
        if not b or b["n_total"] == 0:
            return None
        return (b["touch"] + b["catchup"]) / b["n_total"]

    tox_esc = _esc("toxic")
    fav_esc = _esc("favorable")
    crit_1 = (
        tox_esc is not None and fav_esc is not None and tox_esc > fav_esc
    )

    mk = report.get("markout_by_scenario", {})
    tox_m = mk.get("toxic", {}).get("mean")
    fav_m = mk.get("favorable", {}).get("mean")
    neu_m = mk.get("neutral", {}).get("mean")
    if tox_m is None or fav_m is None or neu_m is None:
        crit_2 = None
    else:
        crit_2 = (fav_m >= neu_m - 1.0) and (tox_m <= fav_m)

    skipped_total = sum(report.get("status_counts", {}).get(k, 0) for k in (
        "skipped_no_obi", "skipped_bad_obi_type", "skipped_obi_nonfinite",
        "skipped_bad_mid", "skipped_bad_side_sign", "skipped_post_trade_flat",
    ))
    n_total = report.get("n_shadow_events_total", 0)
    skip_frac = (skipped_total / n_total) if n_total else 1.0
    crit_3 = n_total > 0 and skip_frac < 0.20

    contradictions = report.get("contradiction_counts", {})
    pathological_total = (
        contradictions.get("shadow_passive_engine_zero", 0)
        + contradictions.get("shadow_catchup_engine_zero", 0)
    )
    crit_4 = pathological_total == 0 or (n_total > 0 and pathological_total / n_total < 0.05)

    return {
        "crit_1_toxic_more_touch_than_favorable": crit_1,
        "crit_2_markout_ordering_preserved": crit_2,
        "crit_3_skip_rate_under_20pct": crit_3,
        "crit_4_no_pathological_churn": crit_4,
        "all_pass": all(v is True for v in (crit_1, crit_2, crit_3, crit_4)),
        "details": {
            "toxic_escalation_share": tox_esc,
            "favorable_escalation_share": fav_esc,
            "toxic_markout_mean_bps": tox_m,
            "neutral_markout_mean_bps": neu_m,
            "favorable_markout_mean_bps": fav_m,
            "skipped_fraction": skip_frac,
            "pathological_total": pathological_total,
        },
    }


def build_report(
    shadow_events: list[dict],
    fills: list[dict],
    mid_by_sym: dict[str, list[tuple[float, float]]],
    markout_horizon_s: float = 60.0,
) -> dict:
    n_total = len(shadow_events)
    status_counts: Counter = Counter()
    for e in shadow_events:
        status_counts[e.get("shadow_status", "unknown")] += 1

    ok_events = [e for e in shadow_events if e.get("shadow_status") == "ok"]

    regime_by_scenario: dict[str, dict] = {
        "toxic": {"passive": 0, "touch": 0, "catchup": 0, "n_total": 0},
        "neutral": {"passive": 0, "touch": 0, "catchup": 0, "n_total": 0},
        "favorable": {"passive": 0, "touch": 0, "catchup": 0, "n_total": 0},
    }
    by_symbol: dict[str, dict] = defaultdict(
        lambda: {"passive": 0, "touch": 0, "catchup": 0, "n_total": 0}
    )

    contradiction_counts: Counter = Counter()
    intended_clip = []
    shadow_clip = []
    obi_seen = []

    for e in ok_events:
        regime = e.get("shadow_regime", "passive")
        side = e.get("side", "unknown")
        obi = e.get("shadow_y_toxicity")
        if obi is None:
            continue
        sym = e.get("symbol")
        scen = _scenario_for_side(float(obi), side)
        if scen in regime_by_scenario:
            bucket = regime_by_scenario[scen]
            bucket["n_total"] += 1
            if regime in bucket:
                bucket[regime] += 1
        if sym:
            sb = by_symbol[sym]
            sb["n_total"] += 1
            if regime in sb:
                sb[regime] += 1
        if e.get("intended_notional") is not None:
            intended_clip.append(float(e["intended_notional"]))
        if e.get("shadow_clip_usd") is not None:
            shadow_clip.append(float(e["shadow_clip_usd"]))
        obi_seen.append(float(obi))
        intended = e.get("intended_notional")
        if intended is not None and intended <= 0:
            if regime in ("passive", "touch"):
                contradiction_counts["shadow_passive_engine_zero"] += 1
            elif regime == "catchup":
                contradiction_counts["shadow_catchup_engine_zero"] += 1

    markout_by_scenario: dict[str, dict] = {
        "toxic": {"values": []},
        "neutral": {"values": []},
        "favorable": {"values": []},
    }
    if fills and mid_by_sym:
        for fill in fills:
            sym = fill.get("symbol") or fill.get("coin")
            t_fill = fill.get("_t")
            px = fill.get("price") or fill.get("avg_price") or fill.get("limit_px")
            side = fill.get("side")
            if not sym or t_fill is None or px is None or not side:
                continue
            future_mid = _lookup_future_mid(
                mid_by_sym.get(sym, []), t_fill, markout_horizon_s
            )
            if future_mid is None or float(px) <= 0:
                continue
            side_str = str(side).lower()
            if side_str in ("buy", "long", "bid"):
                markout_bps = (float(future_mid) - float(px)) / float(px) * 10_000.0
                bucket_side = "buy"
            elif side_str in ("sell", "short", "ask"):
                markout_bps = (float(px) - float(future_mid)) / float(px) * 10_000.0
                bucket_side = "sell"
            else:
                continue
            obi = fill.get("obi")
            if obi is None:
                continue
            try:
                obi_f = float(obi)
            except (TypeError, ValueError):
                continue
            scen = _scenario_for_side(obi_f, bucket_side)
            if scen in markout_by_scenario:
                markout_by_scenario[scen]["values"].append(markout_bps)

    for scen in markout_by_scenario:
        vals = markout_by_scenario[scen]["values"]
        if vals:
            markout_by_scenario[scen]["mean"] = sum(vals) / len(vals)
            markout_by_scenario[scen]["median"] = statistics.median(vals)
            markout_by_scenario[scen]["n"] = len(vals)
        else:
            markout_by_scenario[scen]["mean"] = None
            markout_by_scenario[scen]["median"] = None
            markout_by_scenario[scen]["n"] = 0
        markout_by_scenario[scen].pop("values", None)

    return {
        "kind": "quoter_shadow_distributions",
        "git_sha": _git_sha(),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "n_shadow_events_total": n_total,
        "n_shadow_events_ok": len(ok_events),
        "status_counts": dict(status_counts),
        "regime_by_scenario": regime_by_scenario,
        "regime_by_symbol_top": dict(
            sorted(by_symbol.items(), key=lambda kv: -kv[1]["n_total"])[:20]
        ),
        "intended_clip_stats": _stats(intended_clip),
        "shadow_clip_stats": _stats(shadow_clip),
        "obi_seen_stats": _stats(obi_seen),
        "contradiction_counts": dict(contradiction_counts),
        "markout_by_scenario": markout_by_scenario,
        "markout_horizon_s": markout_horizon_s,
        "n_fills_with_markout": sum(
            v.get("n", 0) for v in markout_by_scenario.values()
        ),
    }


def _stats(vals: list[float]) -> dict:
    if not vals:
        return {"n": 0}
    return {
        "n": len(vals),
        "mean": sum(vals) / len(vals),
        "median": statistics.median(vals),
        "min": min(vals),
        "max": max(vals),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", type=Path, default=ROOT / "logs/hl_engine.jsonl")
    ap.add_argument(
        "--out",
        type=Path,
        default=ROOT / "autoresearch_gated/quoter_shadow_distributions.json",
    )
    ap.add_argument("--markout-horizon-s", type=float, default=60.0)
    ap.add_argument("--soak-start", type=str, default=None,
                    help="ISO8601 soak window start (default: first hl_engine_start in log)")
    ap.add_argument("--soak-end", type=str, default=None,
                    help="ISO8601 soak window end (default: now)")
    ap.add_argument("--api-fills", type=Path, default=None,
                    help="optional JSON file containing userFillsByTime payload for PnL attribution")
    ap.add_argument("--per-trade-cap-usd", type=float, default=75.0)
    args = ap.parse_args()

    print(f"Reading {args.log}...")
    shadow_events = collect_shadow_events(args.log)
    print(f"  found {len(shadow_events)} quoter_shadow events")

    if not shadow_events:
        print("No quoter_shadow events found. Either:")
        print("  • Engine has not run since the Gate 3 wiring was deployed.")
        print("  • Engine ran but emitted zero entry decisions.")
        print("Both are valid pre-soak states; nothing to analyze yet.")
        return 0

    fills, mid_by_sym = collect_fill_and_tick_events(args.log)
    print(f"  found {len(fills)} fill events, {len(mid_by_sym)} symbols with mid history")

    report = build_report(
        shadow_events, fills, mid_by_sym, markout_horizon_s=args.markout_horizon_s
    )
    report["acceptance"] = evaluate_gate_3(report)

    soak_start = args.soak_start
    if soak_start is None:
        latest_start = None
        with args.log.open() as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("event") == "hl_engine_start":
                    latest_start = d.get("timestamp")
        soak_start = latest_start
    if soak_start is None and shadow_events:
        soak_start = shadow_events[0].get("timestamp")
    if soak_start:
        try:
            report["soak_label"] = compute_soak_label(
                args.log,
                soak_start_iso=soak_start,
                soak_end_iso=args.soak_end,
                api_fills_path=args.api_fills,
                per_trade_cap_usd=args.per_trade_cap_usd,
            )
        except Exception as exc:
            report["soak_label"] = {"label": "error", "error": repr(exc)}

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2))
    print(f"\nReport written: {args.out}\n")

    print("=" * 80)
    print(f"shadow events:   total={report['n_shadow_events_total']}  "
          f"ok={report['n_shadow_events_ok']}")
    print(f"status counts:")
    for k, v in sorted(report["status_counts"].items(), key=lambda kv: -kv[1]):
        print(f"  {v:>7}  {k}")

    print("\nRegime distribution by scenario (OBI-bucketed):")
    for scen in ("toxic", "neutral", "favorable"):
        b = report["regime_by_scenario"][scen]
        n = b["n_total"] or 1
        print(
            f"  {scen:<10}  n={b['n_total']:>5}  "
            f"PASSIVE={b['passive']/n:.1%}  "
            f"TOUCH={b['touch']/n:.1%}  "
            f"CATCHUP={b['catchup']/n:.1%}"
        )

    print("\nMarkout by scenario:")
    for scen in ("toxic", "neutral", "favorable"):
        m = report["markout_by_scenario"][scen]
        if m.get("n", 0) > 0:
            print(f"  {scen:<10}  n={m['n']:>4}  "
                  f"mean={m['mean']:+.3f} bps  median={m['median']:+.3f} bps")
        else:
            print(f"  {scen:<10}  n=0  (no fills with both OBI and future-mid resolution)")

    print("\nClip stats (intended vs shadow):")
    print(f"  intended: {report['intended_clip_stats']}")
    print(f"  shadow:   {report['shadow_clip_stats']}")

    print(f"\nOBI seen: {report['obi_seen_stats']}")
    print(f"Contradictions: {report['contradiction_counts']}")

    print("\n" + "=" * 80)
    print("GATE 3 ACCEPTANCE")
    print("=" * 80)
    a = report["acceptance"]
    for k in (
        "crit_1_toxic_more_touch_than_favorable",
        "crit_2_markout_ordering_preserved",
        "crit_3_skip_rate_under_20pct",
        "crit_4_no_pathological_churn",
    ):
        v = a[k]
        mark = "PASS" if v is True else ("FAIL" if v is False else "INCONCLUSIVE")
        print(f"  [{mark:>12}] {k}")
    print(f"  ALL_PASS = {a['all_pass']}")
    print(f"  details: {json.dumps(a['details'], indent=4)}")

    sl = report.get("soak_label")
    if sl and "metrics" in sl:
        m_metrics = sl["metrics"]
        m_raw = sl["raw"]
        print("\n" + "=" * 80)
        print(f"SOAK CONTAMINATION LABEL = {sl['label'].upper()}")
        print("=" * 80)
        print(f"  reason: {sl['reason']}")
        print(f"  window: {sl['soak_window']['start']}  →  {sl['soak_window']['end']}")
        print(f"          duration = {sl['soak_window']['duration_s']/3600.0:.2f} h")
        print()
        print(f"  m (manual PnL share):       {m_metrics['m']:+.3f}   "
              f"manual=${m_raw['manual_pnl']:+.2f} / total=${m_raw['total_pnl']:+.2f}")
        print(f"  m_earnings (earnings PnL):  {m_metrics['m_earnings']:+.3f}   "
              f"earnings=${m_raw['earnings_pnl']:+.2f}")
        print(f"  e (manual exit share):      {m_metrics['e']:.3f}   "
              f"manual_exits={m_raw['manual_exits']} / total_exits={m_raw['total_exits']}")
        print(f"  r (regime pause share):     {m_metrics['r']:.3f}   "
              f"paused={m_raw['regime_pause_seconds']/3600.0:.2f}h / "
              f"soak={m_raw['soak_total_seconds']/3600.0:.2f}h")
        print(f"  n_toxic (engine entries):   {m_metrics['n_toxic']}")
        print(f"  quoter_shadow_failed:       {m_metrics['quoter_shadow_failed']}")
        print()
        print(f"  manual coins detected: {m_raw['manual_coins']}")
        print(f"  earnings coins:        {m_raw['earnings_coins']}")
        print()
        if sl["label"] == "clean":
            print("  → Gate 3 PROMOTABLE if acceptance ALL_PASS")
        elif sl["label"] == "diagnostic":
            print("  → Gate 3 NOT PROMOTABLE; useful for sanity checks only")
        else:
            print("  → DO NOT USE THIS RUN to judge the quoter")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
