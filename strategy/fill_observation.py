"""strategy/fill_observation.py — Per-fill quote-stability telemetry helper.

Pure observer. Given a per-symbol micro-history of book state and a fill's
context, returns a dict suitable for log emission as a `fill_observation`
event. Not in any order or risk path.

Sibling to strategy/quoter_shadow.py — same shape (no I/O, never raises in
its caller). Markouts are NOT computed here; they're computed post-hoc by
scripts/analyze_fill_markout.py against the engine's existing mid time
series in `quoter_shadow` events.

Schema version: 1.
"""

from __future__ import annotations

import statistics
from collections import deque
from typing import Any, Optional


SCHEMA_VERSION = 1

# ── Stability bucket thresholds (v1, deliberately blunt) ────────────────────
# Tunable by editing here; no config-file plumbing in v1.
WIDENING_THRESHOLD_X_MEDIAN = 1.5
WITHDRAWAL_FRACTION_OF_INITIAL = 0.40
FLIP_COUNT_THRESHOLD = 2
FLIP_DELTA_THRESHOLD = 0.50
MEDIAN_SPREAD_LOOKBACK_SAMPLES = 20

# ── Ring sizing ─────────────────────────────────────────────────────────────
# At ~2-4 L2 ticks/sec on active HL coins, ~20 entries ≈ 5-10s window.
STABILITY_RING_MAXLEN = 20
STABILITY_WINDOW_S = 5.0


class _StabilityRing:
    """Bounded micro-history of book state per symbol.

    Each entry: (ts_s, obi, depth_top_bid_sum, depth_top_ask_sum, spread).
    All depths are top-N where N matches OBI_LEVELS in signals.py — the same
    aggregation already computed in update_orderbook, reused here for
    consistency (no top-1 anywhere).
    """

    __slots__ = ("_buf",)

    def __init__(self, maxlen: int = STABILITY_RING_MAXLEN):
        self._buf: deque[tuple[float, float, float, float, float]] = deque(
            maxlen=maxlen
        )

    def push(
        self,
        ts_s: float,
        obi: float,
        depth_top_bid_sum: float,
        depth_top_ask_sum: float,
        spread: float,
    ) -> None:
        self._buf.append((ts_s, obi, depth_top_bid_sum, depth_top_ask_sum, spread))

    def window(self, now_s: float, window_s: float = STABILITY_WINDOW_S) -> list:
        cutoff = now_s - window_s
        return [e for e in self._buf if e[0] >= cutoff]

    def __len__(self) -> int:
        return len(self._buf)


def compute_microstability(
    ring: _StabilityRing, now_s: float, window_s: float = STABILITY_WINDOW_S
) -> dict[str, Any]:
    """Derive 5s deltas + multi-hot flags from the ring.

    Returns a dict with raw deltas AND boolean flags. The flags preserve
    overlap information (one fill can trip multiple flags); a single
    bucket label is derived separately by select_bucket().

    On insufficient data (< 2 samples in window), returns all zeros and
    all flags False with `microstability_status='insufficient_window'` so
    the caller can still emit something useful.
    """
    samples = ring.window(now_s, window_s)
    if len(samples) < 2:
        return {
            "obi_flips_5s": 0,
            "obi_delta_5s": 0.0,
            "spread_delta_5s": 0.0,
            "depth_top_total_delta_5s": 0.0,
            "median_spread": None,
            "initial_total_depth": None,
            "flag_widening": False,
            "flag_withdrawal": False,
            "flag_flip": False,
            "microstability_status": "insufficient_window",
            "n_samples_in_window": len(samples),
        }

    obi_series = [s[1] for s in samples]
    bid_depth_series = [s[2] for s in samples]
    ask_depth_series = [s[3] for s in samples]
    spread_series = [s[4] for s in samples]

    flips = 0
    for i in range(1, len(obi_series)):
        prev, cur = obi_series[i - 1], obi_series[i]
        if (prev > 0 and cur < 0) or (prev < 0 and cur > 0):
            flips += 1

    obi_delta = obi_series[-1] - obi_series[0]
    spread_delta = spread_series[-1] - spread_series[0]
    bid_initial, ask_initial = bid_depth_series[0], ask_depth_series[0]
    bid_now, ask_now = bid_depth_series[-1], ask_depth_series[-1]
    initial_total = bid_initial + ask_initial
    depth_total_delta = (bid_now + ask_now) - initial_total

    median_window = spread_series[-MEDIAN_SPREAD_LOOKBACK_SAMPLES:]
    median_spread = statistics.median(median_window) if median_window else 0.0

    flag_widening = (
        median_spread > 0
        and spread_delta > WIDENING_THRESHOLD_X_MEDIAN * median_spread
    )
    flag_withdrawal = (
        initial_total > 0
        and depth_total_delta < -WITHDRAWAL_FRACTION_OF_INITIAL * initial_total
    )
    flag_flip = (flips >= FLIP_COUNT_THRESHOLD) or (
        abs(obi_delta) > FLIP_DELTA_THRESHOLD
    )

    return {
        "obi_flips_5s": flips,
        "obi_delta_5s": round(obi_delta, 6),
        "spread_delta_5s": round(spread_delta, 8),
        "depth_top_total_delta_5s": round(depth_total_delta, 4),
        "median_spread": round(median_spread, 8),
        "initial_total_depth": round(initial_total, 4),
        "flag_widening": bool(flag_widening),
        "flag_withdrawal": bool(flag_withdrawal),
        "flag_flip": bool(flag_flip),
        "microstability_status": "ok",
        "n_samples_in_window": len(samples),
    }


def select_bucket(flags: dict[str, Any]) -> str:
    """Single bucket label, priority-resolved (first match wins).

    Priority: widening > withdrawal > flip > stable. Used only for headline
    tables; raw multi-hot flags are logged independently for overlap analysis.
    """
    if flags.get("flag_widening"):
        return "unstable_widening"
    if flags.get("flag_withdrawal"):
        return "unstable_withdrawal"
    if flags.get("flag_flip"):
        return "unstable_flip"
    return "stable"


def estimate_is_maker(
    side: Optional[str],
    fill_px: Optional[float],
    best_bid: Optional[float],
    best_ask: Optional[float],
) -> Optional[bool]:
    """Coarse maker/taker estimate from fill price vs touch.

    For sells: maker if fill_px <= best_bid (filled passively at our ask).
    For buys: maker if fill_px >= best_ask (filled passively at our bid).
    Wait — corrected: a passive (maker) ASK fills when a buyer crosses to
    our ask, so a SELL maker fill happens at our resting ask price ≥
    best_ask AT the moment we placed; at fill time, our quote IS the touch
    or just inside it. The cleanest heuristic in v1:
      - SELL maker if fill_px >= best_ask  (we sat at ask, buyer lifted us)
      - BUY  maker if fill_px <= best_bid  (we sat at bid, seller hit us)
    Returns None if any input missing. Named `_estimate` so callers can't
    silently treat it as canonical — preserve venue_role alongside.
    """
    if side is None or fill_px is None or best_bid is None or best_ask is None:
        return None
    s = side.lower()
    if s in ("sell", "s", "a"):
        return fill_px >= best_ask
    if s in ("buy", "b"):
        return fill_px <= best_bid
    return None


def build_fill_observation_payload(
    *,
    schema_version: int = SCHEMA_VERSION,
    cloid: Optional[str],
    symbol: str,
    side: Optional[str],
    qty: Optional[float],
    role: Optional[str],
    fill_px: Optional[float],
    fill_ts: Optional[float],
    submit_ts: Optional[float],
    venue_role: Optional[str],
    is_shadow: bool,
    best_bid: Optional[float],
    best_ask: Optional[float],
    depth_top_bid_sum: Optional[float],
    depth_top_ask_sum: Optional[float],
    obi: Optional[float],
    ring: Optional[_StabilityRing],
    now_s: float,
) -> dict[str, Any]:
    """Assemble the fill_observation event payload.

    All fields are present (None if unavailable). The analyzer handles
    None gracefully and counts per-reason missing-context occurrences.
    """
    mid: Optional[float] = None
    spread: Optional[float] = None
    if best_bid is not None and best_ask is not None:
        mid = (best_bid + best_ask) / 2.0
        spread = best_ask - best_bid

    quote_age_ms: Optional[float] = None
    if fill_ts is not None and submit_ts is not None:
        quote_age_ms = max(0.0, (fill_ts - submit_ts) * 1000.0)

    is_maker_estimate = estimate_is_maker(side, fill_px, best_bid, best_ask)

    if ring is not None:
        micro = compute_microstability(ring, now_s)
    else:
        micro = {
            "obi_flips_5s": 0,
            "obi_delta_5s": 0.0,
            "spread_delta_5s": 0.0,
            "depth_top_total_delta_5s": 0.0,
            "median_spread": None,
            "initial_total_depth": None,
            "flag_widening": False,
            "flag_withdrawal": False,
            "flag_flip": False,
            "microstability_status": "no_ring",
            "n_samples_in_window": 0,
        }

    bucket = select_bucket(micro)

    return {
        "schema_version": schema_version,
        "cloid": cloid,
        "symbol": symbol,
        "side": side.lower() if isinstance(side, str) else side,
        "qty": qty,
        "role": role,
        "fill_px": fill_px,
        "fill_ts": fill_ts,
        "submit_ts": submit_ts,
        "quote_age_ms": (
            round(quote_age_ms, 3) if quote_age_ms is not None else None
        ),
        "venue_role": venue_role,
        "is_maker_estimate": is_maker_estimate,
        "is_shadow": bool(is_shadow),
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": round(mid, 8) if mid is not None else None,
        "spread": round(spread, 8) if spread is not None else None,
        "depth_top_bid_sum": (
            round(depth_top_bid_sum, 4)
            if depth_top_bid_sum is not None
            else None
        ),
        "depth_top_ask_sum": (
            round(depth_top_ask_sum, 4)
            if depth_top_ask_sum is not None
            else None
        ),
        "obi": round(obi, 6) if obi is not None else None,
        "stability_bucket": bucket,
        **micro,
    }
