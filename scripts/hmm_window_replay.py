#!/usr/bin/env python3
"""Offline replay harness for the latent regime detector's HMM channel.

Builds a windowed-predict_proba version of `_HmmEntropyChannel` (NOT the
production class) and walks it forward over historical 15-minute bars from
data/cache/bars.sqlite. Generates synthetic regime triggers, then scores
them by the same lift metric we used in the empirical pass:

    SCORE = mean( median post-trigger max|return_z| - median random
                  untriggered max|return_z| ) across BTC/ETH/ADA

Higher = larger dislocation amplification = the channel is firing on
moments that meaningfully precede turbulence.

Tunable env vars (all optional):
    HMM_WINDOW_TICKS    int, predict_proba context length. 1 = current prod
                        behavior (single tick → startprob-anchored). Sweep
                        target ∈ [1, 600]. Default 1.
    HMM_AVERAGE_TAPS    int, tail length to average posterior over before
                        entropy. 1 = just last step. Default 1.
    HMM_REFIT_EVERY     int, refit cadence in bars. Default 96 (1 day at 15m).
    HMM_THRESHOLD_PCT   float, rising-edge percentile gate (mirrors live
                        detector's `threshold_percentile`). Default 85.0.
    HMM_LOOKAHEAD_BARS  int, window for post-trigger turbulence measurement.
                        Default 4 (1h at 15m).
    HMM_COOLDOWN_BARS   int, edge-cooldown in bars. Default 20 (5h at 15m).
    HMM_REPLAY_DAYS     int, how many days of bar history to use. Default 30.
    HMM_REPLAY_SEED     int, RNG seed for HMM fit + baseline sampling.
                        Default 0.

Limitation: this runs on 15-minute bars; the live channel runs on
orderbook ticks (~20 Hz), an ~18,000× higher rate. Tunable optima here
are *directional* for live deployment — sign and order-of-magnitude
should transfer, exact values may not. Combine with live shadow data
before committing a production tunable.

Output:
    SCORE: <float>           # mean per-symbol lift
    GUARD_FIRE_COUNT: <int>  # total triggers across symbols
    GUARD_BASELINE_N: <int>  # baseline windows sampled
"""
from __future__ import annotations

import os
import random
import sqlite3
import statistics
from collections import deque
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
BARS_DB = REPO_ROOT / "data" / "cache" / "bars.sqlite"

WINDOW_TICKS = int(os.environ.get("HMM_WINDOW_TICKS", "1"))
AVERAGE_TAPS = int(os.environ.get("HMM_AVERAGE_TAPS", "1"))
REFIT_EVERY = int(os.environ.get("HMM_REFIT_EVERY", "96"))
THRESHOLD_PCT = float(os.environ.get("HMM_THRESHOLD_PCT", "85.0"))
LOOKAHEAD_BARS = int(os.environ.get("HMM_LOOKAHEAD_BARS", "4"))
COOLDOWN_BARS = int(os.environ.get("HMM_COOLDOWN_BARS", "20"))
REPLAY_DAYS = int(os.environ.get("HMM_REPLAY_DAYS", "30"))
SEED = int(os.environ.get("HMM_REPLAY_SEED", "0"))
SYMBOLS = ("BTC", "ETH", "ADA")  # SOL not in bars cache
INTERVAL = "15m"

try:
    from hmmlearn.hmm import GaussianHMM
    _HMM_OK = True
except Exception:
    _HMM_OK = False


def _load_returns(symbol: str, days: int) -> np.ndarray:
    """Return chronological log-returns from `symbol`'s 15m bars over the
    most recent `days`."""
    con = sqlite3.connect(BARS_DB)
    try:
        rows = list(con.execute(
            "SELECT t_close_ms, c FROM bars "
            "WHERE symbol=? AND interval=? ORDER BY t_close_ms DESC LIMIT ?",
            (symbol, INTERVAL, days * 96),
        ))
    finally:
        con.close()
    rows.reverse()
    if len(rows) < 2:
        return np.zeros(0)
    closes = np.array([r[1] for r in rows], dtype=np.float64)
    rets = np.diff(np.log(closes))
    return rets


def _windowed_entropy(model, history: deque[float], window_ticks: int,
                      average_taps: int) -> float | None:
    """Forward-backward over the last `window_ticks` of `history`; entropy
    of the last `average_taps`-mean of the posterior."""
    n = min(window_ticks, len(history))
    if n < 1:
        return None
    arr = np.fromiter(
        (history[i] for i in range(len(history) - n, len(history))),
        dtype=np.float64, count=n,
    ).reshape(-1, 1)
    try:
        post = model.predict_proba(arr)
    except Exception:
        return None
    taps = max(1, min(average_taps, post.shape[0]))
    p = post[-taps:].mean(axis=0).clip(1e-9, 1.0)
    return float(-(p * np.log(p)).sum())


class _ReplayChannel:
    """Mirrors latent_regime_detector._HmmEntropyChannel state machine:
    rolling z of the entropy series, percentile threshold, rising-edge
    detection with cooldown."""

    def __init__(self, z_window: int = 600,
                 threshold_pct: float = THRESHOLD_PCT,
                 cooldown_bars: int = COOLDOWN_BARS):
        self.entropy_buf: deque[float] = deque(maxlen=z_window)
        self.max_history: deque[float] = deque(maxlen=z_window)
        self.threshold_pct = threshold_pct
        self.cooldown_bars = cooldown_bars
        self.edge_armed = True
        self.last_fire_idx: int | None = None
        self.fires: list[tuple[int, float, float]] = []  # (idx, z, threshold)

    def push(self, idx: int, entropy: float | None) -> None:
        if entropy is None:
            return
        self.entropy_buf.append(entropy)
        if len(self.entropy_buf) < 60:
            return
        mu = statistics.fmean(self.entropy_buf)
        sd = statistics.pstdev(self.entropy_buf) or 1.0
        z = (entropy - mu) / sd
        self.max_history.append(z)
        if len(self.max_history) < 120:
            return
        thr = float(np.percentile(self.max_history, self.threshold_pct))
        if z < thr:
            self.edge_armed = True
            return
        if not self.edge_armed:
            return
        if (self.last_fire_idx is not None
                and idx - self.last_fire_idx < self.cooldown_bars):
            return
        self.edge_armed = False
        self.last_fire_idx = idx
        self.fires.append((idx, z, thr))


def _replay_symbol(symbol: str, rng_seed: int) -> dict:
    rets = _load_returns(symbol, REPLAY_DAYS)
    if len(rets) < 600:
        return {"symbol": symbol, "n_bars": int(len(rets)),
                "fires": [], "rets": rets, "ret_z": np.array([])}

    # Run HMM forward, refitting every REFIT_EVERY bars.
    history: deque[float] = deque(maxlen=5000)
    model = None
    last_fit_idx = -10**9
    channel = _ReplayChannel()
    min_samples = 600

    for i, r in enumerate(rets):
        history.append(float(r))
        if len(history) < min_samples:
            continue
        if model is None or (i - last_fit_idx) >= REFIT_EVERY:
            try:
                m = GaussianHMM(
                    n_components=3, covariance_type="diag",
                    n_iter=20, random_state=rng_seed,
                )
                m.fit(np.asarray(history).reshape(-1, 1))
                model = m
                last_fit_idx = i
            except Exception:
                continue
        ent = _windowed_entropy(model, history, WINDOW_TICKS, AVERAGE_TAPS)
        channel.push(i, ent)

    # Compute |z| of returns for the lift metric.
    if len(rets) >= 60:
        ret_buf: deque[float] = deque(maxlen=600)
        ret_z = np.zeros(len(rets))
        for i, r in enumerate(rets):
            ret_buf.append(float(r))
            if len(ret_buf) < 60:
                ret_z[i] = 0.0
                continue
            mu = statistics.fmean(ret_buf)
            sd = statistics.pstdev(ret_buf) or 1.0
            ret_z[i] = (r - mu) / sd
    else:
        ret_z = np.zeros(len(rets))

    return {
        "symbol": symbol,
        "n_bars": int(len(rets)),
        "fires": channel.fires,
        "ret_z": ret_z,
    }


def _lift_for(symbol_data: dict, rng: random.Random,
              n_baseline: int = 50) -> dict:
    """Compute median post-trigger max|z| and median random-baseline max|z|."""
    fires = symbol_data["fires"]
    ret_z = symbol_data["ret_z"]
    n_bars = len(ret_z)
    if not fires or n_bars < LOOKAHEAD_BARS + 2:
        return {"trig_median": None, "base_median": None,
                "lift": None, "n_trig": 0, "n_base": 0}
    fire_idxs = {f[0] for f in fires}

    # Triggered: max|z| in (idx, idx+LOOKAHEAD]
    trig_max = []
    for idx, _z, _thr in fires:
        end = min(idx + 1 + LOOKAHEAD_BARS, n_bars)
        if end <= idx + 1:
            continue
        trig_max.append(float(np.max(np.abs(ret_z[idx + 1:end]))))

    # Baseline: random idx ≥ 60, ≥ COOLDOWN_BARS away from any fire
    base_max = []
    tries = 0
    while len(base_max) < n_baseline and tries < n_baseline * 8:
        tries += 1
        i = rng.randint(60, n_bars - LOOKAHEAD_BARS - 1)
        if any(abs(i - fi) < COOLDOWN_BARS for fi in fire_idxs):
            continue
        base_max.append(float(np.max(np.abs(ret_z[i + 1:i + 1 + LOOKAHEAD_BARS]))))

    if not trig_max or not base_max:
        return {"trig_median": None, "base_median": None,
                "lift": None, "n_trig": len(trig_max), "n_base": len(base_max)}

    tm = statistics.median(trig_max)
    bm = statistics.median(base_max)
    return {
        "trig_median": tm, "base_median": bm,
        "lift": tm - bm, "n_trig": len(trig_max), "n_base": len(base_max),
    }


def main() -> int:
    if not _HMM_OK:
        print("# hmmlearn not available")
        print("SCORE: -inf")
        return 1

    rng = random.Random(SEED)
    print(
        f"# config window_ticks={WINDOW_TICKS} average_taps={AVERAGE_TAPS} "
        f"refit_every={REFIT_EVERY} thr_pct={THRESHOLD_PCT} "
        f"lookahead={LOOKAHEAD_BARS} cooldown={COOLDOWN_BARS} "
        f"replay_days={REPLAY_DAYS} seed={SEED}"
    )

    per_sym = []
    total_fires = 0
    total_baseline = 0
    lifts = []

    for sym in SYMBOLS:
        data = _replay_symbol(sym, SEED)
        result = _lift_for(data, rng)
        per_sym.append((sym, data, result))
        total_fires += len(data["fires"])
        total_baseline += result["n_base"]
        if result["lift"] is not None:
            lifts.append(result["lift"])
        print(
            f"# {sym}: bars={data['n_bars']} fires={len(data['fires'])} "
            f"trig_med={result['trig_median']} base_med={result['base_median']} "
            f"lift={result['lift']} n_trig={result['n_trig']} n_base={result['n_base']}"
        )

    if not lifts:
        print(f"GUARD_FIRE_COUNT: {total_fires}")
        print(f"GUARD_BASELINE_N: {total_baseline}")
        print("SCORE: -inf  # no symbol produced a lift")
        return 0

    score = statistics.fmean(lifts)
    print(f"GUARD_FIRE_COUNT: {total_fires}")
    print(f"GUARD_BASELINE_N: {total_baseline}")
    print(f"SCORE: {score:.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
