#!/usr/bin/env python3
"""Gate-aware Z-entry replay harness with PnL attribution (Phase 1-3).

Fork of scripts/z_entry_replay.py that applies the full live-engine gate chain
to the signal_tick stream, emits per-gate PnL attribution, and enforces an
anti-degeneracy MIN_TRADES floor. Mirrors strategy/signals.py:413-611 and
hl_engine.py:1657-1668 gate order.

Gate chain (entry):
  G1 obi_gate         direction * obi >= OBI_THETA
  G2 trend_regime     long ok if close >= trend_sma; short ok if close <= trend_sma
  G3 flip_guard       block entry if opposite-side position already open
  G4 momentum_dedup   block mean-reversion entry if momentum tag holds the symbol
                      (modeled here by blocking entries while |z| >= Z_MOMENTUM_ENTRY
                       and direction agrees with momentum thesis)
  G5 z_threshold      z <= Z_ENTRY or z >= Z_SHORT_ENTRY

Exit chain:
  X1 z_revert         z back through Z_EXIT / Z_EXIT_SHORT
  X2 ratchet          armed at |z_4h| >= SHOCK_ARM, exits in tranches at peak ± N*STEP
  X3 time_stop        age >= MAX_HOLD_S
  X4 stop_loss        adverse move <= -STOP_LOSS_PCT

Deterministic. Reads logs + bars only. No network. No live order path touched.

============================================================================
VALIDATION REQUIRED BEFORE AUTORESEARCH (Phase 4 -- DEFERRED)

This harness has NOT YET been validated against live realized PnL. Per the
plan (proud-conjuring-pebble.md), any /autoresearch run against this harness
requires first passing validate_replay_fit.py with portfolio rho >= 0.80 and
all per-symbol rho >= 0.70. Phase 4 is deferred until the current shock book
closes and `position_close_complete` events accumulate in logs/hl_engine.jsonl.
============================================================================

ENV:
  CONSTRAINED_FILE=<path>           override path for z-entry params JSON
                                    (used by /autoresearch runs)
  OBI_GATE_FILE=<path>              override path for config/gates/obi.json
  TREND_GATE_FILE=<path>            override path for config/gates/trend.json
  RATCHET_GATE_FILE=<path>          override path for config/gates/ratchet.json
  MOMENTUM_GATE_FILE=<path>         override path for config/gates/momentum.json
  REPLAY_SYMBOLS=a,b,c              restrict to these symbols (default: all)
  REPLAY_MIN_TICKS=50               skip symbols with fewer ticks
  GATED_WRITE_BASELINE=1            write per-symbol pnl baseline
  GATED_BASELINE_COUNT=<int>        override baseline trade count (else use
                                    current run's trade count for floor)
  GATED_MIN_TRADES_FRAC=0.30        floor ratio (default 0.30)
  GATED_ATTRIBUTION=<path>          write attribution.jsonl here
                                    (default autoresearch_gated/attribution.jsonl)
"""

from __future__ import annotations

import json
import os
import sqlite3
from bisect import bisect_left, bisect_right
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / "hl_engine.jsonl"
BARS = ROOT / "data" / "cache" / "bars.sqlite"
if not BARS.exists():
    # fallback to data/bars.sqlite if cache version missing
    alt = ROOT / "data" / "bars.sqlite"
    if alt.exists():
        BARS = alt

PARAMS_DEFAULT = ROOT / "config" / "z_entry_params.json"
OBI_GATE_DEFAULT = ROOT / "config" / "gates" / "obi.json"
TREND_GATE_DEFAULT = ROOT / "config" / "gates" / "trend.json"
RATCHET_GATE_DEFAULT = ROOT / "config" / "gates" / "ratchet.json"
MOM_GATE_DEFAULT = ROOT / "config" / "gates" / "momentum.json"

OUT_DIR = ROOT / "autoresearch_gated"
BASELINE_FILE = OUT_DIR / "_baseline_per_symbol.json"
BASELINE_COUNT_FILE = OUT_DIR / "_baseline_trade_count.json"
ATTRIBUTION_FILE = Path(os.environ.get("GATED_ATTRIBUTION", str(OUT_DIR / "attribution.jsonl")))

# ── Constants (copied inline from strategy/signals.py + z_entry_replay.py) ─
NOTIONAL_PER_TRADE = 750.0
STOP_LOSS_PCT = 0.010          # X4
TIME_STOP_S = 60 * 60          # X3: 1h MAX_HOLD_S
# X1 dampers: imported from strategy.signals so /autoresearch sees fresh values
# whenever the agent edits the constrained file.
import sys as _sys
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in _sys.path:
    _sys.path.insert(0, _REPO_ROOT)
from strategy.signals import (  # noqa: E402
    MIN_HOLD_FOR_REVERT_S,
    MIN_REVERT_BPS,
)


def _load_z4h_exit_map() -> dict[str, tuple[float, float]]:
    """Parse Z4H_EXIT_<COIN>=long,short from env.sh. Keys are normalized
    symbol bases (e.g. 'ZEC', 'xyz:MSTR')."""
    env_path = Path(__file__).resolve().parent.parent / "env.sh"
    if not env_path.exists():
        return {}
    out: dict[str, tuple[float, float]] = {}
    for line in env_path.read_text().splitlines():
        if "Z4H_EXIT_" not in line or line.strip().startswith("#"):
            continue
        import re as _re
        m = _re.search(r'Z4H_EXIT_([A-Za-z0-9_]+)=([^\s#]+)', line)
        if not m:
            continue
        raw_sym = m.group(1)
        vals = m.group(2).strip().strip('"').split(",")
        if len(vals) != 2:
            continue
        try:
            ex_long = float(vals[0])
            ex_short = float(vals[1])
        except Exception:
            continue
        # env var form: Z4H_EXIT_xyz_MSTR → sym "xyz:MSTR"; Z4H_EXIT_ZEC → "ZEC"
        if raw_sym.startswith("xyz_") or raw_sym.startswith("para_") or raw_sym.startswith("hyna_") or raw_sym.startswith("vntl_") or raw_sym.startswith("km_") or raw_sym.startswith("flx_") or raw_sym.startswith("cash_") or raw_sym.startswith("abcd_"):
            prefix, _, tail = raw_sym.partition("_")
            sym = f"{prefix}:{tail}"
        else:
            sym = raw_sym
        out[sym] = (ex_long, ex_short)
    return out


Z4H_EXIT_MAP = _load_z4h_exit_map()
MIN_TICKS = int(os.environ.get("REPLAY_MIN_TICKS", "50"))
SYMBOL_FILTER = os.environ.get("REPLAY_SYMBOLS", "all")

WRITE_BASELINE = os.environ.get("GATED_WRITE_BASELINE", "0") == "1"
MIN_TRADES_FRAC = float(os.environ.get("GATED_MIN_TRADES_FRAC", "0.30"))


# ── Config loading ─────────────────────────────────────────────────────────
def _load_json(env_name: str, default_path: Path, fallback: dict) -> dict:
    p = os.environ.get(env_name)
    path = Path(p) if p else default_path
    if not path.exists():
        return dict(fallback)
    try:
        return json.loads(path.read_text())
    except Exception:
        return dict(fallback)


_params = _load_json(
    "CONSTRAINED_FILE",
    PARAMS_DEFAULT,
    {
        "z_entry": -1.25,
        "z_exit": -0.50,
        "z_short_entry": 1.25,
        "z_exit_short": 0.50,
        "per_symbol_overrides": {},
    },
)
Z_ENTRY = float(_params.get("z_entry", -1.25))
Z_EXIT = float(_params.get("z_exit", -0.50))
Z_SHORT_ENTRY = float(_params.get("z_short_entry", 1.25))
Z_EXIT_SHORT = float(_params.get("z_exit_short", 0.50))
OVERRIDES = _params.get("per_symbol_overrides", {}) or {}

_obi_cfg = _load_json("OBI_GATE_FILE", OBI_GATE_DEFAULT, {"OBI_THETA": 0.0, "OBI_DIRECTION_MODE": "signed"})
OBI_THETA = float(_obi_cfg.get("OBI_THETA", 0.0))
OBI_DIRECTION_MODE = str(_obi_cfg.get("OBI_DIRECTION_MODE", "signed"))

_trend_cfg = _load_json("TREND_GATE_FILE", TREND_GATE_DEFAULT, {"TREND_MA_WINDOW": 240, "Z_4H_MOMENTUM_THRESHOLD": 2.0})
TREND_MA_WINDOW = int(_trend_cfg.get("TREND_MA_WINDOW", 240))
Z_4H_MOMENTUM_THRESHOLD = float(_trend_cfg.get("Z_4H_MOMENTUM_THRESHOLD", 2.0))

_ratchet_cfg = _load_json("RATCHET_GATE_FILE", RATCHET_GATE_DEFAULT, {"SHOCK_ARM": 4.0, "SHOCK_STEP": 1.0, "TRANCHES": 3})
SHOCK_ARM = float(_ratchet_cfg.get("SHOCK_ARM", 4.0))
SHOCK_STEP = float(_ratchet_cfg.get("SHOCK_STEP", 1.0))
RATCHET_TRANCHES = int(_ratchet_cfg.get("TRANCHES", 3))

_mom_cfg = _load_json("MOMENTUM_GATE_FILE", MOM_GATE_DEFAULT, {"Z_MOMENTUM_ENTRY": 3.0})
Z_MOMENTUM_ENTRY = float(_mom_cfg.get("Z_MOMENTUM_ENTRY", 3.0))


def thresholds_for(sym: str) -> tuple[float, float, float, float]:
    ov = OVERRIDES.get(sym)
    if isinstance(ov, dict):
        return (
            float(ov.get("z_entry", Z_ENTRY)),
            float(ov.get("z_exit", Z_EXIT)),
            float(ov.get("z_short_entry", Z_SHORT_ENTRY)),
            float(ov.get("z_exit_short", Z_EXIT_SHORT)),
        )
    return (Z_ENTRY, Z_EXIT, Z_SHORT_ENTRY, Z_EXIT_SHORT)


# ── Helpers: ts, symbol normalisation, bars loader, mark_at ───────────────
def _parse_ts(ts) -> int:
    if isinstance(ts, (int, float)):
        return int(ts * 1000) if ts < 1e12 else int(ts)
    return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)


def _norm(sym: str) -> str:
    return (sym or "").replace("/USD", "").replace("/USDC", "")


def load_bars() -> dict[str, tuple[list[int], list[float]]]:
    if not BARS.exists():
        return {}
    out: dict[str, tuple[list[int], list[float]]] = {}
    with sqlite3.connect(BARS) as c:
        for iv in ("15m", "1h"):
            try:
                syms = c.execute(
                    "SELECT DISTINCT symbol FROM bars WHERE interval=?", (iv,)
                ).fetchall()
            except sqlite3.OperationalError:
                continue
            for (sym,) in syms:
                if sym in out:
                    continue
                rows = c.execute(
                    "SELECT t_close_ms, c FROM bars WHERE symbol=? AND interval=? ORDER BY t_close_ms",
                    (sym, iv),
                ).fetchall()
                if rows:
                    out[sym] = ([r[0] for r in rows], [r[1] for r in rows])
    return out


def mark_at(bars, sym, ts_ms):
    b = bars.get(sym)
    if not b:
        return None
    ts_list, closes = b
    i = bisect_left(ts_list, ts_ms)
    if i == 0:
        return closes[0]
    if i >= len(ts_list):
        return closes[-1]
    return closes[i] if (ts_list[i] - ts_ms) < (ts_ms - ts_list[i - 1]) else closes[i - 1]


def trend_sma_at(bars, sym, ts_ms) -> float | None:
    """Rolling SMA of last TREND_MA_WINDOW 15m closes strictly before ts_ms.

    Mirrors strategy/signals.py:598-599 where trend_buf is a 240-bar buffer.
    Returns None if fewer than TREND_MA_WINDOW bars available.
    """
    b = bars.get(sym)
    if not b:
        return None
    ts_list, closes = b
    i = bisect_right(ts_list, ts_ms)
    if i < TREND_MA_WINDOW:
        return None
    s = sum(closes[i - TREND_MA_WINDOW : i])
    return s / TREND_MA_WINDOW


# ── Tick loader (extended: obi + z_4h fields) ─────────────────────────────
def load_ticks() -> dict[str, list[tuple[int, float, float, float]]]:
    """Returns per-symbol list of (ts_ms, z, obi, z_4h).

    obi defaults to 0.0 when missing. z_4h defaults to NaN→sentinel when
    missing (treat as unavailable for ratchet gate).

    Optional window filter via env:
      REPLAY_FROM_MS / REPLAY_TO_MS — keep only ticks where FROM <= ts < TO.
    """
    import os as _os
    _from = _os.environ.get("REPLAY_FROM_MS")
    _to = _os.environ.get("REPLAY_TO_MS")
    from_ms = int(_from) if _from else None
    to_ms = int(_to) if _to else None
    ticks: dict[str, list[tuple[int, float, float, float]]] = defaultdict(list)
    NAN = float("nan")
    with LOG.open() as f:
        for line in f:
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get("event") != "signal_tick":
                continue
            sym = _norm(o.get("symbol") or o.get("coin") or "")
            z = o.get("z")
            ts = o.get("timestamp")
            if not sym or z is None or ts is None:
                continue
            try:
                z = float(z)
            except Exception:
                continue
            if z != z:
                continue
            ts_ms = _parse_ts(ts)
            if from_ms is not None and ts_ms < from_ms:
                continue
            if to_ms is not None and ts_ms >= to_ms:
                continue
            obi_raw = o.get("obi")
            try:
                obi = float(obi_raw) if obi_raw is not None else 0.0
            except Exception:
                obi = 0.0
            z4_raw = o.get("z_4h")
            try:
                z4 = float(z4_raw) if z4_raw is not None else NAN
            except Exception:
                z4 = NAN
            ticks[sym].append((ts_ms, z, obi, z4))
    for s in ticks:
        ticks[s].sort()
    return ticks


# ── Attribution sink ──────────────────────────────────────────────────────
class AttributionSink:
    """Collects (gate, symbol, ts, counterfactual_pnl) rows.

    Counterfactual PnL = what the trade would have paid if the gate had
    passed and the G5 z-threshold check had fired. Uses a fixed forward-mark
    lookahead (to the NEXT tick whose z satisfies the exit rule, else time-stop).

    This is a lightweight estimator intended to rank gates, not to produce
    exact PnL (which depends on state that we can't replay for rejected
    trades -- by definition we don't know what else would have happened).
    """

    def __init__(self, path: Path):
        self.path = path
        self.rows: list[dict] = []
        self.gate_counts: dict[str, int] = defaultdict(int)
        self.gate_saved: dict[str, float] = defaultdict(float)

    def record(self, gate: str, sym: str, ts: int, cf_pnl: float, side: int, z: float):
        self.gate_counts[gate] += 1
        # "saved" = positive if the skipped trade would have LOST money.
        self.gate_saved[gate] += -cf_pnl
        self.rows.append(
            {
                "gate": gate,
                "gate_rejected": True,
                "symbol": sym,
                "ts_ms": ts,
                "side": side,
                "z": round(z, 4),
                "cf_pnl": round(cf_pnl, 4),
            }
        )

    def flush(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w") as f:
            for r in self.rows:
                f.write(json.dumps(r, separators=(",", ":")) + "\n")


def _counterfactual_pnl(
    ticks_sym: list[tuple[int, float, float, float]],
    start_idx: int,
    side: int,
    bars,
    sym: str,
    z_exit: float,
    z_exit_short: float,
) -> float:
    """Estimate PnL if a trade had been opened at ticks[start_idx].

    Walks forward until z_revert / stop_loss / time_stop fires, mirroring the
    live exit chain (X1, X3, X4). Does NOT simulate ratchet for CF (would
    require peak_abs state we don't carry for CF branches).
    """
    ts0, _, _, _ = ticks_sym[start_idx]
    entry_mark = mark_at(bars, sym, ts0)
    if entry_mark is None or entry_mark <= 0:
        return 0.0
    qty = NOTIONAL_PER_TRADE / entry_mark
    for ts, z, _obi, _z4 in ticks_sym[start_idx + 1 :]:
        age_s = (ts - ts0) / 1000.0
        cur = mark_at(bars, sym, ts)
        if cur is None:
            continue
        adverse = (cur - entry_mark) / entry_mark * side
        if side == 1 and z >= z_exit:
            return (cur - entry_mark) * side * qty
        if side == -1 and z <= z_exit_short:
            return (cur - entry_mark) * side * qty
        if adverse <= -STOP_LOSS_PCT:
            return (cur - entry_mark) * side * qty
        if age_s >= TIME_STOP_S:
            return (cur - entry_mark) * side * qty
    # Ran out of ticks -- mark at last available
    last_ts = ticks_sym[-1][0]
    cur = mark_at(bars, sym, last_ts) or entry_mark
    return (cur - entry_mark) * side * qty


# ── Gate-aware simulation ─────────────────────────────────────────────────
def simulate_symbol_gated(
    sym: str,
    ticks_sym: list[tuple[int, float, float, float]],
    bars,
    thr: tuple[float, float, float, float],
    sink: AttributionSink,
) -> tuple[float, int, dict]:
    """Return (total_pnl, num_trades, per_exit_reason_counts).

    Stateful gates: flip_guard (current side), ratchet (peak_abs, tranches_done).
    """
    z_entry, z_exit, z_short_entry, z_exit_short = thr
    total = 0.0
    n_trades = 0
    pos = None  # dict with keys: side, entry_ts, entry_mark, qty, ratchet (dict|None)
    last_closed_side: int = 0  # for flip_guard approximation across ticks
    exit_reasons: dict[str, int] = defaultdict(int)

    for idx, (ts, z, obi, z4) in enumerate(ticks_sym):
        # ── Exit processing (if in position) ──────────────────────────────
        if pos is not None:
            side = pos["side"]
            entry_ts = pos["entry_ts"]
            entry_mark = pos["entry_mark"]
            qty = pos["qty"]
            age_s = (ts - entry_ts) / 1000.0
            cur = mark_at(bars, sym, ts)
            if cur is None:
                continue
            adverse = (cur - entry_mark) / entry_mark * side
            exit_reason = None

            # X1 z_revert (with live-engine dampers)
            z_revert_candidate = False
            if side == 1 and z >= z_exit:
                z_revert_candidate = True
            elif side == -1 and z <= z_exit_short:
                z_revert_candidate = True
            if z_revert_candidate:
                # Z4H_EXIT override: for patient-hold symbols, block z-revert
                # exit unless |z_4h| has reached the systematic threshold on the
                # profitable side.
                ex = Z4H_EXIT_MAP.get(sym)
                if ex is not None and z4 == z4:
                    ex_long, ex_short = ex
                    patient_block = (side == 1 and z4 < ex_long) or (side == -1 and z4 > ex_short)
                    if patient_block:
                        z_revert_candidate = False
                # Damper: require min hold AND min favorable move
                favorable = (cur - entry_mark) / entry_mark * side
                if z_revert_candidate and (age_s < MIN_HOLD_FOR_REVERT_S or favorable < MIN_REVERT_BPS):
                    z_revert_candidate = False
                if z_revert_candidate:
                    exit_reason = "z_revert"

            # X2 ratchet (port of scripts/shock_ratchet.py:234-289)
            if exit_reason is None and z4 == z4:  # z4 not NaN
                rs = pos.get("ratchet")
                if rs is None:
                    if side * z4 >= SHOCK_ARM:
                        pos["ratchet"] = {
                            "peak_abs": abs(z4),
                            "peak_sign": side,
                            "tranches_done": 0,
                        }
                else:
                    # update peak
                    if side * z4 > rs["peak_abs"]:
                        rs["peak_abs"] = side * z4
                    retrace = rs["peak_abs"] - side * z4
                    done = rs["tranches_done"]
                    if done == 0 and retrace >= SHOCK_STEP:
                        rs["tranches_done"] = 1
                        exit_reason = "ratchet_1"
                    elif done == 1 and retrace >= 2 * SHOCK_STEP:
                        rs["tranches_done"] = 2
                        exit_reason = "ratchet_2"
                    elif done == 2 and (
                        retrace >= RATCHET_TRANCHES * SHOCK_STEP or side * z4 <= 0
                    ):
                        rs["tranches_done"] = RATCHET_TRANCHES
                        exit_reason = "ratchet_final"

            # X4 stop_loss
            if exit_reason is None and adverse <= -STOP_LOSS_PCT:
                exit_reason = "stop_loss"

            # X3 time_stop
            if exit_reason is None and age_s >= TIME_STOP_S:
                exit_reason = "time_stop"

            if exit_reason is not None:
                # Partial-ratchet intermediate tranches don't flatten in live engine,
                # but for PnL replay we model each ratchet trigger as a scaled
                # exit of 1/TRANCHES of the position and keep the residual open.
                # However, to keep the harness simple AND conservative, we exit
                # the full position on any ratchet trigger (models worst-case
                # attribution; a more granular model is out of scope for phase 1).
                pnl = (cur - entry_mark) * side * qty
                total += pnl
                n_trades += 1
                last_closed_side = side
                exit_reasons[exit_reason] += 1
                pos = None
            continue  # don't try to open on the same tick we exited on

        # ── Entry processing ──────────────────────────────────────────────
        # G5 candidate-direction check first (cheapest), so the upstream
        # gates only waste work on ticks that COULD open a trade.
        want_long = z <= z_entry
        want_short = z >= z_short_entry
        if not (want_long or want_short):
            continue

        # Record direction sign for gate checks (long=+1, short=-1)
        side = 1 if want_long else -1

        # G1 obi_gate: signed(direction · obi) >= OBI_THETA
        # Per strategy/signals.py:589-592:
        #   long  requires  obi >  +OBI_THETA
        #   short requires  obi < -OBI_THETA
        # We generalise to direction * obi > OBI_THETA.
        if OBI_DIRECTION_MODE == "signed":
            if side * obi <= OBI_THETA:
                cf = _counterfactual_pnl(ticks_sym, idx, side, bars, sym, z_exit, z_exit_short)
                sink.record("obi_gate", sym, ts, cf, side, z)
                continue

        # G2 trend_regime: long ok if close >= sma; short ok if close <= sma
        sma = trend_sma_at(bars, sym, ts)
        cur_close = mark_at(bars, sym, ts)
        if sma is not None and cur_close is not None:
            if side == 1 and cur_close < sma:
                cf = _counterfactual_pnl(ticks_sym, idx, side, bars, sym, z_exit, z_exit_short)
                sink.record("trend_regime", sym, ts, cf, side, z)
                continue
            if side == -1 and cur_close > sma:
                cf = _counterfactual_pnl(ticks_sym, idx, side, bars, sym, z_exit, z_exit_short)
                sink.record("trend_regime", sym, ts, cf, side, z)
                continue

        # G3 flip_guard: if last closed trade was same-day opposite side, block
        # (approximation: we don't have concurrent positions; we only guard
        # against immediate reopen in the opposite direction of the last
        # trade we just closed).
        if last_closed_side != 0 and last_closed_side == -side:
            cf = _counterfactual_pnl(ticks_sym, idx, side, bars, sym, z_exit, z_exit_short)
            sink.record("flip_guard", sym, ts, cf, side, z)
            # clear after one tick so we don't permanently block
            last_closed_side = 0
            continue

        # G4 momentum_dedup: block mean-reversion entry while the z is extreme
        # enough that momentum-tag would own the symbol.
        if abs(z) >= Z_MOMENTUM_ENTRY:
            cf = _counterfactual_pnl(ticks_sym, idx, side, bars, sym, z_exit, z_exit_short)
            sink.record("momentum_dedup", sym, ts, cf, side, z)
            continue

        # G5 z_threshold (already satisfied by want_long/want_short gate). OPEN.
        m = mark_at(bars, sym, ts)
        if m is None or m <= 0:
            continue
        qty = NOTIONAL_PER_TRADE / m
        pos = {
            "side": side,
            "entry_ts": ts,
            "entry_mark": m,
            "qty": qty,
            "ratchet": None,
        }

    return total, n_trades, dict(exit_reasons)


# ── Main ──────────────────────────────────────────────────────────────────
def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    bars = load_bars()
    ticks = load_ticks()
    allowed = None
    if SYMBOL_FILTER != "all":
        allowed = {_norm(s) for s in SYMBOL_FILTER.split(",")}

    sink = AttributionSink(ATTRIBUTION_FILE)

    per_sym: dict[str, float] = {}
    per_sym_trades: dict[str, int] = {}
    per_sym_exit_reasons: dict[str, dict[str, int]] = {}
    total = 0.0
    skipped_no_bars = 0
    skipped_few_ticks = 0

    # iterate deterministically
    for sym in sorted(ticks.keys()):
        ticks_sym = ticks[sym]
        if len(ticks_sym) < MIN_TICKS:
            skipped_few_ticks += 1
            continue
        if sym not in bars:
            skipped_no_bars += 1
            continue
        if allowed is not None and sym not in allowed:
            continue
        thr = thresholds_for(sym)
        pnl, n, reasons = simulate_symbol_gated(sym, ticks_sym, bars, thr, sink)
        per_sym[sym] = pnl
        per_sym_trades[sym] = n
        per_sym_exit_reasons[sym] = reasons
        total += pnl

    sink.flush()

    # ── Guard: worst per-symbol regression vs baseline ─────────────────────
    worst_sym = None
    worst_delta = 0.0
    baseline = None
    if BASELINE_FILE.exists() and not WRITE_BASELINE:
        try:
            baseline = json.loads(BASELINE_FILE.read_text())
        except Exception:
            baseline = None
    if baseline:
        for sym, pnl in per_sym.items():
            base = float(baseline.get(sym, pnl))
            d = pnl - base
            if d < worst_delta:
                worst_delta = d
                worst_sym = sym

    if WRITE_BASELINE:
        BASELINE_FILE.write_text(json.dumps(per_sym, indent=2, sort_keys=True))
        BASELINE_COUNT_FILE.write_text(
            json.dumps({"total_trades": sum(per_sym_trades.values())}, indent=2)
        )
        print(f"# wrote baseline to {BASELINE_FILE}")
        print(f"# wrote baseline-trade-count to {BASELINE_COUNT_FILE}")

    _persym_out = os.environ.get("REPLAY_PERSYM_OUT")
    if _persym_out:
        Path(_persym_out).write_text(json.dumps(per_sym, indent=2, sort_keys=True))
        print(f"# wrote per-symbol pnl to {_persym_out}")

    # ── Anti-degeneracy floor ──────────────────────────────────────────────
    total_trades = sum(per_sym_trades.values())
    env_bc = os.environ.get("GATED_BASELINE_COUNT")
    if env_bc is not None:
        try:
            baseline_count = int(env_bc)
        except Exception:
            baseline_count = total_trades
    elif BASELINE_COUNT_FILE.exists():
        try:
            baseline_count = int(json.loads(BASELINE_COUNT_FILE.read_text()).get("total_trades", 0))
        except Exception:
            baseline_count = total_trades
    else:
        baseline_count = total_trades  # first run establishes own floor at 100% of itself

    min_trades = int(MIN_TRADES_FRAC * baseline_count) if baseline_count > 0 else 0

    if total_trades < min_trades:
        score = float("-inf")
    else:
        score = total

    secondary = (total / total_trades) if total_trades > 0 else 0.0

    # ── Report ─────────────────────────────────────────────────────────────
    print(
        f"# z_entry={Z_ENTRY} z_exit={Z_EXIT} z_short={Z_SHORT_ENTRY} z_exit_short={Z_EXIT_SHORT} "
        f"overrides={len(OVERRIDES)} symbols={len(per_sym)} "
        f"skipped_no_bars={skipped_no_bars} skipped_few_ticks={skipped_few_ticks}"
    )
    print(
        f"# gates: OBI_THETA={OBI_THETA} TREND_MA_WINDOW={TREND_MA_WINDOW} "
        f"SHOCK_ARM={SHOCK_ARM} SHOCK_STEP={SHOCK_STEP} Z_MOM_ENTRY={Z_MOMENTUM_ENTRY}"
    )
    print(
        f"# total_trades={total_trades} baseline_trade_count={baseline_count} "
        f"min_trades_floor={min_trades} sim_pnl=${total:+.2f}"
    )
    print(f"GUARD_WORST_SYM_DELTA: {worst_delta:.4f}  ({worst_sym})")
    print(f"SECONDARY: {secondary:+.4f}  (pnl_per_trade)")
    if score == float("-inf"):
        print("SCORE: -inf")
    else:
        print(f"SCORE: {score:.4f}")

    # ── Attribution block ──────────────────────────────────────────────────
    print("\n=== GATE ATTRIBUTION ===")
    for gate in ("obi_gate", "trend_regime", "flip_guard", "momentum_dedup"):
        cnt = sink.gate_counts.get(gate, 0)
        saved = sink.gate_saved.get(gate, 0.0)
        avg = (saved / cnt) if cnt > 0 else 0.0
        print(f"  {gate:<16s} rejected {cnt:>7d}  saved ${saved:+9.2f}  avg_per_reject ${avg:+.3f}")
    print(
        f"  {'FIRED':<16s} entries  {total_trades:>7d}  SCORE  ${total:+9.2f}  "
        f"$/trade ${secondary:+.3f}"
    )

    # top-8 table
    top = sorted(per_sym.items(), key=lambda kv: -abs(kv[1]))[:8]
    print("\ntop-8 |pnl|:")
    for s, pnl in top:
        base_pnl = float(baseline.get(s, 0.0)) if baseline else 0.0
        d = f"Δ={pnl-base_pnl:+6.2f}" if baseline else ""
        reasons = per_sym_exit_reasons.get(s, {})
        rstr = ",".join(f"{k}={v}" for k, v in sorted(reasons.items())) or "-"
        print(f"  {s:18s} trades={per_sym_trades[s]:4d} sim={pnl:+8.2f} {d}  exits[{rstr}]")


if __name__ == "__main__":
    main()
