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
from collections import defaultdict
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
ATTRIBUTION_FILE = Path(
    os.environ.get("GATED_ATTRIBUTION", str(OUT_DIR / "attribution.jsonl"))
)

# ── Constants (copied inline from strategy/signals.py + z_entry_replay.py) ─
NOTIONAL_PER_TRADE = 750.0
STOP_LOSS_PCT = 0.010  # X4
TIME_STOP_S = 60 * 60  # X3: 1h MAX_HOLD_S
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

# ── G0: regime pause gate (optional, off by default) ───────────────────────
# Mirrors hl_engine.py:140-141 + REGIME_PROXIES gating: if any proxy's 1h
# absolute return crosses REGIME_1H_ABS_RETURN, all entries are blocked for
# REGIME_PAUSE_SECONDS. Read tunables from env first (for grid sweeps),
# falling back to the literal defaults in hl_engine.py.
import re as _regex  # noqa: E402

REGIME_PROXIES_REPLAY = ("BTC", "xyz:SP500")


def _read_regime_default(key: str, fallback: float) -> float:
    """Parse the default literal from hl_engine.py (e.g. '0.015' or '3600')."""
    try:
        src = (Path(_REPO_ROOT) / "hl_engine.py").read_text()
        m = _regex.search(rf'{key}"[^"]*"([0-9.]+)"', src)
        if m:
            return float(m.group(1))
    except OSError:
        pass
    return float(fallback)


REGIME_GATE = os.environ.get("REGIME_GATE", "0") == "1"
REGIME_1H_ABS_RETURN = float(
    os.environ.get(
        "REGIME_1H_ABS_RETURN",
        str(_read_regime_default("REGIME_1H_ABS_RETURN", 0.015)),
    )
)
REGIME_PAUSE_SECONDS = int(
    float(
        os.environ.get(
            "REGIME_PAUSE_SECONDS",
            str(_read_regime_default("REGIME_PAUSE_SECONDS", 3600)),
        )
    )
)

# Populated by main() after bars are loaded; consulted by simulate_symbol_gated.
_regime_trips: list[tuple[int, int]] = []

# Optional opens-emission file handle (for diagnose_entry_alignment.py and
# similar tooling). Set when REPLAY_OPENS_OUT env var points at a writable
# path. None disables emission entirely (zero overhead).
_OPENS_OUT_FH = None
_TRADES_OUT_FH = None


def build_regime_trips(bars: dict) -> list[tuple[int, int]]:
    """Return a sorted list of (trip_ts_ms, pause_until_ms) for any tick where
    a REGIME_PROXIES_REPLAY symbol's |1h return| crosses REGIME_1H_ABS_RETURN.

    Uses the same bar interval as the rest of the replay (15m when
    available, 1h fallback). For 15m bars, 1h-window = 4 bars back; for 1h
    bars, 1 bar back.
    """
    trips: list[tuple[int, int]] = []
    pause_ms = REGIME_PAUSE_SECONDS * 1000
    for sym in REGIME_PROXIES_REPLAY:
        if sym not in bars:
            continue
        ts_list, closes = bars[sym]
        if len(ts_list) < 2:
            continue
        # Detect bar interval from the first two timestamps (in ms).
        bar_ms = ts_list[1] - ts_list[0]
        window_bars = max(1, int(round(3600_000 / bar_ms)))
        for i in range(window_bars, len(ts_list)):
            prev = closes[i - window_bars]
            cur = closes[i]
            if prev <= 0:
                continue
            ret_abs = abs(cur - prev) / prev
            if ret_abs >= REGIME_1H_ABS_RETURN:
                trips.append((ts_list[i], ts_list[i] + pause_ms))
    trips.sort()
    return trips


def is_regime_paused(trips: list[tuple[int, int]], ts_ms: int) -> bool:
    """True iff `ts_ms` is inside any (trip_ts, pause_until_ts) window in trips.

    O(log n) via bisect on the trip_ts axis.
    """
    if not trips:
        return False
    trip_ts_only = [t[0] for t in trips]  # could pre-extract; cheap on small lists
    j = bisect_right(trip_ts_only, ts_ms) - 1
    while j >= 0:
        trip_ts, pause_until = trips[j]
        if pause_until > ts_ms:
            return True
        # Earlier trips have older pause_until; still walk back while we may
        # find an overlap (windows can stack via max() in the engine; for
        # replay we treat any covering trip as a hit).
        if trip_ts + REGIME_PAUSE_SECONDS * 1000 < ts_ms - REGIME_PAUSE_SECONDS * 1000:
            break
        j -= 1
    return False


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

        m = _re.search(r"Z4H_EXIT_([A-Za-z0-9_]+)=([^\s#]+)", line)
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
        if (
            raw_sym.startswith("xyz_")
            or raw_sym.startswith("para_")
            or raw_sym.startswith("hyna_")
            or raw_sym.startswith("vntl_")
            or raw_sym.startswith("km_")
            or raw_sym.startswith("flx_")
            or raw_sym.startswith("cash_")
            or raw_sym.startswith("abcd_")
        ):
            prefix, _, tail = raw_sym.partition("_")
            sym = f"{prefix}:{tail}"
        else:
            sym = raw_sym
        out[sym] = (ex_long, ex_short)
    return out


Z4H_EXIT_MAP = _load_z4h_exit_map()
MIN_TICKS = int(os.environ.get("REPLAY_MIN_TICKS", "50"))
SYMBOL_FILTER = os.environ.get("REPLAY_SYMBOLS", "all")

# Universe-filter mode (residual decomposition Phase 1):
#   all                     — every symbol with ticks (current behavior)
#   live_fills_window       — DIAGNOSTIC: symbols with >=N hl_fill_received
#                             in [REPLAY_FROM_MS, REPLAY_TO_MS). Lookahead.
#   entry_signal_window     — DIAGNOSTIC: symbols with >=1 entry_signal in
#                             window. Lookahead but weaker than fills.
#   configured_live         — DEPLOYABLE: HL_UNIVERSE + HIP3_UNIVERSE env
#                             vars (production trading universe). No
#                             lookahead.
REPLAY_UNIVERSE_MODE = os.environ.get("REPLAY_UNIVERSE", "all")
REPLAY_MIN_LIVE_FILLS = int(os.environ.get("REPLAY_MIN_LIVE_FILLS", "1"))
# Cardinality-suppression gates (Phase 3 — replay over-trades by ~20×):
#   MIN_REENTRY_COOLDOWN_S   block re-entry on a symbol within N seconds
#                             of its last close. Default 0 = off.
#   MAX_OPENS_PER_SYMBOL_PER_DAY  hard daily-bucket cap on opens per symbol.
#                                  Default 0 = off.
MIN_REENTRY_COOLDOWN_S = int(os.environ.get("MIN_REENTRY_COOLDOWN_S", "0"))
MAX_OPENS_PER_SYMBOL_PER_DAY = int(os.environ.get("MAX_OPENS_PER_SYMBOL_PER_DAY", "0"))
# REENTRY_COOLDOWN_BY_SYMBOL — JSON file with per-symbol cooldown overrides.
# When set, supersedes MIN_REENTRY_COOLDOWN_S per symbol.
REENTRY_COOLDOWN_BY_SYMBOL_FILE = os.environ.get("REENTRY_COOLDOWN_BY_SYMBOL", "")


def _load_reentry_cooldown_by_symbol(path: str) -> dict[str, int]:
    """Parse JSON config; return symbol→cooldown_s map.
    Default is taken from MIN_REENTRY_COOLDOWN_S (or 0 if absent).
    Group symbols apply uniform cooldown_s; overrides win over groups.
    """
    if not path:
        return {}
    try:
        cfg = json.loads(Path(path).read_text())
    except Exception as e:
        print(
            f"# WARN: cooldown-by-symbol load failed: {e}",
            file=__import__("sys").stderr,
        )
        return {}
    out: dict[str, int] = {}
    for grp in (cfg.get("groups") or {}).values():
        try:
            cd = int(grp.get("cooldown_s", 0))
        except (TypeError, ValueError):
            continue
        for sym in grp.get("symbols") or []:
            s = (sym or "").replace("/USD", "").replace("/USDC", "")
            if s:
                out[s] = cd
    for sym, cd in (cfg.get("overrides") or {}).items():
        s = (sym or "").replace("/USD", "").replace("/USDC", "")
        try:
            out[s] = int(cd)
        except (TypeError, ValueError):
            continue
    return out


REENTRY_COOLDOWN_BY_SYMBOL: dict[str, int] = _load_reentry_cooldown_by_symbol(
    REENTRY_COOLDOWN_BY_SYMBOL_FILE
)
REENTRY_COOLDOWN_DEFAULT_FROM_FILE: int = 0
if REENTRY_COOLDOWN_BY_SYMBOL_FILE:
    try:
        _cfg = json.loads(Path(REENTRY_COOLDOWN_BY_SYMBOL_FILE).read_text())
        REENTRY_COOLDOWN_DEFAULT_FROM_FILE = int(_cfg.get("default", 0))
    except Exception:
        pass

# Ratchet exit model (Phase 2 partial-close patch):
#   full     existing behavior — every ratchet trigger fully closes the
#            position. Numerically identical to baseline.
#   tranche  ratchet_1/ratchet_2 reduce by RATCHET_TRANCHE_FRAC * initial
#            qty; ratchet_final closes the remainder. Position stays open
#            between tranches and remains exposed to z_revert / stop_loss /
#            time_stop on subsequent ticks.
RATCHET_EXIT_MODEL = os.environ.get("RATCHET_EXIT_MODEL", "full")
RATCHET_TRANCHE_FRAC = float(os.environ.get("RATCHET_TRANCHE_FRAC", "0.333333"))
RATCHET_TRANCHES_TOTAL_FLAG = int(os.environ.get("RATCHET_TRANCHES_TOTAL", "3"))
MIN_POSITION_QTY = float(os.environ.get("MIN_POSITION_QTY", "1e-12"))

# Held-source for `configured_or_held` mode:
#   reconstruct_from_fills    walk hl_fill_received from log start to window
#                              start; symbols with non-zero net position are
#                              "held". No lookahead. May miss positions
#                              opened before log start.
#   current_user_state         query HL API now. Lookahead — only valid for
#                              forward/paper-soak windows ending within ~6h.
REPLAY_HELD_SOURCE = os.environ.get("REPLAY_HELD_SOURCE", "reconstruct_from_fills")

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

_obi_cfg = _load_json(
    "OBI_GATE_FILE",
    OBI_GATE_DEFAULT,
    {"OBI_THETA": 0.0, "OBI_DIRECTION_MODE": "signed"},
)
OBI_THETA = float(_obi_cfg.get("OBI_THETA", 0.0))
OBI_DIRECTION_MODE = str(_obi_cfg.get("OBI_DIRECTION_MODE", "signed"))

_trend_cfg = _load_json(
    "TREND_GATE_FILE",
    TREND_GATE_DEFAULT,
    {"TREND_MA_WINDOW": 240, "Z_4H_MOMENTUM_THRESHOLD": 2.0},
)
TREND_MA_WINDOW = int(_trend_cfg.get("TREND_MA_WINDOW", 240))
Z_4H_MOMENTUM_THRESHOLD = float(_trend_cfg.get("Z_4H_MOMENTUM_THRESHOLD", 2.0))

_ratchet_cfg = _load_json(
    "RATCHET_GATE_FILE",
    RATCHET_GATE_DEFAULT,
    {"SHOCK_ARM": 4.0, "SHOCK_STEP": 1.0, "TRANCHES": 3},
)
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
    return (
        closes[i] if (ts_list[i] - ts_ms) < (ts_ms - ts_list[i - 1]) else closes[i - 1]
    )


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


# ── Position state helpers (Phase 2 partial-close) ────────────────────────
def _side_sign(side) -> float:
    """Accepts +1/-1 ints or 'long'/'short' strings; returns ±1.0."""
    if isinstance(side, str):
        return 1.0 if side.lower() == "long" else -1.0
    return 1.0 if side >= 0 else -1.0


def reduce_position(
    position: dict, close_qty: float, px: float, ts_ms: int, reason: str
) -> float:
    """Realize PnL on `close_qty` units at `px` and shrink position.

    Returns the incremental realized PnL (signed). Caller adds this to the
    per-symbol total.
    """
    close_qty = min(close_qty, position["qty"])
    pnl = _side_sign(position["side"]) * close_qty * (px - position["entry_vwap"])
    position["qty"] -= close_qty
    position["realized_pnl"] += pnl
    position["reductions"].append(
        {
            "ts_ms": ts_ms,
            "qty": close_qty,
            "px": px,
            "reason": reason,
            "pnl": pnl,
            "remaining_qty": position["qty"],
        }
    )
    return pnl


def close_remaining(position: dict, px: float, ts_ms: int, reason: str) -> float:
    """Close any remaining qty. Returns the incremental PnL realized on
    this final reduction (NOT cumulative — caller already accounted for
    earlier tranche PnL via reduce_position calls)."""
    if position["qty"] <= 0:
        return 0.0
    return reduce_position(position, position["qty"], px, ts_ms, reason)


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
    last_close_ts_ms: int = 0  # for G6 reentry cooldown
    opens_per_day: dict[str, int] = defaultdict(int)  # for G7 daily cap
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
                    patient_block = (side == 1 and z4 < ex_long) or (
                        side == -1 and z4 > ex_short
                    )
                    if patient_block:
                        z_revert_candidate = False
                # Damper: require min hold AND min favorable move
                favorable = (cur - entry_mark) / entry_mark * side
                if z_revert_candidate and (
                    age_s < MIN_HOLD_FOR_REVERT_S or favorable < MIN_REVERT_BPS
                ):
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
                # Phase 2: route every exit through reduce_position /
                # close_remaining. In RATCHET_EXIT_MODEL=full this is
                # numerically identical to the previous full-close pnl;
                # in tranche mode, ratchet_1/ratchet_2 reduce by a fixed
                # fraction of initial_qty and leave the position open
                # for subsequent exits on the remainder.
                is_ratchet = exit_reason.startswith("ratchet")
                is_final_ratchet = exit_reason == "ratchet_final"
                tranche_mode = (
                    RATCHET_EXIT_MODEL == "tranche"
                    and is_ratchet
                    and not is_final_ratchet
                )
                if tranche_mode:
                    fired = pos["ratchet_tranches_fired"]
                    if fired < RATCHET_TRANCHES_TOTAL_FLAG - 1:
                        close_qty = pos["initial_qty"] * RATCHET_TRANCHE_FRAC
                    else:
                        close_qty = pos["qty"]
                    increment_pnl = reduce_position(
                        pos, close_qty, cur, ts, exit_reason
                    )
                    total += increment_pnl
                    pos["ratchet_tranches_fired"] += 1
                    exit_reasons[exit_reason] += 1
                    if pos["qty"] <= MIN_POSITION_QTY:
                        # tail flush — closing reduction emptied the book
                        n_trades += 1
                        last_closed_side = side
                        last_close_ts_ms = ts
                        pos = None
                    # else: position remains open with reduced qty
                else:
                    # Full close — z_revert / stop_loss / time_stop / any
                    # ratchet exit when model=full / ratchet_final under
                    # tranche mode.
                    increment_pnl = reduce_position(
                        pos, pos["qty"], cur, ts, exit_reason
                    )
                    total += increment_pnl
                    n_trades += 1
                    last_closed_side = side
                    last_close_ts_ms = ts
                    exit_reasons[exit_reason] += 1
                    if _TRADES_OUT_FH is not None:
                        _TRADES_OUT_FH.write(
                            json.dumps(
                                {
                                    "entry_ts": pos["entry_ts_ms"],
                                    "exit_ts": ts,
                                    "symbol": sym,
                                    "side": pos["side"],
                                    "initial_qty": pos["initial_qty"],
                                    "entry_vwap": pos["entry_vwap"],
                                    "exit_px": cur,
                                    "pnl": pos["realized_pnl"],
                                    "reason": exit_reason,
                                    "n_reductions": len(pos["reductions"]),
                                    "ratchet_tranches_fired": pos[
                                        "ratchet_tranches_fired"
                                    ],
                                },
                                separators=(",", ":"),
                            )
                            + "\n"
                        )
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

        # G0 regime_pause: portfolio-level pause when BTC/SP500 1h |return|
        # crossed REGIME_1H_ABS_RETURN within the last REGIME_PAUSE_SECONDS.
        # Off by default; enabled with REGIME_GATE=1 env (autoresearch driver).
        if REGIME_GATE and is_regime_paused(_regime_trips, ts):
            cf = _counterfactual_pnl(
                ticks_sym, idx, side, bars, sym, z_exit, z_exit_short
            )
            sink.record("regime_pause", sym, ts, cf, side, z)
            continue

        # G1 obi_gate: signed(direction · obi) >= OBI_THETA
        # Per strategy/signals.py:589-592:
        #   long  requires  obi >  +OBI_THETA
        #   short requires  obi < -OBI_THETA
        # We generalise to direction * obi > OBI_THETA.
        if OBI_DIRECTION_MODE == "signed":
            if side * obi <= OBI_THETA:
                cf = _counterfactual_pnl(
                    ticks_sym, idx, side, bars, sym, z_exit, z_exit_short
                )
                sink.record("obi_gate", sym, ts, cf, side, z)
                continue

        # G2 trend_regime: long ok if close >= sma; short ok if close <= sma
        sma = trend_sma_at(bars, sym, ts)
        cur_close = mark_at(bars, sym, ts)
        if sma is not None and cur_close is not None:
            if side == 1 and cur_close < sma:
                cf = _counterfactual_pnl(
                    ticks_sym, idx, side, bars, sym, z_exit, z_exit_short
                )
                sink.record("trend_regime", sym, ts, cf, side, z)
                continue
            if side == -1 and cur_close > sma:
                cf = _counterfactual_pnl(
                    ticks_sym, idx, side, bars, sym, z_exit, z_exit_short
                )
                sink.record("trend_regime", sym, ts, cf, side, z)
                continue

        # G3 flip_guard: if last closed trade was same-day opposite side, block
        # (approximation: we don't have concurrent positions; we only guard
        # against immediate reopen in the opposite direction of the last
        # trade we just closed).
        if last_closed_side != 0 and last_closed_side == -side:
            cf = _counterfactual_pnl(
                ticks_sym, idx, side, bars, sym, z_exit, z_exit_short
            )
            sink.record("flip_guard", sym, ts, cf, side, z)
            # clear after one tick so we don't permanently block
            last_closed_side = 0
            continue

        # G4 momentum_dedup: block mean-reversion entry while the z is extreme
        # enough that momentum-tag would own the symbol.
        if abs(z) >= Z_MOMENTUM_ENTRY:
            cf = _counterfactual_pnl(
                ticks_sym, idx, side, bars, sym, z_exit, z_exit_short
            )
            sink.record("momentum_dedup", sym, ts, cf, side, z)
            continue

        # G6 reentry_cooldown — phase-3 cardinality suppression.
        # Per-symbol map (if loaded) overrides the global threshold.
        if REENTRY_COOLDOWN_BY_SYMBOL:
            cooldown_s = REENTRY_COOLDOWN_BY_SYMBOL.get(
                sym, REENTRY_COOLDOWN_DEFAULT_FROM_FILE
            )
        else:
            cooldown_s = MIN_REENTRY_COOLDOWN_S
        if cooldown_s > 0 and last_close_ts_ms > 0:
            if (ts - last_close_ts_ms) < cooldown_s * 1000:
                cf = _counterfactual_pnl(
                    ticks_sym, idx, side, bars, sym, z_exit, z_exit_short
                )
                sink.record("reentry_cooldown", sym, ts, cf, side, z)
                continue

        # G7 max_opens_per_day — phase-3 hard cardinality cap
        day_key = datetime.utcfromtimestamp(ts / 1000).strftime("%Y-%m-%d")
        if MAX_OPENS_PER_SYMBOL_PER_DAY > 0:
            if opens_per_day[day_key] >= MAX_OPENS_PER_SYMBOL_PER_DAY:
                cf = _counterfactual_pnl(
                    ticks_sym, idx, side, bars, sym, z_exit, z_exit_short
                )
                sink.record("max_opens_day", sym, ts, cf, side, z)
                continue

        # G5 z_threshold (already satisfied by want_long/want_short gate). OPEN.
        m = mark_at(bars, sym, ts)
        if m is None or m <= 0:
            continue
        qty = NOTIONAL_PER_TRADE / m
        opens_per_day[day_key] += 1
        pos = {
            "symbol": sym,
            "side": side,  # int ±1 (kept for back-compat with downstream code)
            "qty": qty,
            "initial_qty": qty,
            "entry_px": m,
            "entry_vwap": m,
            "entry_ts_ms": ts,
            "realized_pnl": 0.0,
            "reductions": [],
            "ratchet_tranches_fired": 0,
            "ratchet": None,
            # back-compat aliases — existing exit code reads these:
            "entry_ts": ts,
            "entry_mark": m,
        }
        # Optional opens emission for diagnostic tooling (e.g. entry alignment).
        if _OPENS_OUT_FH is not None:
            _OPENS_OUT_FH.write(
                json.dumps(
                    {
                        "ts": ts,
                        "symbol": sym,
                        "side": side,
                        "z": z,
                        "mark": m,
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )

    return total, n_trades, dict(exit_reasons)


def _load_universe_live_fills(from_ms: int, to_ms: int, min_fills: int = 1) -> set[str]:
    """Symbols with >=min_fills hl_fill_received events in [from_ms, to_ms).
    Diagnostic only — uses live outcome inside the validation window."""
    from collections import Counter

    cnt: Counter = Counter()
    with LOG.open() as f:
        for line in f:
            if '"hl_fill_received"' not in line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get("event") != "hl_fill_received":
                continue
            ts_raw = o.get("timestamp")
            if isinstance(ts_raw, (int, float)):
                ts_ms = int(ts_raw * 1000) if ts_raw < 1e12 else int(ts_raw)
            elif isinstance(ts_raw, str):
                try:
                    s = ts_raw[:-1] + "+00:00" if ts_raw.endswith("Z") else ts_raw
                    ts_ms = int(
                        __import__("datetime").datetime.fromisoformat(s).timestamp()
                        * 1000
                    )
                except Exception:
                    continue
            else:
                continue
            if ts_ms < from_ms or ts_ms >= to_ms:
                continue
            sym = _norm(o.get("symbol") or o.get("coin") or "")
            if sym:
                cnt[sym] += 1
    return {s for s, n in cnt.items() if n >= min_fills}


def _load_universe_entry_signals(from_ms: int, to_ms: int) -> set[str]:
    """Symbols with >=1 entry_signal event in the window. Diagnostic."""
    out: set[str] = set()
    with LOG.open() as f:
        for line in f:
            if '"entry_signal"' not in line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get("event") != "entry_signal":
                continue
            ts_raw = o.get("timestamp")
            if isinstance(ts_raw, str):
                try:
                    s = ts_raw[:-1] + "+00:00" if ts_raw.endswith("Z") else ts_raw
                    ts_ms = int(
                        __import__("datetime").datetime.fromisoformat(s).timestamp()
                        * 1000
                    )
                except Exception:
                    continue
            elif isinstance(ts_raw, (int, float)):
                ts_ms = int(ts_raw * 1000) if ts_raw < 1e12 else int(ts_raw)
            else:
                continue
            if ts_ms < from_ms or ts_ms >= to_ms:
                continue
            sym = _norm(o.get("symbol") or o.get("coin") or "")
            if sym:
                out.add(sym)
    return out


def _load_universe_configured_live() -> set[str]:
    """Production trading universe = HL_UNIVERSE + HIP3_UNIVERSE env vars
    + config/pairs_whitelist.json (universe + leg_a/leg_b of every pair).
    Auto_topup also actively trades ZEC by design.

    Non-lookahead — what the engine ecosystem *would* trade ex ante.
    """
    out: set[str] = set()
    for var in ("HL_UNIVERSE", "HIP3_UNIVERSE"):
        v = os.environ.get(var) or ""
        for tok in v.split(","):
            s = _norm(tok.strip())
            if s:
                out.add(s)
    # pairs whitelist — separate hl_pairs.py engine trades these.
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
    # auto_topup ZEC watcher (deployed; pause-on-ratchet shipped 2026-04-26)
    out.add("ZEC")
    return out


def _load_held_at_start_from_fills(start_ms: int) -> set[str]:
    """Reconstruct symbols with non-zero net position at start_ms by
    walking hl_fill_received events from log start to start_ms.

    LIMITATION: misses positions opened before the engine log begins.
    Caller should warn if start_ms < earliest log timestamp.
    """
    pos: dict[str, float] = defaultdict(float)
    with LOG.open() as f:
        for line in f:
            if '"hl_fill_received"' not in line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get("event") != "hl_fill_received":
                continue
            ts_raw = o.get("timestamp", "")
            try:
                if isinstance(ts_raw, str):
                    s = ts_raw[:-1] + "+00:00" if ts_raw.endswith("Z") else ts_raw
                    ts_ms = int(datetime.fromisoformat(s).timestamp() * 1000)
                else:
                    ts_ms = int(ts_raw * 1000) if ts_raw < 1e12 else int(ts_raw)
            except Exception:
                continue
            if ts_ms >= start_ms:
                break  # log is chronological; everything after is in-window or after
            sym = _norm(o.get("symbol") or o.get("coin") or "")
            if not sym:
                continue
            try:
                sz = float(o.get("sz", 0) or 0)
            except (TypeError, ValueError):
                continue
            side = (o.get("side") or "").lower()
            if side == "buy":
                pos[sym] += sz
            elif side == "sell":
                pos[sym] -= sz
    eps = 1e-6
    return {s for s, q in pos.items() if abs(q) > eps}


def _load_held_at_start_from_engine_log(start_ms: int) -> set[str]:
    """Use hl_position_reconciled events: take the LAST event per symbol
    before start_ms; symbols with non-zero szi are held at start.

    This is the most accurate non-lookahead source because the engine
    emits ~per-minute reconciliation snapshots from HL state.
    """
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
            ts_raw = o.get("timestamp", "")
            try:
                if isinstance(ts_raw, str):
                    s = ts_raw[:-1] + "+00:00" if ts_raw.endswith("Z") else ts_raw
                    ts_ms = int(datetime.fromisoformat(s).timestamp() * 1000)
                else:
                    ts_ms = int(ts_raw * 1000) if ts_raw < 1e12 else int(ts_raw)
            except Exception:
                continue
            if ts_ms >= start_ms:
                break  # log is chronological
            sym = _norm(o.get("symbol") or o.get("coin") or "")
            if not sym:
                continue
            try:
                szi = float(o.get("szi", 0) or 0)
            except (TypeError, ValueError):
                continue
            last_szi[sym] = szi
    eps = 1e-6
    return {s for s, q in last_szi.items() if abs(q) > eps}


def _load_held_at_start_from_hl_history(
    start_ms: int, lookback_days: int = 30
) -> set[str]:
    """Reconstruct positions at start_ms by walking HL API user_fills_by_time
    over [start_ms - lookback_days, start_ms). Non-lookahead w.r.t. the
    validation window. Use when local log doesn't span far enough back."""
    addr = os.environ.get("HL_WALLET_ADDRESS")
    if not addr:
        env_path = ROOT / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("HL_WALLET_ADDRESS="):
                    addr = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not addr:
        return set()
    try:
        from hyperliquid.info import Info
        from hyperliquid.utils import constants
    except ImportError:
        return set()
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    from_ms = start_ms - lookback_days * 86_400_000
    try:
        fills = (
            info.user_fills_by_time(addr, from_ms, start_ms, aggregate_by_time=False)
            or []
        )
    except Exception as e:
        print(f"# WARN: hl_history fills fetch failed: {e}", file=_sys.stderr)
        return set()
    pos: dict[str, float] = defaultdict(float)
    for f in fills:
        sym = _norm(f.get("coin", ""))
        if not sym:
            continue
        try:
            sz = float(f.get("sz", 0) or 0)
        except (TypeError, ValueError):
            continue
        side = (f.get("side") or "").upper()
        if side == "B":
            pos[sym] += sz
        elif side == "A":
            pos[sym] -= sz
    eps = 1e-6
    return {s for s, q in pos.items() if abs(q) > eps}


def _load_held_at_start_from_api(start_ms: int) -> set[str]:
    """Query HL user_state now and return symbols with non-zero position.
    Only valid for forward/paper-soak windows ending within ~6h of now."""
    addr = os.environ.get("HL_WALLET_ADDRESS")
    if not addr:
        env_path = ROOT / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("HL_WALLET_ADDRESS="):
                    addr = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not addr:
        return set()
    try:
        from hyperliquid.info import Info
        from hyperliquid.utils import constants
    except ImportError:
        return set()
    held: set[str] = set()
    for dex in (None, "xyz", "vntl", "hyna", "flx", "km", "cash", "para"):
        kwargs = dict(skip_ws=True)
        if dex:
            kwargs["perp_dexs"] = [dex]
        try:
            info = Info(constants.MAINNET_API_URL, **kwargs)
            us = info.user_state(addr)
        except Exception:
            continue
        for ap in us.get("assetPositions") or []:
            p = ap.get("position") or {}
            try:
                if abs(float(p.get("szi", 0) or 0)) > 1e-9:
                    held.add(_norm(p.get("coin", "")))
            except (TypeError, ValueError):
                continue
    return {h for h in held if h}


def _load_held_at_start(start_ms: int, end_ms: int) -> set[str]:
    """Return held-symbol set per REPLAY_HELD_SOURCE. Guard against
    using current_user_state for stale historical windows."""
    src = REPLAY_HELD_SOURCE
    if src == "current_user_state":
        now_ms = int(datetime.now().timestamp() * 1000)
        if end_ms < now_ms - 6 * 3600_000:
            print(
                f"# WARN: REPLAY_HELD_SOURCE=current_user_state but window ends "
                f"{(now_ms - end_ms) / 3600_000:.1f}h ago; held set may be wrong",
                file=_sys.stderr,
            )
        return _load_held_at_start_from_api(start_ms)
    if src == "hl_history":
        return _load_held_at_start_from_hl_history(start_ms)
    if src == "engine_position_log":
        return _load_held_at_start_from_engine_log(start_ms)
    if src == "user_state_snapshot_at_start":
        print(
            "# NOTE: user_state_snapshot_at_start not implemented; using "
            "reconstruct_from_fills instead",
            file=_sys.stderr,
        )
    # default: reconstruct_from_fills (local hl_fill_received log).
    held = _load_held_at_start_from_fills(start_ms)
    if not held:
        # Local log fills empty before start_ms; try engine position log
        # (hl_position_reconciled), which has per-minute snapshots even
        # when fills are sparse.
        held = _load_held_at_start_from_engine_log(start_ms)
        if held:
            print(
                f"# NOTE: held reconstructed from engine_position_log: "
                f"{len(held)} syms",
                file=_sys.stderr,
            )
        else:
            # Last resort: HL API history.
            held = _load_held_at_start_from_hl_history(start_ms)
            if held:
                print(
                    f"# NOTE: held reconstructed from HL API history: {len(held)} syms",
                    file=_sys.stderr,
                )
    return held


def _resolve_universe(from_ms: int | None, to_ms: int | None) -> set[str] | None:
    """Return the set of allowed symbols, or None if no filter."""
    mode = REPLAY_UNIVERSE_MODE
    if mode == "all":
        return None
    if mode == "live_fills_window":
        if from_ms is None or to_ms is None:
            print(
                "# WARN: live_fills_window needs REPLAY_FROM_MS/REPLAY_TO_MS; falling back to all"
            )
            return None
        return _load_universe_live_fills(from_ms, to_ms, REPLAY_MIN_LIVE_FILLS)
    if mode == "entry_signal_window":
        if from_ms is None or to_ms is None:
            print(
                "# WARN: entry_signal_window needs REPLAY_FROM_MS/REPLAY_TO_MS; falling back to all"
            )
            return None
        return _load_universe_entry_signals(from_ms, to_ms)
    if mode == "configured_live":
        u = _load_universe_configured_live()
        if not u:
            print(
                "# WARN: configured_live mode but HL_UNIVERSE/HIP3_UNIVERSE empty; falling back to all"
            )
            return None
        return u
    if mode == "configured_or_held":
        u = _load_universe_configured_live()
        if from_ms is None:
            print(
                "# WARN: configured_or_held needs REPLAY_FROM_MS; using configured_live alone"
            )
            return u or None
        held = _load_held_at_start(from_ms, to_ms or from_ms)
        if not u and not held:
            print("# WARN: both configured + held empty; falling back to all")
            return None
        combined = (u or set()) | held
        print(
            f"# configured_or_held breakdown: configured={len(u or [])} "
            f"held_at_start={len(held)} union={len(combined)}",
            file=_sys.stderr,
        )
        return combined
    print(f"# WARN: unknown REPLAY_UNIVERSE={mode}; falling back to all")
    return None


# ── Main ──────────────────────────────────────────────────────────────────
def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    bars = load_bars()
    ticks = load_ticks()
    allowed = None
    if SYMBOL_FILTER != "all":
        allowed = {_norm(s) for s in SYMBOL_FILTER.split(",")}

    # Universe filter (Phase 1 residual decomposition)
    _from = os.environ.get("REPLAY_FROM_MS")
    _to = os.environ.get("REPLAY_TO_MS")
    _from_ms = int(_from) if _from else None
    _to_ms = int(_to) if _to else None
    universe = _resolve_universe(_from_ms, _to_ms)
    if universe is not None:
        print(
            f"# REPLAY_UNIVERSE={REPLAY_UNIVERSE_MODE}  "
            f"allowed_symbols={len(universe)}",
            file=_sys.stderr,
        )
        if allowed is None:
            allowed = universe
        else:
            allowed = allowed & universe

    # G0 prep: build regime-pause trip index if the gate is enabled.
    global _regime_trips
    if REGIME_GATE:
        _regime_trips = build_regime_trips(bars)
        print(
            f"# G0 regime_pause: ENABLED  "
            f"thresh={REGIME_1H_ABS_RETURN}  pause={REGIME_PAUSE_SECONDS}s  "
            f"trips={len(_regime_trips)}"
        )
    else:
        _regime_trips = []

    sink = AttributionSink(ATTRIBUTION_FILE)

    # Diagnostic: opens emission for entry-alignment analysis.
    global _OPENS_OUT_FH
    _opens_out_path = os.environ.get("REPLAY_OPENS_OUT")
    if _opens_out_path:
        _OPENS_OUT_FH = open(_opens_out_path, "w")

    # Diagnostic: per-trade emission for cardinality / holding analysis.
    global _TRADES_OUT_FH
    _trades_out_path = os.environ.get("REPLAY_TRADES_OUT")
    if _trades_out_path:
        _TRADES_OUT_FH = open(_trades_out_path, "w")

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
    if _OPENS_OUT_FH is not None:
        _OPENS_OUT_FH.close()
    if _TRADES_OUT_FH is not None:
        _TRADES_OUT_FH.close()

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
            baseline_count = int(
                json.loads(BASELINE_COUNT_FILE.read_text()).get("total_trades", 0)
            )
        except Exception:
            baseline_count = total_trades
    else:
        baseline_count = (
            total_trades  # first run establishes own floor at 100% of itself
        )

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
    for gate in (
        "regime_pause",
        "obi_gate",
        "trend_regime",
        "flip_guard",
        "momentum_dedup",
        "reentry_cooldown",
        "max_opens_day",
    ):
        cnt = sink.gate_counts.get(gate, 0)
        saved = sink.gate_saved.get(gate, 0.0)
        avg = (saved / cnt) if cnt > 0 else 0.0
        print(
            f"  {gate:<16s} rejected {cnt:>7d}  saved ${saved:+9.2f}  avg_per_reject ${avg:+.3f}"
        )
    # Structured emission for tooling — single line, parseable.
    print(
        "REPLAY_GATE_COUNTS_JSON "
        + json.dumps(
            {"event": "replay_gate_counts", "gate_counts": dict(sink.gate_counts)},
            separators=(",", ":"),
        )
    )
    print(
        f"  {'FIRED':<16s} entries  {total_trades:>7d}  SCORE  ${total:+9.2f}  "
        f"$/trade ${secondary:+.3f}"
    )

    # top-8 table
    top = sorted(per_sym.items(), key=lambda kv: -abs(kv[1]))[:8]
    print("\ntop-8 |pnl|:")
    for s, pnl in top:
        base_pnl = float(baseline.get(s, 0.0)) if baseline else 0.0
        d = f"Δ={pnl - base_pnl:+6.2f}" if baseline else ""
        reasons = per_sym_exit_reasons.get(s, {})
        rstr = ",".join(f"{k}={v}" for k, v in sorted(reasons.items())) or "-"
        print(
            f"  {s:18s} trades={per_sym_trades[s]:4d} sim={pnl:+8.2f} {d}  exits[{rstr}]"
        )


if __name__ == "__main__":
    main()
