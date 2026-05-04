#!/usr/bin/env python3
"""Alert-only stop monitor for the ZEC tranche-1 discretionary add.

Watches ZEC perp for the add-only invalidation thresholds defined in
place_zec_add_409.py:

  STOP_TRIGGER     — most recent CLOSED 15m bar close < 407.50
  TACTICAL_WARNING — current mid < 406.50

Pure observation. NEVER places, cancels, or modifies any order. Style
matches scripts/breakout_watcher.py — stdout + a sidecar JSONL file.

This is the alert-only companion to place_zec_add_409.py. It does not
attach a server-side stop (Alo orders cannot carry one); the operator
must manually exit the filled tranche when STOP_TRIGGER fires.

Usage:
  One-shot (default):
    venv/bin/python3 scripts/monitor_zec_add_stop.py

  Watch loop (poll every N seconds):
    venv/bin/python3 scripts/monitor_zec_add_stop.py --watch 60

Alert log: logs/zec_add_stop_alerts.jsonl
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
STOP_TRIGGER_15M_CLOSE = 407.50
TACTICAL_WARNING_MID = 406.50

ROOT = Path(__file__).resolve().parent.parent
ALERT_LOG = ROOT / "logs" / "zec_add_stop_alerts.jsonl"


def _post(body: dict) -> dict | list:
    req = urllib.request.Request(
        INFO_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _l2_mid_obi() -> tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    """Return (best_bid, best_ask, mid, OBI_top10). None on any error."""
    try:
        book = _post({"type": "l2Book", "coin": COIN})
        levels = book.get("levels", [[], []])
        bids, asks = levels[0][:10], levels[1][:10]
        if not bids or not asks:
            return None, None, None, None
        bb = float(bids[0]["px"])
        ba = float(asks[0]["px"])
        bsz = sum(float(l["sz"]) for l in bids)
        asz = sum(float(l["sz"]) for l in asks)
        obi = (bsz - asz) / (bsz + asz) if (bsz + asz) else 0.0
        return bb, ba, (bb + ba) / 2.0, obi
    except Exception:
        return None, None, None, None


def _last_closed_15m() -> Optional[dict]:
    """Return the most recent CLOSED 15m bar (excludes the in-progress one).

    HL candleSnapshot returns bars with t = bar open time. The currently
    open bar is the one whose t is within [now - 15m, now]. The last
    closed bar is the one before that.
    """
    try:
        now_ms = int(time.time() * 1000)
        h_ms = 60 * 60 * 1000
        bars = _post({
            "type": "candleSnapshot",
            "req": {
                "coin": COIN,
                "interval": "15m",
                "startTime": now_ms - 4 * h_ms,
                "endTime": now_ms,
            },
        })
        if not isinstance(bars, list) or len(bars) < 2:
            return None
        cutoff = now_ms - 15 * 60 * 1000
        closed = [b for b in bars if int(b.get("t", 0)) <= cutoff]
        return closed[-1] if closed else None
    except Exception:
        return None


def _classify(
    mid: Optional[float], last_15m_close: Optional[float]
) -> tuple[str, list[str]]:
    """Return (state, reasons). State is OK / WARNING / STOP."""
    reasons: list[str] = []
    state = "OK"

    if last_15m_close is not None and last_15m_close < STOP_TRIGGER_15M_CLOSE:
        state = "STOP"
        reasons.append(
            f"15m closed {last_15m_close:.2f} < {STOP_TRIGGER_15M_CLOSE} (full stop trigger)"
        )

    if mid is not None and mid < TACTICAL_WARNING_MID:
        if state != "STOP":
            state = "WARNING"
        reasons.append(
            f"mid {mid:.2f} < {TACTICAL_WARNING_MID} (tactical alert)"
        )

    if state == "OK":
        if last_15m_close is not None:
            margin = last_15m_close - STOP_TRIGGER_15M_CLOSE
            reasons.append(
                f"last 15m close {last_15m_close:.2f}, "
                f"{margin:+.2f} above stop {STOP_TRIGGER_15M_CLOSE}"
            )
        if mid is not None:
            margin = mid - TACTICAL_WARNING_MID
            reasons.append(
                f"mid {mid:.2f}, {margin:+.2f} above tactical alert {TACTICAL_WARNING_MID}"
            )

    return state, reasons


def _emit(
    state: str, reasons: list[str], mid: Optional[float],
    obi: Optional[float], last_15m_close: Optional[float],
    write_alert: bool,
) -> None:
    ts_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    color = {"OK": "", "WARNING": "⚠ ", "STOP": "🛑 "}.get(state, "")
    print(f"[{ts_iso}] {color}{state}  mid={mid}  OBI={obi}  last_15m_close={last_15m_close}")
    for r in reasons:
        print(f"   - {r}")

    if write_alert and state in ("WARNING", "STOP"):
        ALERT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with ALERT_LOG.open("a", buffering=1) as fh:
            fh.write(json.dumps({
                "ts_utc": ts_iso,
                "state": state,
                "mid": mid,
                "obi_top10": obi,
                "last_15m_close": last_15m_close,
                "stop_trigger_threshold": STOP_TRIGGER_15M_CLOSE,
                "tactical_warning_threshold": TACTICAL_WARNING_MID,
                "reasons": reasons,
                "context": (
                    "ZEC tranche-1 discretionary add stop monitor; "
                    "alert-only — no auto-cancel, no auto-close"
                ),
            }) + "\n")


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--watch", type=int, default=0, metavar="SEC",
                   help="poll loop interval in seconds (default 0 = one-shot)")
    args = p.parse_args()

    print(f"# ZEC tranche-1 stop monitor — ALERT ONLY, NO ORDER PATH")
    print(f"# stop_trigger    : 15m close < {STOP_TRIGGER_15M_CLOSE}")
    print(f"# tactical_warning: mid < {TACTICAL_WARNING_MID}")
    print(f"# alert log       : {ALERT_LOG}")
    print()

    prev_state: Optional[str] = None
    while True:
        bb, ba, mid, obi = _l2_mid_obi()
        last_bar = _last_closed_15m()
        last_15m_close = (
            float(last_bar["c"]) if last_bar and "c" in last_bar else None
        )
        state, reasons = _classify(mid, last_15m_close)

        # In watch mode, only emit on state change OR if alert-level state.
        # In one-shot mode, always emit.
        is_oneshot = args.watch <= 0
        state_changed = state != prev_state
        if is_oneshot or state_changed or state in ("WARNING", "STOP"):
            _emit(state, reasons, mid, obi, last_15m_close, write_alert=True)
        prev_state = state

        if is_oneshot:
            return 1 if state == "STOP" else 0
        try:
            time.sleep(args.watch)
        except KeyboardInterrupt:
            print("\n# stopped by user")
            return 0


if __name__ == "__main__":
    sys.exit(main())
