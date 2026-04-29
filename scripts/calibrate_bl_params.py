#!/usr/bin/env python3
"""
scripts/calibrate_bl_params.py — Bechler-Ludkovski (β, σ, η) calibration runner.

Phase 1 of the BL integration plan (docs/execution_calibration_sketch.md).
Read-only fit of three per-symbol parameters from logs/hl_engine.jsonl:

  β  — OFI mean-reversion rate (1/seconds), from AR(1) on signal_tick.obi
       resampled to a uniform 60s grid. Drives time-in-market in the BL
       optimal-trading-rate formula.
  σ  — OFI driving-noise vol, from the AR(1) residual variance.
  η  — temporary linear price impact (bps per $1 of notional), from
       realized slippage between hl_order_submitted.limit_px and
       hl_fill_received.px on matched cloid pairs.

This runner does NOT:
  - touch bars.sqlite (Phase 1 sketch isolates the data source to the JSONL)
  - solve the Riccati ODE (separate authorized PR)
  - modify config/risk_params.py (operator reviews fit numbers before any
    config commit)
  - alter engine state in any way

Output: structured stdout with per-symbol fits + class-level medians +
status histogram + one-line provenance footer. Diagnostic stderr is
suppressed by default; use --verbose to surface it.

Usage:
  venv/bin/python3 scripts/calibrate_bl_params.py
  venv/bin/python3 scripts/calibrate_bl_params.py --window-days 14
  venv/bin/python3 scripts/calibrate_bl_params.py --start-ts 2026-04-28T19:35:07Z
  venv/bin/python3 scripts/calibrate_bl_params.py --top 60 --verbose
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

DEFAULT_LOG = Path("logs/hl_engine.jsonl")
DEFAULT_WINDOW_DAYS = 7.0

# OU fit parameters
DT_S = 60               # uniform resampling grid for AR(1)
N_MIN_OU = 60           # minimum observations on the grid (1h equivalent)
N_MIN_ETA = 3           # minimum fill pairs for η fit

# Cloid prefix for manual orders (excluded per Gate E intervention mask).
MANUAL_PFX = "0xdead0001"

# Fill→submission matching: nearest-prior window (seconds) when cloid absent.
NEAREST_PRIOR_S = 10.0

# Submission→shadow matching: nearest-prior shadow event window (seconds).
# `sizing_runtime_shadow` fires once per signal evaluation (immediately
# before `_risk_gate_ok`). The first `hl_order_submitted` follows within
# ~1s, but Alo cancel-replace retries and IOC escalations can stretch over
# 30-60s for the same logical order. All those retries should reuse the
# original shadow.mid as the reference. 60s window covers the typical
# Alo reprice ladder.
SHADOW_LOOKBACK_S = 60.0


# ── helpers ────────────────────────────────────────────────────────────────

def is_hip3(sym: str) -> bool:
    return ":" in (sym or "")


def normalize_symbol(sym: str | None) -> str:
    """Strip the /USD or /USDC quote suffix so events from different log
    sources collate to the same key. `signal_tick` and `sizing_runtime_shadow`
    log symbols as 'BTC/USD' / 'xyz:GOOGL/USD'; `hl_order_submitted` and
    `hl_fill_received` log them bare as 'BTC' / 'xyz:GOOGL'. Bare is the
    canonical engine convention; we conform everything to it."""
    if not sym:
        return ""
    if sym.endswith("/USDC"):
        return sym[:-5]
    if sym.endswith("/USD"):
        return sym[:-4]
    return sym


def parse_ts(ts_str: str) -> dt.datetime:
    return dt.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))


def percentile(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    k = (len(s) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] + (s[c] - s[f]) * (k - f)


# ── data collection ────────────────────────────────────────────────────────

def collect(jsonl_path: Path, start_ts: str) -> dict:
    """Single-pass scan of hl_engine.jsonl. Returns five collections.

    obi[sym]            -> [(ts_seconds, obi_value)]
    sends[sym]          -> [{ts, side, qty, px, cloid}, ...]
    sends_by_cloid      -> {(sym, cloid): {ts, side, qty, px}}
    fills (list)        -> [{sym, ts, side, qty, px, fee, closed_pnl, cloid, crossed}, ...]
    shadows[sym]        -> [(ts_seconds, side, mid)]
    """
    obi: dict[str, list[tuple[float, float]]] = defaultdict(list)
    sends: dict[str, list[dict]] = defaultdict(list)
    sends_by_cloid: dict[tuple[str, str], dict] = {}
    fills: list[dict] = []
    shadows: dict[str, list[tuple[float, str, float]]] = defaultdict(list)

    with jsonl_path.open() as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts_str = r.get("timestamp", "")
            if ts_str < start_ts:
                continue
            ev = r.get("event", "")
            sym = normalize_symbol(r.get("symbol") or r.get("coin"))
            if not sym:
                continue

            if ev == "signal_tick":
                obi_val = r.get("obi")
                if obi_val is None:
                    continue
                try:
                    obi[sym].append((parse_ts(ts_str).timestamp(), float(obi_val)))
                except (TypeError, ValueError):
                    continue

            elif ev == "hl_order_submitted":
                try:
                    qty = float(r.get("qty") or 0)
                    px = float(r.get("limit_px") or 0)
                    if qty <= 0 or px <= 0:
                        continue
                    rec = {
                        "ts": parse_ts(ts_str).timestamp(),
                        "side": r.get("side"),
                        "qty": qty,
                        "px": px,
                        "cloid": r.get("cloid") or "",
                    }
                    sends[sym].append(rec)
                    if rec["cloid"]:
                        sends_by_cloid[(sym, rec["cloid"])] = rec
                except (TypeError, ValueError):
                    continue

            elif ev == "hl_fill_received":
                cloid = r.get("cloid")
                if cloid and str(cloid).startswith(MANUAL_PFX):
                    continue
                try:
                    qty = float(r.get("sz") or 0)
                    px = float(r.get("px") or 0)
                    fee = float(r.get("fee") or 0)
                    cp = r.get("closed_pnl")
                    cp_f = float(cp) if cp is not None else 0.0
                    if qty <= 0 or px <= 0:
                        continue
                    fills.append({
                        "sym": sym,
                        "ts": parse_ts(ts_str).timestamp(),
                        "side": r.get("side"),
                        "qty": qty,
                        "px": px,
                        "fee": fee,
                        "closed_pnl": cp_f,
                        "cloid": cloid or "",
                        "crossed": bool(r.get("crossed", False)),
                    })
                except (TypeError, ValueError):
                    continue

            elif ev == "sizing_runtime_shadow":
                # Captures the contemporaneous mid at the moment the strategy
                # decided to fire. Matched to subsequent hl_order_submitted by
                # nearest-prior (symbol, side) within SHADOW_LOOKBACK_S.
                try:
                    mid = r.get("mid")
                    if mid is None:
                        continue
                    mid_f = float(mid)
                    if not math.isfinite(mid_f) or mid_f <= 0:
                        continue
                    side = r.get("side")
                    if side not in ("buy", "sell"):
                        continue
                    shadows[sym].append((parse_ts(ts_str).timestamp(), side, mid_f))
                except (TypeError, ValueError):
                    continue

    return {"obi": dict(obi), "sends": dict(sends),
            "sends_by_cloid": sends_by_cloid, "fills": fills,
            "shadows": dict(shadows)}


def attach_mid_to_sends(
    sends: dict[str, list[dict]],
    shadows: dict[str, list[tuple[float, str, float]]],
    window_s: float = SHADOW_LOOKBACK_S,
) -> int:
    """For each send, attach `mid_at_send` from the nearest-prior matching
    shadow event on (symbol, side) within `window_s` seconds. Mutates `sends`
    in place. Returns count of sends that received a mid attachment.

    A send without a matching shadow keeps `mid_at_send = None`; the η fit
    will skip those fills as `no_mid_reference`.
    """
    n_attached = 0
    for sym, sym_sends in sends.items():
        sym_shadows = sorted(shadows.get(sym, []), key=lambda x: x[0])
        if not sym_shadows:
            for s in sym_sends:
                s["mid_at_send"] = None
            continue
        for s in sym_sends:
            send_ts = s["ts"]
            send_side = s["side"]
            best_ts = -1.0
            best_mid: float | None = None
            for sh_ts, sh_side, sh_mid in sym_shadows:
                if sh_ts > send_ts:
                    break
                if send_ts - sh_ts > window_s:
                    continue
                if sh_side != send_side:
                    continue
                if sh_ts > best_ts:
                    best_ts = sh_ts
                    best_mid = sh_mid
            s["mid_at_send"] = best_mid
            if best_mid is not None:
                n_attached += 1
    return n_attached


# ── OU fit on OBI ──────────────────────────────────────────────────────────

def fit_ou(series: list[tuple[float, float]], dt_s: int = DT_S) -> dict:
    """Fit dY = -β(Y - Ȳ) dt + σ dW via AR(1) on a uniform Δt grid.

    Returns dict with keys: status, n_samples, beta, sigma, half_life_s, ar1_b.
    Status values: ok | thin_sample | non_stationary | negative_b | no_variance.
    """
    if len(series) < N_MIN_OU:
        return {"status": "thin_sample", "n_samples": len(series),
                "beta": None, "sigma": None, "half_life_s": None, "ar1_b": None}

    series = sorted(series, key=lambda x: x[0])
    t0 = series[0][0]
    t_end = series[-1][0]
    n_bins = max(1, int((t_end - t0) / dt_s) + 1)
    grid: list[float | None] = [None] * n_bins
    for ts, val in series:
        idx = min(n_bins - 1, max(0, int((ts - t0) / dt_s)))
        grid[idx] = val
    # Forward-fill missing bins; drop leading Nones.
    last: float | None = None
    for i in range(n_bins):
        if grid[i] is not None:
            last = grid[i]
        else:
            grid[i] = last
    filled: list[float] = [v for v in grid if v is not None]

    if len(filled) < N_MIN_OU:
        return {"status": "thin_sample", "n_samples": len(filled),
                "beta": None, "sigma": None, "half_life_s": None, "ar1_b": None}

    Y = filled[:-1]
    Yp = filled[1:]
    n = len(Y)
    mean_Y = sum(Y) / n
    mean_Yp = sum(Yp) / n
    num = sum((y - mean_Y) * (yp - mean_Yp) for y, yp in zip(Y, Yp))
    den = sum((y - mean_Y) ** 2 for y in Y)
    if den <= 0:
        return {"status": "no_variance", "n_samples": n,
                "beta": None, "sigma": None, "half_life_s": None, "ar1_b": None}
    b = num / den
    a = mean_Yp - b * mean_Y

    if b >= 1.0:
        return {"status": "non_stationary", "n_samples": n, "ar1_b": b,
                "beta": None, "sigma": None, "half_life_s": None}
    if b <= 0.0:
        return {"status": "negative_b", "n_samples": n, "ar1_b": b,
                "beta": None, "sigma": None, "half_life_s": None}

    beta = -math.log(b) / dt_s
    half_life_s = math.log(2.0) / beta if beta > 0 else None

    eps = [yp - (a + b * y) for y, yp in zip(Y, Yp)]
    sigma_eps = math.sqrt(sum(e * e for e in eps) / max(n - 2, 1))
    sigma = sigma_eps * math.sqrt(2 * beta / (1 - math.exp(-2 * beta * dt_s)))

    return {"status": "ok", "n_samples": n, "beta": beta, "sigma": sigma,
            "half_life_s": half_life_s, "ar1_b": b}


# ── η fit on slippage ──────────────────────────────────────────────────────

def fit_eta(
    fills: list[dict],
    sends: dict[str, list[dict]],
    sends_by_cloid: dict[tuple[str, str], dict],
    reference: str = "mid",
) -> dict:
    """Per-symbol median slippage and η = median_slip_bps / median_notional.

    Reference price for slippage:
      reference="mid"  → fill_px vs send.mid_at_send (BL-correct; structural).
                         Drops fills whose send has no matching shadow event.
      reference="limit"→ fill_px vs send.px  (legacy; pre-walked by IOC ladder).

    Adverse convention: positive slip = trader paid worse than reference.
      buy:  slip_bps = (fill_px - ref_px) / ref_px × 10000
      sell: slip_bps = (ref_px - fill_px) / ref_px × 10000

    Also returns a per-symbol counter of fills with no resolvable mid (only
    populated when reference="mid").
    """
    per_sym: dict[str, list[tuple[float, float]]] = defaultdict(list)
    per_sym_no_mid: dict[str, int] = defaultdict(int)
    per_sym_no_send: dict[str, int] = defaultdict(int)

    for fill in fills:
        sym = fill["sym"]
        sent: dict | None = None
        if fill["cloid"]:
            sent = sends_by_cloid.get((sym, fill["cloid"]))
        if sent is None:
            candidates = [
                s for s in sends.get(sym, [])
                if s["side"] == fill["side"] and 0 <= fill["ts"] - s["ts"] <= NEAREST_PRIOR_S
            ]
            if candidates:
                sent = max(candidates, key=lambda s: s["ts"])
        if sent is None:
            per_sym_no_send[sym] += 1
            continue

        if reference == "mid":
            ref_px = sent.get("mid_at_send")
            if ref_px is None or not math.isfinite(ref_px) or ref_px <= 0:
                per_sym_no_mid[sym] += 1
                continue
        elif reference == "limit":
            ref_px = sent["px"]
            if ref_px <= 0:
                continue
        else:
            raise ValueError(f"unknown reference: {reference!r}")

        if fill["side"] == "buy":
            slip_bps = (fill["px"] - ref_px) / ref_px * 10000.0
        elif fill["side"] == "sell":
            slip_bps = (ref_px - fill["px"]) / ref_px * 10000.0
        else:
            continue

        notional = fill["qty"] * fill["px"]
        if notional <= 0:
            continue
        per_sym[sym].append((slip_bps, notional))

    out: dict[str, dict] = {}
    all_syms = set(per_sym.keys()) | set(per_sym_no_mid.keys()) | set(per_sym_no_send.keys())
    for sym in all_syms:
        samples = per_sym.get(sym, [])
        if len(samples) < N_MIN_ETA:
            out[sym] = {
                "status": "thin_sample",
                "n_pairs": len(samples),
                "median_slip_bps": None,
                "median_notional": None,
                "eta_bps_per_dollar": None,
                "p25_slip_bps": None,
                "p75_slip_bps": None,
                "n_no_mid": per_sym_no_mid.get(sym, 0),
                "n_no_send": per_sym_no_send.get(sym, 0),
            }
            continue
        slips = [s[0] for s in samples]
        notionals = [s[1] for s in samples]
        med_slip = statistics.median(slips)
        med_not = statistics.median(notionals)
        eta = med_slip / med_not if med_not > 0 else None
        out[sym] = {
            "status": "ok",
            "n_pairs": len(samples),
            "median_slip_bps": med_slip,
            "median_notional": med_not,
            "eta_bps_per_dollar": eta,
            "p25_slip_bps": percentile(slips, 0.25),
            "p75_slip_bps": percentile(slips, 0.75),
            "n_no_mid": per_sym_no_mid.get(sym, 0),
            "n_no_send": per_sym_no_send.get(sym, 0),
        }
    return out


# ── time-in-market via FIFO entry/exit pairing ─────────────────────────────

def time_in_market(fills: list[dict]) -> dict[str, list[float]]:
    """Pair entry (closed_pnl=0) with exit (closed_pnl≠0) FIFO per symbol.

    Cross-side pairing: a buy entry pairs with a sell exit and vice versa.
    Returns per-symbol list of hold times in seconds.
    """
    pending: dict[tuple[str, str], list[float]] = defaultdict(list)
    holds: dict[str, list[float]] = defaultdict(list)

    for fill in sorted(fills, key=lambda f: f["ts"]):
        sym = fill["sym"]
        side = fill["side"]
        if fill["closed_pnl"] == 0.0:
            pending[(sym, side)].append(fill["ts"])
        else:
            opp = "sell" if side == "buy" else "buy"
            if pending[(sym, opp)]:
                entry_ts = pending[(sym, opp)].pop(0)
                holds[sym].append(fill["ts"] - entry_ts)
    return dict(holds)


# ── output ─────────────────────────────────────────────────────────────────

def fmt_sec(s: float | None) -> str:
    if s is None:
        return "—"
    if s < 60:
        return f"{s:.0f}s"
    if s < 3600:
        return f"{s/60:.1f}m"
    return f"{s/3600:.2f}h"


def main() -> int:
    ap = argparse.ArgumentParser(description="Bechler-Ludkovski (β, σ, η) calibration")
    ap.add_argument("--log", default=str(DEFAULT_LOG),
                    help="Engine fill log (default: logs/hl_engine.jsonl)")
    ap.add_argument("--window-days", type=float, default=DEFAULT_WINDOW_DAYS,
                    help="Look-back window in days (default 7)")
    ap.add_argument("--start-ts", default=None,
                    help="Override window start (ISO UTC); takes precedence over --window-days")
    ap.add_argument("--top", type=int, default=30,
                    help="Per-symbol table row count (top N by signal_tick count)")
    ap.add_argument("--verbose", action="store_true",
                    help="Surface diagnostic stderr")
    ap.add_argument(
        "--taker-only",
        action="store_true",
        help=(
            "Filter η fit to taker fills only (crossed=true). REQUIRED for a "
            "Bechler-Ludkovski-compatible η: the BL formula models temporary "
            "impact as a strictly positive friction term, which only makes "
            "sense for liquidity-consuming (taker) trades. Maker fills "
            "produce negative η — that's opportunity cost, not impact, and "
            "needs a separate Avellaneda-Stoikov-style framework. β and σ "
            "fits are unaffected by this flag (they come from signal_tick.obi)."
        ),
    )
    ap.add_argument(
        "--reference",
        choices=("mid", "limit"),
        default="mid",
        help=(
            "Reference price for slippage. 'mid' joins each fill to the "
            "contemporaneous sizing_runtime_shadow.mid (BL-correct, captures "
            "true temporary impact). 'limit' uses the submitted limit_px "
            "(legacy; biased by our IOC pre-walk). Default 'mid'."
        ),
    )
    args = ap.parse_args()

    log_path = Path(args.log)
    if not log_path.exists():
        print(f"calibrate_bl_params: missing log {log_path}", file=sys.stderr)
        return 2

    if args.start_ts:
        start_ts = args.start_ts
        if not start_ts.endswith("Z") and "+" not in start_ts:
            start_ts += "Z"
    else:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.window_days)
        start_ts = cutoff.isoformat()

    if args.verbose:
        print(f"[verbose] start_ts={start_ts}", file=sys.stderr)
        print(f"[verbose] reading {log_path} ...", file=sys.stderr)

    data = collect(log_path, start_ts)
    obi = data["obi"]
    sends = data["sends"]
    sends_by_cloid = data["sends_by_cloid"]
    all_fills = data["fills"]
    shadows = data["shadows"]

    # Attach mid_at_send to every send via nearest-prior shadow event match.
    n_sends_total = sum(len(v) for v in sends.values())
    n_sends_with_mid = attach_mid_to_sends(sends, shadows)
    n_shadows_total = sum(len(v) for v in shadows.values())

    n_obi_total = sum(len(v) for v in obi.values())
    n_fills_all = len(all_fills)
    n_fills_taker = sum(1 for f in all_fills if f.get("crossed"))
    n_fills_maker = n_fills_all - n_fills_taker

    # η fit gets the (possibly-filtered) subset; β/σ/holds use the full set.
    if args.taker_only:
        fills_for_eta = [f for f in all_fills if f.get("crossed")]
    else:
        fills_for_eta = all_fills
    n_fills_eta = len(fills_for_eta)

    ou_fits = {sym: fit_ou(series) for sym, series in obi.items()}
    eta_fits = fit_eta(fills_for_eta, sends, sends_by_cloid, reference=args.reference)
    holds = time_in_market(all_fills)

    # ── header ─────────────────────────────────────────────────────────────
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    suffix_parts = []
    if args.reference == "mid":
        suffix_parts.append("MID-REFERENCED η")
    elif args.reference == "limit":
        suffix_parts.append("LIMIT-REFERENCED η")
    if args.taker_only:
        suffix_parts.append("TAKER-ONLY")
    title_suffix = (" — " + " · ".join(suffix_parts)) if suffix_parts else ""
    print(f"# Bechler-Ludkovski (β, σ, η) calibration{title_suffix}")
    print()
    print(f"**Window:** `{start_ts}` → `{now_iso}`")
    print(f"**Source:** `{log_path}` (read-only)")
    print(f"**Excluded:** manual-cloid fills (prefix `{MANUAL_PFX}`)")
    if args.reference == "mid":
        print(f"**η reference:** `sizing_runtime_shadow.mid` joined nearest-prior to send (window {SHADOW_LOOKBACK_S}s)")
    else:
        print("**η reference:** `hl_order_submitted.limit_px` (legacy — biased by IOC pre-walk)")
    if args.taker_only:
        print("**η filter:** taker fills only (`crossed=true`). β/σ unaffected.")
    print()
    print("| Counter | Value |")
    print("|---|---|")
    print(f"| `signal_tick` obi observations | {n_obi_total:,} |")
    print(f"| `sizing_runtime_shadow` events | {n_shadows_total:,} |")
    print(f"| `hl_order_submitted` | {n_sends_total:,} |")
    print(f"|   ↳ with matching shadow.mid | {n_sends_with_mid:,} ({100*n_sends_with_mid/max(n_sends_total,1):.0f}%) |")
    print(f"| `hl_fill_received` (manual excluded) | {n_fills_all:,} |")
    print(f"|   ↳ taker (`crossed=true`) | {n_fills_taker:,} |")
    print(f"|   ↳ maker (`crossed=false`) | {n_fills_maker:,} |")
    print(f"| Fills used for η fit | {n_fills_eta:,} ({'taker-only' if args.taker_only else 'all'}) |")
    print(f"| OU resampling Δt | {DT_S}s |")
    print(f"| Min OU samples | {N_MIN_OU} |")
    print(f"| Min η pairs | {N_MIN_ETA} |")
    print(f"| Symbols with any obi data | {len(obi)} |")
    print(f"| Symbols with any fill | {len({f['sym'] for f in all_fills})} |")
    print()

    # ── per-symbol table ───────────────────────────────────────────────────
    syms_sorted = sorted(obi.keys(), key=lambda s: -len(obi[s]))
    head_top = min(args.top, len(syms_sorted))

    print(f"## Per-symbol fits (top {head_top} by `signal_tick` count)")
    print()
    print("| symbol | class | n_obi | β (1/s) | half-life | σ | η (bps/$) | med slip (bps) | n_fills | med hold |")
    print("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for sym in syms_sorted[:head_top]:
        cls = "hip3" if is_hip3(sym) else "native"
        ou = ou_fits.get(sym, {})
        et = eta_fits.get(sym, {})
        hd = holds.get(sym, [])
        beta = ou.get("beta")
        beta_str = f"{beta:.5f}" if beta is not None else f"_{ou.get('status','?')}_"
        hl = ou.get("half_life_s")
        hl_str = fmt_sec(hl)
        sg = ou.get("sigma")
        sg_str = f"{sg:.3f}" if sg is not None else "—"
        eta_v = et.get("eta_bps_per_dollar")
        eta_str = f"{eta_v:.6f}" if eta_v is not None else f"_{et.get('status','no_data')}_"
        med_slip = et.get("median_slip_bps")
        med_slip_str = f"{med_slip:+.2f}" if med_slip is not None else "—"
        n_fills_sym = et.get("n_pairs", 0)
        med_hold = statistics.median(hd) if hd else None
        med_hold_str = fmt_sec(med_hold)
        print(f"| `{sym}` | {cls} | {len(obi[sym])} | {beta_str} | {hl_str} | {sg_str} | {eta_str} | {med_slip_str} | {n_fills_sym} | {med_hold_str} |")
    print()

    # ── class summary ──────────────────────────────────────────────────────
    print("## Class-level medians (valid fits only)")
    print()
    print("| class | n_β | β median | half-life median | σ median | n_η | η median (bps/$) | med slip (bps) |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|")
    for cls_name in ("native", "hip3"):
        match = (cls_name == "hip3")
        betas = [ou_fits[s]["beta"] for s in obi if is_hip3(s) == match and ou_fits[s].get("beta") is not None]
        sigmas = [ou_fits[s]["sigma"] for s in obi if is_hip3(s) == match and ou_fits[s].get("sigma") is not None]
        etas = [eta_fits[s]["eta_bps_per_dollar"] for s in eta_fits
                if is_hip3(s) == match and eta_fits[s].get("eta_bps_per_dollar") is not None]
        slips = [eta_fits[s]["median_slip_bps"] for s in eta_fits
                 if is_hip3(s) == match and eta_fits[s].get("median_slip_bps") is not None]
        if betas:
            beta_med = statistics.median(betas)
            sigma_med = statistics.median(sigmas)
            hl_med = math.log(2) / beta_med if beta_med > 0 else None
            eta_med_str = f"{statistics.median(etas):.6f}" if etas else "—"
            slip_med_str = f"{statistics.median(slips):+.2f}" if slips else "—"
            print(f"| {cls_name} | {len(betas)} | {beta_med:.5f} | {fmt_sec(hl_med)} | {sigma_med:.3f} | {len(etas)} | {eta_med_str} | {slip_med_str} |")
        else:
            print(f"| {cls_name} | 0 | — | — | — | 0 | — | — |")
    print()

    # ── status histogram ───────────────────────────────────────────────────
    statuses: dict[str, int] = defaultdict(int)
    for ou in ou_fits.values():
        statuses["ou_" + ou.get("status", "?")] += 1
    for et in eta_fits.values():
        statuses["eta_" + et.get("status", "?")] += 1
    print("## Status histogram")
    print()
    print("| key | count |")
    print("|---|---:|")
    for k in sorted(statuses):
        print(f"| `{k}` | {statuses[k]} |")
    print()

    # ── footer ─────────────────────────────────────────────────────────────
    print("## Provenance / interpretation")
    print()
    print("- β is the OU mean-reversion rate of the OBI process. half-life = ln(2)/β. Use this as the time-scale knob in the BL closed-form (paper: 1409.2618).")
    print("- σ is the OU driving-noise vol; conditioned on β so it's directly comparable across symbols.")
    print("- η is bps of slippage per $1 of notional traded. Adverse-side convention (positive = trader paid more than expected). Linear approximation; the empirical crypto-LOB law is concave (square-root), captured later by 2503.04323.")
    if args.reference == "mid":
        print(f"- ✅ Slippage reference is `sizing_runtime_shadow.mid` joined to each send by nearest-prior (symbol, side) within {SHADOW_LOOKBACK_S}s. This is the structural BL-η reference: positive = true temporary impact (spread crossing + book walk).")
    else:
        print("- ⚠️ Slippage reference is `limit_px` (legacy). Our IOC ladder pre-walks the limit so even taker fills land inside it; η here is biased toward zero/negative. Re-run with `--reference mid` for the BL-correct calibration.")
    if not args.taker_only:
        print("- ⚠️  Without `--taker-only`, η is computed across BOTH maker and taker fills. Maker fills produce structurally negative η vs mid (you capture the spread, not pay it). For Bechler-Ludkovski use, combine `--reference mid --taker-only`.")
    else:
        print("- ✅ η fit restricted to taker fills (`crossed=true`). Result is the structural temporary-impact coefficient for the BL Riccati formula's spread-crossing branch.")
    print("- median hold (last column) is the empirical FIFO-paired entry→exit duration. Compare to half-life: well-aligned ⇒ OU model consistent with strategy turnover.")
    print("- Status `ok` = fit accepted; `thin_sample` = below threshold; `non_stationary` / `negative_b` / `no_variance` = OU model rejected.")
    print()
    print("**This run did NOT write to `config/risk_params.py`.** Operator must review these numbers and authorize separately before they touch the live engine.")
    print()
    print(f"_Generated: {now_iso}_  ·  _Source: scripts/calibrate_bl_params.py_")
    return 0


if __name__ == "__main__":
    sys.exit(main())
