#!/usr/bin/env python3
"""Alert-only entry-trigger watcher for ZEC discretionary management.

Two modes, picked by --mode. Both alert-only. Neither places, cancels,
or modifies any order. Sibling to scripts/monitor_zec_add_stop.py.

──────────────────────────────────────────────────────────────────────────
Mode: continuation_420 (DEFAULT — active thesis)
──────────────────────────────────────────────────────────────────────────
Active when ZEC is trading the 417 support / 425 breakout corridor after
the 412+ acceptance push. Replaces the stale 409 retest setup as the live
trigger surface.

  BREAKOUT_ACCEPTANCE_READY — most-recent closed 5m bar closed ≥ 425 AND
                              current intra-bar mid ≥ 423 AND OBI ≥ +0.05
                              → continuation add review
  CHEAP_PULLBACK_READY      — mid in 415-417 held ≥ 5 min (not just a
                              wick), OBI stabilizing / improving
                              → better-priced add review
  TRIM_REJECTION_READY      — recent 5m bar high ≥ 425 followed by a
                              5m close < 422, OBI deteriorating
                              → tactical trim review on existing 18 ZEC
  FAILED_BREAKOUT_WARNING   — most-recent closed 5m bar closed < 417
                              → continuation structure weakening
  NO_TRADE_DRIFT            — no other trigger for ≥4h

──────────────────────────────────────────────────────────────────────────
Mode: retest_409 (PARKED — original tranche-1 setup)
──────────────────────────────────────────────────────────────────────────
Preserved verbatim for the case where ZEC drops back to 409 territory.
Switch back with --mode retest_409 if that happens.

  RETEST_READY        — mid in 409.20-409.80 held ≥2min, OBI not strongly
                        negative, no fast flush below 408.80
  FLUSH_RECLAIM_READY — wash below 406.50 then reclaim above 408.00
                        within 15 min, OBI improves from flush low
  ACCEPTANCE_READY    — mid ≥ 413 for ≥15min, pullbacks held above 412,
                        OBI nonnegative or improving
  NO_TRADE_DRIFT      — no other trigger for ≥4h

──────────────────────────────────────────────────────────────────────────
Per-trigger 30-min cooldown prevents spam while conditions remain true.
On script restart, in-flight context is lost — deliberate simplicity.

Usage:
  Default (continuation_420), watch loop:
    venv/bin/python3 scripts/monitor_zec_entry_triggers.py --watch 30

  Switch back to old retest mode:
    venv/bin/python3 scripts/monitor_zec_entry_triggers.py --mode retest_409 --watch 30

  One-shot snapshot:
    venv/bin/python3 scripts/monitor_zec_entry_triggers.py

Alert log: logs/zec_entry_alerts.jsonl
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Optional


INFO_URL = "https://api.hyperliquid.xyz/info"
COIN = "ZEC"

# ── Mode: retest_409 thresholds (preserved) ────────────────────────────────
RETEST_ZONE_LO = 409.20
RETEST_ZONE_HI = 409.80
RETEST_HOLD_S = 120.0
RETEST_OBI_FLOOR = -0.30
RETEST_NO_FLUSH_BELOW = 408.80

FLUSH_BELOW = 406.50
FLUSH_RECLAIM_ABOVE = 408.00
FLUSH_RECLAIM_WINDOW_S = 900.0
FLUSH_OBI_IMPROVE_DELTA = 0.20

ACCEPTANCE_LEVEL = 413.00
ACCEPTANCE_HOLD_S = 900.0
ACCEPTANCE_NO_BREAK_BELOW = 412.00
ACCEPTANCE_OBI_FLOOR = -0.05

# ── Mode: continuation_420 thresholds ──────────────────────────────────────
BREAKOUT_CLOSE_LEVEL = 425.00         # most-recent CLOSED 5m bar must close >= this
BREAKOUT_FOLLOWTHROUGH_FLOOR = 423.00  # current intra-bar mid must hold >= this
BREAKOUT_OBI_FLOOR = 0.05             # OBI must be meaningfully positive

CHEAP_PULLBACK_LO = 415.00
CHEAP_PULLBACK_HI = 417.00
CHEAP_PULLBACK_HOLD_S = 300.0         # 5 min in zone — rejects wicks
CHEAP_PULLBACK_OBI_FLOOR = -0.20      # stabilizing / not deeply ask-heavy

TRIM_TEST_HIGH = 425.00               # any recent 5m bar high >= this
TRIM_REJECT_CLOSE_BELOW = 422.00      # then a 5m close < this fires the trim
TRIM_LOOKBACK_BARS = 6                # search the last N closed 5m bars
TRIM_OBI_CEILING = 0.05               # if OBI > this at trim moment, suppress (rejection isn't decisive)

FAILED_BREAKOUT_CLOSE_BELOW = 417.00  # most-recent closed 5m bar close < this

# ── Cross-mode ─────────────────────────────────────────────────────────────
DRIFT_QUIET_S = 4 * 3600.0
DRIFT_COOLDOWN_S = 4 * 3600.0
PER_TRIGGER_COOLDOWN_S = 1800.0  # 30 min

ROOT = Path(__file__).resolve().parent.parent
ALERT_LOG = ROOT / "logs" / "zec_entry_alerts.jsonl"


# ── HTTP helpers ────────────────────────────────────────────────────────────

def _post(body: dict):
    req = urllib.request.Request(
        INFO_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _l2_mid_obi() -> tuple[Optional[float], Optional[float]]:
    """Return (mid, OBI_top10). None on any error."""
    try:
        book = _post({"type": "l2Book", "coin": COIN})
        levels = book.get("levels", [[], []])
        bids, asks = levels[0][:10], levels[1][:10]
        if not bids or not asks:
            return None, None
        bb = float(bids[0]["px"])
        ba = float(asks[0]["px"])
        bsz = sum(float(lvl["sz"]) for lvl in bids)
        asz = sum(float(lvl["sz"]) for lvl in asks)
        obi = (bsz - asz) / (bsz + asz) if (bsz + asz) else 0.0
        return (bb + ba) / 2.0, obi
    except Exception:
        return None, None


def _recent_5m_candles(n_bars: int = 12) -> Optional[list[dict]]:
    """Return last ~n_bars of 5m candles, oldest first. None on error.

    Each candle: {t, o, h, l, c, v} — t is bar-open ms, c is close (str).
    The final element may be the in-progress bar (not yet closed).
    """
    try:
        now_ms = int(time.time() * 1000)
        start = now_ms - (n_bars + 2) * 5 * 60 * 1000  # small overshoot
        cdl = _post({
            "type": "candleSnapshot",
            "req": {"coin": COIN, "interval": "5m",
                    "startTime": start, "endTime": now_ms},
        })
        if not isinstance(cdl, list) or not cdl:
            return None
        return cdl[-n_bars:]
    except Exception:
        return None


def _split_closed_inprogress(cdl: list[dict]) -> tuple[list[dict], Optional[dict]]:
    """Return (closed_bars, in_progress_or_None) using bar-close timestamps."""
    if not cdl:
        return [], None
    now_ms = int(time.time() * 1000)
    bar_ms = 5 * 60 * 1000
    closed = []
    in_prog = None
    for c in cdl:
        bar_close_ms = int(c["t"]) + bar_ms
        if bar_close_ms <= now_ms:
            closed.append(c)
        else:
            in_prog = c
    return closed, in_prog


# ── State machine ──────────────────────────────────────────────────────────

class TriggerState:
    """Holds rolling state. Reset on script restart."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.start_ts: float = time.time()

        # retest_409 state
        self.retest_in_zone_since: Optional[float] = None
        self.retest_min_mid_in_hold: Optional[float] = None
        self.flush_low_ts: Optional[float] = None
        self.flush_low_mid: Optional[float] = None
        self.flush_low_obi: Optional[float] = None
        self.flush_active: bool = False
        self.accept_above_413_since: Optional[float] = None
        self.accept_min_mid_in_hold: Optional[float] = None
        self.accept_obi_at_start: Optional[float] = None
        self.accept_obi_min: Optional[float] = None

        # continuation_420 state
        self.cheap_pullback_since: Optional[float] = None
        self.cheap_pullback_min_obi: Optional[float] = None

        # Cooldowns + drift bookkeeping
        self.last_fire: dict[str, float] = {}
        self.last_real_trigger_ts: float = self.start_ts

    def update(self, mid: float, obi: float, now: float,
               candles: Optional[list[dict]]) -> list[dict]:
        if self.mode == "continuation_420":
            fires = self._update_continuation_420(mid, obi, now, candles)
        else:
            fires = self._update_retest_409(mid, obi, now)

        # Drift bookkeeping is mode-shared
        for f in fires:
            if f["trigger"] != "NO_TRADE_DRIFT":
                self.last_real_trigger_ts = now

        if (
            now - self.last_real_trigger_ts >= DRIFT_QUIET_S
            and self._can_fire("NO_TRADE_DRIFT", now)
        ):
            fires.append({
                "trigger": "NO_TRADE_DRIFT",
                "message": (
                    f"NO_TRADE_DRIFT: No active-mode ({self.mode}) trigger for "
                    f"≥{int(DRIFT_QUIET_S/3600)}h; stand down and re-evaluate later."
                ),
                "mid": mid, "obi": obi,
                "quiet_for_s": round(now - self.last_real_trigger_ts, 1),
            })
            self._mark_fire("NO_TRADE_DRIFT", now)
            self.last_real_trigger_ts = now

        return fires

    # ── continuation_420 ───────────────────────────────────────────────────

    def _update_continuation_420(self, mid: float, obi: float, now: float,
                                 candles: Optional[list[dict]]) -> list[dict]:
        fires: list[dict] = []
        if candles is None:
            return fires
        closed, in_prog = _split_closed_inprogress(candles)
        if not closed:
            return fires

        last_closed = closed[-1]
        last_close_px = float(last_closed["c"])

        # BREAKOUT_ACCEPTANCE_READY ──────────────────────────────────────
        # Last closed 5m bar closed >= 425 AND current intra-bar mid still
        # holding >= 423 AND OBI meaningfully positive.
        if (
            last_close_px >= BREAKOUT_CLOSE_LEVEL
            and mid >= BREAKOUT_FOLLOWTHROUGH_FLOOR
            and obi >= BREAKOUT_OBI_FLOOR
            and self._can_fire("BREAKOUT_ACCEPTANCE_READY", now)
        ):
            fires.append({
                "trigger": "BREAKOUT_ACCEPTANCE_READY",
                "message": (
                    f"BREAKOUT_ACCEPTANCE_READY: last 5m closed {last_close_px:.2f} "
                    f"≥{BREAKOUT_CLOSE_LEVEL}, current mid {mid:.2f} ≥"
                    f"{BREAKOUT_FOLLOWTHROUGH_FLOOR}, OBI {obi:+.3f} — review "
                    f"continuation add."
                ),
                "mid": mid, "obi": obi,
                "last_5m_close": last_close_px,
                "in_progress_low": float(in_prog["l"]) if in_prog else None,
            })
            self._mark_fire("BREAKOUT_ACCEPTANCE_READY", now)

        # CHEAP_PULLBACK_READY ───────────────────────────────────────────
        # Mid in 415-417 held ≥ 5 min, OBI not deeply ask-heavy.
        in_zone = CHEAP_PULLBACK_LO <= mid <= CHEAP_PULLBACK_HI
        if in_zone:
            if self.cheap_pullback_since is None:
                self.cheap_pullback_since = now
                self.cheap_pullback_min_obi = obi
            else:
                self.cheap_pullback_min_obi = min(
                    self.cheap_pullback_min_obi or obi, obi
                )
            held = now - self.cheap_pullback_since
            if (
                held >= CHEAP_PULLBACK_HOLD_S
                and obi >= CHEAP_PULLBACK_OBI_FLOOR
                and self._can_fire("CHEAP_PULLBACK_READY", now)
            ):
                fires.append({
                    "trigger": "CHEAP_PULLBACK_READY",
                    "message": (
                        f"CHEAP_PULLBACK_READY: ZEC in {CHEAP_PULLBACK_LO}-"
                        f"{CHEAP_PULLBACK_HI} for {held:.0f}s, OBI {obi:+.3f} "
                        f"— review better-priced add (tighter invalidation than core)."
                    ),
                    "mid": mid, "obi": obi,
                    "held_for_s": round(held, 1),
                    "min_obi_during_hold": self.cheap_pullback_min_obi,
                })
                self._mark_fire("CHEAP_PULLBACK_READY", now)
        else:
            # Reset hold if we leave the zone — even a brief exit invalidates
            # the "held for 5 min" requirement (this is the wick-rejection
            # property the user explicitly asked for).
            self.cheap_pullback_since = None
            self.cheap_pullback_min_obi = None

        # TRIM_REJECTION_READY ───────────────────────────────────────────
        # Within last TRIM_LOOKBACK_BARS: any high >= 425, then a CLOSED bar
        # that closes < 422. Suppress if current OBI is still strongly bid
        # (rejection isn't decisive yet).
        recent = closed[-TRIM_LOOKBACK_BARS:] if len(closed) >= 1 else []
        had_test = any(float(b["h"]) >= TRIM_TEST_HIGH for b in recent)
        if (
            had_test
            and last_close_px < TRIM_REJECT_CLOSE_BELOW
            and obi <= TRIM_OBI_CEILING
            and self._can_fire("TRIM_REJECTION_READY", now)
        ):
            test_bar = max(
                (b for b in recent if float(b["h"]) >= TRIM_TEST_HIGH),
                key=lambda b: float(b["h"]),
            )
            fires.append({
                "trigger": "TRIM_REJECTION_READY",
                "message": (
                    f"TRIM_REJECTION_READY: 5m bar tested {float(test_bar['h']):.2f} "
                    f"≥{TRIM_TEST_HIGH} then last 5m closed {last_close_px:.2f} "
                    f"<{TRIM_REJECT_CLOSE_BELOW}, OBI {obi:+.3f} — review "
                    f"tactical trim on 18 ZEC core."
                ),
                "mid": mid, "obi": obi,
                "test_bar_high": float(test_bar["h"]),
                "last_5m_close": last_close_px,
            })
            self._mark_fire("TRIM_REJECTION_READY", now)

        # FAILED_BREAKOUT_WARNING ────────────────────────────────────────
        if (
            last_close_px < FAILED_BREAKOUT_CLOSE_BELOW
            and self._can_fire("FAILED_BREAKOUT_WARNING", now)
        ):
            fires.append({
                "trigger": "FAILED_BREAKOUT_WARNING",
                "message": (
                    f"FAILED_BREAKOUT_WARNING: last 5m closed {last_close_px:.2f} "
                    f"<{FAILED_BREAKOUT_CLOSE_BELOW} — continuation structure "
                    f"weakening; revisit hold/trim posture, do NOT add."
                ),
                "mid": mid, "obi": obi,
                "last_5m_close": last_close_px,
            })
            self._mark_fire("FAILED_BREAKOUT_WARNING", now)

        return fires

    # ── retest_409 (preserved) ─────────────────────────────────────────────

    def _update_retest_409(self, mid: float, obi: float, now: float) -> list[dict]:
        fires: list[dict] = []

        # RETEST_READY
        in_zone = RETEST_ZONE_LO <= mid <= RETEST_ZONE_HI
        if in_zone:
            if self.retest_in_zone_since is None:
                self.retest_in_zone_since = now
                self.retest_min_mid_in_hold = mid
            else:
                self.retest_min_mid_in_hold = min(
                    self.retest_min_mid_in_hold or mid, mid
                )
            if mid < RETEST_NO_FLUSH_BELOW:
                self.retest_in_zone_since = None
                self.retest_min_mid_in_hold = None
            elif (
                obi < RETEST_OBI_FLOOR
                and self.retest_in_zone_since is not None
            ):
                pass
            else:
                held_for = now - self.retest_in_zone_since
                if (
                    held_for >= RETEST_HOLD_S
                    and obi >= RETEST_OBI_FLOOR
                    and (self.retest_min_mid_in_hold or mid) >= RETEST_NO_FLUSH_BELOW
                    and self._can_fire("RETEST_READY", now)
                ):
                    fires.append({
                        "trigger": "RETEST_READY",
                        "message": (
                            f"RETEST_READY: ZEC back in {RETEST_ZONE_LO}-"
                            f"{RETEST_ZONE_HI} support zone; reassess tranche-1 add."
                        ),
                        "mid": mid, "obi": obi,
                        "held_for_s": round(held_for, 1),
                        "min_mid_during_hold": self.retest_min_mid_in_hold,
                    })
                    self._mark_fire("RETEST_READY", now)
        else:
            self.retest_in_zone_since = None
            self.retest_min_mid_in_hold = None

        # FLUSH_RECLAIM_READY
        if mid < FLUSH_BELOW:
            self.flush_active = True
            if self.flush_low_mid is None or mid < self.flush_low_mid:
                self.flush_low_mid = mid
                self.flush_low_ts = now
                self.flush_low_obi = obi
        else:
            if (
                self.flush_low_ts is not None
                and self.flush_low_obi is not None
                and self.flush_low_mid is not None
                and mid >= FLUSH_RECLAIM_ABOVE
            ):
                age = now - self.flush_low_ts
                obi_improvement = obi - self.flush_low_obi
                if (
                    age <= FLUSH_RECLAIM_WINDOW_S
                    and obi_improvement >= FLUSH_OBI_IMPROVE_DELTA
                    and self._can_fire("FLUSH_RECLAIM_READY", now)
                ):
                    fires.append({
                        "trigger": "FLUSH_RECLAIM_READY",
                        "message": (
                            f"FLUSH_RECLAIM_READY: ZEC washed below {FLUSH_BELOW} "
                            f"(low {self.flush_low_mid:.2f}) and reclaimed "
                            f"{FLUSH_RECLAIM_ABOVE} in {age:.0f}s; reassess support add."
                        ),
                        "mid": mid, "obi": obi,
                        "flush_low_mid": self.flush_low_mid,
                        "flush_low_obi": self.flush_low_obi,
                        "obi_improvement": round(obi_improvement, 3),
                        "age_s": round(age, 1),
                    })
                    self._mark_fire("FLUSH_RECLAIM_READY", now)
                    self.flush_low_ts = None
                    self.flush_low_mid = None
                    self.flush_low_obi = None
                    self.flush_active = False
                elif age > FLUSH_RECLAIM_WINDOW_S:
                    self.flush_low_ts = None
                    self.flush_low_mid = None
                    self.flush_low_obi = None
                    self.flush_active = False
            self.flush_active = False

        # ACCEPTANCE_READY
        if mid >= ACCEPTANCE_LEVEL:
            if self.accept_above_413_since is None:
                self.accept_above_413_since = now
                self.accept_min_mid_in_hold = mid
                self.accept_obi_at_start = obi
                self.accept_obi_min = obi
            else:
                self.accept_min_mid_in_hold = min(
                    self.accept_min_mid_in_hold or mid, mid
                )
                self.accept_obi_min = min(self.accept_obi_min or obi, obi)
            held = now - self.accept_above_413_since
            obi_ok = (obi >= ACCEPTANCE_OBI_FLOOR) or (
                self.accept_obi_at_start is not None
                and obi > self.accept_obi_at_start
            )
            if (
                held >= ACCEPTANCE_HOLD_S
                and (self.accept_min_mid_in_hold or mid) >= ACCEPTANCE_NO_BREAK_BELOW
                and obi_ok
                and self._can_fire("ACCEPTANCE_READY", now)
            ):
                fires.append({
                    "trigger": "ACCEPTANCE_READY",
                    "message": (
                        f"ACCEPTANCE_READY: ZEC has held above {ACCEPTANCE_LEVEL} "
                        f"with pullbacks above {ACCEPTANCE_NO_BREAK_BELOW}; "
                        f"fresh continuation evaluation needed."
                    ),
                    "mid": mid, "obi": obi,
                    "held_for_s": round(held, 1),
                    "min_mid_during_hold": self.accept_min_mid_in_hold,
                    "obi_at_hold_start": self.accept_obi_at_start,
                    "obi_min_during_hold": self.accept_obi_min,
                })
                self._mark_fire("ACCEPTANCE_READY", now)
        else:
            if (
                self.accept_min_mid_in_hold is not None
                and mid < ACCEPTANCE_NO_BREAK_BELOW
            ):
                self.accept_above_413_since = None
                self.accept_min_mid_in_hold = None
                self.accept_obi_at_start = None
                self.accept_obi_min = None
            elif self.accept_above_413_since is not None:
                self.accept_above_413_since = None
                self.accept_min_mid_in_hold = None
                self.accept_obi_at_start = None
                self.accept_obi_min = None

        return fires

    # ── Cooldown helpers ───────────────────────────────────────────────────

    def _can_fire(self, trigger: str, now: float) -> bool:
        last = self.last_fire.get(trigger, 0.0)
        cooldown = (
            DRIFT_COOLDOWN_S if trigger == "NO_TRADE_DRIFT" else PER_TRIGGER_COOLDOWN_S
        )
        return (now - last) >= cooldown

    def _mark_fire(self, trigger: str, now: float) -> None:
        self.last_fire[trigger] = now

    # ── Snapshot for stdout ────────────────────────────────────────────────

    def snapshot_dict(self, mid: Optional[float], obi: Optional[float],
                      last_5m_close: Optional[float]) -> dict:
        d = {"mid": mid, "obi": obi, "last_5m_close": last_5m_close,
             "quiet_for_s": round(time.time() - self.last_real_trigger_ts, 1)}
        if self.mode == "continuation_420":
            d["cheap_pullback_holding_s"] = (
                round(time.time() - self.cheap_pullback_since, 1)
                if self.cheap_pullback_since else None
            )
        else:
            d["retest_holding_s"] = (
                round(time.time() - self.retest_in_zone_since, 1)
                if self.retest_in_zone_since else None
            )
            d["flush_active"] = self.flush_active
            d["flush_low_mid"] = self.flush_low_mid
            d["accept_holding_s"] = (
                round(time.time() - self.accept_above_413_since, 1)
                if self.accept_above_413_since else None
            )
        return d


# ── Output ──────────────────────────────────────────────────────────────────

BADGES = {
    # continuation_420
    "BREAKOUT_ACCEPTANCE_READY": "🟩",
    "CHEAP_PULLBACK_READY": "🟦",
    "TRIM_REJECTION_READY": "🟥",
    "FAILED_BREAKOUT_WARNING": "⚠️",
    # retest_409
    "RETEST_READY": "🟢",
    "FLUSH_RECLAIM_READY": "🟡",
    "ACCEPTANCE_READY": "🔵",
    # cross-mode
    "NO_TRADE_DRIFT": "⚪",
}


def _emit_fires(fires: list[dict], mode: str) -> None:
    if not fires:
        return
    ALERT_LOG.parent.mkdir(parents=True, exist_ok=True)
    ts_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    with ALERT_LOG.open("a", buffering=1) as fh:
        for f in fires:
            badge = BADGES.get(f["trigger"], "")
            print(f"\n[{ts_iso}] {badge} {f['message']}")
            for k, v in f.items():
                if k in ("trigger", "message"):
                    continue
                print(f"    {k}: {v}")
            fh.write(json.dumps({"ts_utc": ts_iso, "mode": mode, **f}) + "\n")


def _print_snapshot(snap: dict) -> None:
    parts = [f"mid={snap['mid']}", f"OBI={snap['obi']}"]
    if snap.get("last_5m_close") is not None:
        parts.append(f"5mClose={snap['last_5m_close']}")
    if snap.get("cheap_pullback_holding_s"):
        parts.append(f"pullback_hold={snap['cheap_pullback_holding_s']:.0f}s")
    if snap.get("retest_holding_s"):
        parts.append(f"retest_hold={snap['retest_holding_s']:.0f}s")
    if snap.get("flush_active"):
        parts.append(f"flush_active(low={snap['flush_low_mid']})")
    if snap.get("accept_holding_s"):
        parts.append(f"accept_hold={snap['accept_holding_s']:.0f}s")
    parts.append(f"quiet={snap['quiet_for_s']:.0f}s")
    print(f"[{dt.datetime.now(dt.timezone.utc).isoformat()}] " + "  ".join(parts))


def _print_header(mode: str) -> None:
    print(f"# ZEC entry-trigger watcher — mode={mode}, ALERT ONLY, NO ORDER PATH")
    if mode == "continuation_420":
        print(f"# BREAKOUT_ACCEPTANCE : last 5m close ≥{BREAKOUT_CLOSE_LEVEL} AND mid ≥{BREAKOUT_FOLLOWTHROUGH_FLOOR} AND OBI ≥{BREAKOUT_OBI_FLOOR}")
        print(f"# CHEAP_PULLBACK      : mid in [{CHEAP_PULLBACK_LO}, {CHEAP_PULLBACK_HI}] held ≥{int(CHEAP_PULLBACK_HOLD_S/60)}min, OBI ≥{CHEAP_PULLBACK_OBI_FLOOR}")
        print(f"# TRIM_REJECTION      : recent 5m high ≥{TRIM_TEST_HIGH} then 5m close <{TRIM_REJECT_CLOSE_BELOW}, OBI ≤{TRIM_OBI_CEILING}")
        print(f"# FAILED_BREAKOUT     : last 5m close <{FAILED_BREAKOUT_CLOSE_BELOW}")
    else:
        print(f"# RETEST       : mid in [{RETEST_ZONE_LO}, {RETEST_ZONE_HI}] held ≥{int(RETEST_HOLD_S)}s, OBI≥{RETEST_OBI_FLOOR}")
        print(f"# FLUSH/RECLAIM: mid<{FLUSH_BELOW} then ≥{FLUSH_RECLAIM_ABOVE} within {int(FLUSH_RECLAIM_WINDOW_S/60)}min, OBI Δ≥{FLUSH_OBI_IMPROVE_DELTA}")
        print(f"# ACCEPTANCE   : mid≥{ACCEPTANCE_LEVEL} held ≥{int(ACCEPTANCE_HOLD_S/60)}min, no break <{ACCEPTANCE_NO_BREAK_BELOW}, OBI≥{ACCEPTANCE_OBI_FLOOR}")
    print(f"# DRIFT        : no trigger for ≥{int(DRIFT_QUIET_S/3600)}h")
    print(f"# alert log    : {ALERT_LOG}")
    print()


# ── main ────────────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--mode", choices=["continuation_420", "retest_409"],
                   default="continuation_420",
                   help="trigger set to evaluate (default: continuation_420 — active thesis)")
    p.add_argument("--watch", type=int, default=0, metavar="SEC",
                   help="poll loop interval in seconds (default 0 = one-shot)")
    p.add_argument("--quiet-snapshots", action="store_true",
                   help="in watch mode, suppress per-tick snapshots — print only on fire")
    args = p.parse_args()

    _print_header(args.mode)

    state = TriggerState(mode=args.mode)
    is_oneshot = args.watch <= 0

    while True:
        now = time.time()
        mid, obi = _l2_mid_obi()
        candles = _recent_5m_candles() if args.mode == "continuation_420" else None
        last_5m_close = None
        if candles:
            closed, _ = _split_closed_inprogress(candles)
            if closed:
                last_5m_close = float(closed[-1]["c"])

        if mid is None or obi is None:
            print(f"[{dt.datetime.now(dt.timezone.utc).isoformat()}] WARN: l2 fetch failed")
        else:
            fires = state.update(mid, obi, now, candles)
            _emit_fires(fires, args.mode)
            if not args.quiet_snapshots or is_oneshot:
                _print_snapshot(state.snapshot_dict(mid, obi, last_5m_close))

        if is_oneshot:
            return 0
        try:
            time.sleep(args.watch)
        except KeyboardInterrupt:
            print("\n# stopped by user")
            return 0


if __name__ == "__main__":
    sys.exit(main())
