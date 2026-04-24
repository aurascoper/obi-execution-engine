#!/usr/bin/env python3
"""Eval harness for autoresearch on strategy/meta_controller.py discount.

Replays the historical exit_signal stream from logs/hl_engine.jsonl, simulates
POWdTSBandit running over it with the discount currently set in
strategy/meta_controller.py, and emits a SCORE that captures how well the
bandit's online arm selections track the actual reward stream.

Method (offline contextual bandit eval via rejection sampling):
    For each historical exit_signal event in time order:
        a. Bandit.select() picks an arm.
        b. If bandit's pick == event's actual arm (the one that fired in
           production), credit the bandit with that event's pnl_est and
           call bandit.update(arm, reward).
        c. If picks differ, the event is skipped — we have no counterfactual
           reward for the bandit's pick on that event.

SCORE  = sum of credited pnl_est across matched events (signed % units).
GUARD  = match_rate = matched / total. Threshold prevents collapse to a
         single arm that never matches.

Determinism: POWdTSBandit uses np.random; we pass seed=SEED. Same input
log + same discount + same seed -> same SCORE.

ENV:
    META_EVAL_SEED=0           # RNG seed for bandit.select()
    META_EVAL_LOOKBACK_DAYS=14 # only events newer than this; default 14d
    META_EVAL_LOG_PATH=...     # override log path
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from strategy.meta_controller import POWdTSBandit  # noqa: E402
from strategy.signals import MOMENTUM_TAG  # noqa: E402

STRATEGY_TAG = "hl_z"
ARMS = [STRATEGY_TAG, MOMENTUM_TAG]

SEED = int(os.environ.get("META_EVAL_SEED", "0"))
LOOKBACK_DAYS = int(os.environ.get("META_EVAL_LOOKBACK_DAYS", "14"))
LOG_PATH = Path(
    os.environ.get("META_EVAL_LOG_PATH", str(REPO_ROOT / "logs" / "hl_engine.jsonl"))
)
FEE_ROUND_TRIP_PCT = float(os.environ.get("FEE_ROUND_TRIP_BPS", "5.0")) / 100.0
MATCH_RATE_FLOOR = float(os.environ.get("META_EVAL_MATCH_FLOOR", "0.20"))


def _iso_to_dt(s: str) -> _dt.datetime | None:
    try:
        ts = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_dt.timezone.utc)
    return ts


def _load_events(path: Path, since: _dt.datetime) -> list[tuple[_dt.datetime, str, float]]:
    """Return list of (ts, arm, pnl_pct) for exit_signal events newer than `since`."""
    out: list[tuple[_dt.datetime, str, float]] = []
    if not path.exists():
        return out
    with path.open() as fh:
        for line in fh:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("event") != "exit_signal":
                continue
            tag = e.get("tag")
            if tag not in ARMS:
                continue
            ts = _iso_to_dt(e.get("timestamp", ""))
            if ts is None or ts < since:
                continue
            pnl = e.get("pnl_est")
            if not isinstance(pnl, (int, float)):
                continue
            out.append((ts, tag, float(pnl)))
    out.sort(key=lambda x: x[0])
    return out


def main() -> int:
    since = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=LOOKBACK_DAYS)
    events = _load_events(LOG_PATH, since)
    if not events:
        print("SCORE: -inf")
        print("MATCH_RATE: 0.0")
        print("# no events in lookback window")
        return 1

    bandit = POWdTSBandit(arms=ARMS, seed=SEED)

    matched = 0
    cum_pnl_pct = 0.0
    by_arm_credited: dict[str, list[float]] = {a: [] for a in ARMS}
    pick_counts: dict[str, int] = {a: 0 for a in ARMS}

    for _ts, actual_arm, pnl_pct in events:
        pick = bandit.select()
        pick_counts[pick] += 1
        if pick == actual_arm:
            matched += 1
            cum_pnl_pct += pnl_pct
            by_arm_credited[actual_arm].append(pnl_pct)
            reward_signal = 1.0 if pnl_pct > FEE_ROUND_TRIP_PCT else -1.0
            bandit.update(actual_arm, reward_signal)

    total = len(events)
    match_rate = matched / total

    snap = bandit.snapshot()
    print(f"# total_events={total} matched={matched} fee_threshold_pct={FEE_ROUND_TRIP_PCT:.4f}")
    print(f"# pick_counts={pick_counts}")
    for arm in ARMS:
        creds = by_arm_credited[arm]
        n = len(creds)
        avg = (sum(creds) / n) if n else 0.0
        s = snap.get(arm, {})
        print(
            f"# {arm:10s} credited={n:4d} avg_pnl_pct={avg:+.4f} "
            f"alpha={s.get('alpha',0):.3f} beta={s.get('beta',0):.3f} "
            f"posterior_mean={s.get('mean',0):.4f}"
        )

    if match_rate < MATCH_RATE_FLOOR:
        print(f"MATCH_RATE: {match_rate:.4f}")
        print(f"SCORE: -inf  # match_rate {match_rate:.3f} < floor {MATCH_RATE_FLOOR}")
        return 0

    print(f"MATCH_RATE: {match_rate:.4f}")
    print(f"SCORE: {cum_pnl_pct:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
