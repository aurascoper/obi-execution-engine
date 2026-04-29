"""math_core/fill_model.py — microstructure simulation environment (v1).

Pure simulation. This module is the **environment** half of Gate 2A: it
returns fills, queue state, and post-fill markouts given the quoter's
ExecutionIntent. It does NOT decide regime, offsets, clip size, or TTL —
that remains the quoter's job (math_core/quoter_policy.py).

Design rule (locked in by Task 21 spec): fill model is environment, not
policy. The model answers:
  - how much filled
  - when it filled
  - what the markout was (post-fill mid drift relative to fill price)

The quoter still answers:
  - which regime
  - which offsets
  - which clip
  - which TTL

This module is pure-math and has no engine imports beyond
quoter_policy types (ExecutionIntent / OrderType / Side) which are
themselves engine-isolated.

v1 components:
  1. AR(1) order-book imbalance Y_t around scenario target
  2. AR(1) log-spread (stationary mean, clipped)
  3. Stylized depth model: queue_ahead grows linearly with offset from touch
  4. Aggressor arrival as Poisson; per-aggressor size lognormal
  5. Queue-position fills: aggressor flow consumes queue ahead first,
     residual hits our resting limit (partial fills supported)
  6. Cancellation decay: queue_ahead erodes at a constant rate
  7. IOC execution: immediate fill at touch + half-spread cost
  8. Markout: per-fill mid drift over a forward horizon (positive = good
     for our side, negative = adverse selection)

Sign conventions:
  - state.y_obi: positive = bid pressure (mid rising bias). For a SELLER,
    positive y_obi is adverse (we sell, mid then rises). For a BUYER,
    positive y_obi is also adverse (we buy, must keep paying up).
  - markout_bps: positive = good for our side. For SELL, this is
    (fill_price − future_mid) / fill_price · 10000. For BUY,
    (future_mid − fill_price) / fill_price · 10000.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Optional

from math_core.quoter_policy import ExecutionIntent, OrderType, Side


# ── Parameters ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MicrostructureParams:
    """Time-invariant environment parameters. All fields default to a
    reasonable v1 baseline; scenario differentiation flows through
    obi_target and mid_drift_y_coupling_bps."""

    obi_phi: float = 0.92
    obi_target: float = 0.0
    obi_vol: float = 0.10
    obi_clip: float = 0.99

    spread_log_phi: float = 0.92
    spread_log_mean: float = 0.6931  # log(2)
    spread_log_vol: float = 0.05
    spread_min_bps: float = 0.5
    spread_max_bps: float = 20.0

    touch_depth_usd: float = 60.0
    queue_growth_per_bps: float = 25.0
    queue_cancel_rate_per_s: float = 0.05

    aggressor_arrival_rate_per_s: float = 0.6
    aggressor_buy_prob: float = 0.5
    aggressor_size_mean_usd: float = 70.0
    aggressor_size_cv: float = 0.5

    mid_drift_bps_per_step: float = 0.0
    mid_drift_y_coupling_bps: float = 0.0
    mid_vol_bps_per_step: float = 0.5

    markout_horizon_s: float = 5.0


@dataclass
class MicrostructureState:
    mid: float
    spread_bps: float
    y_obi: float
    t: float


@dataclass
class QuoteState:
    """Resting limit quote state. Defaults to "no quote posted"."""

    posted: bool = False
    side: Side = Side.HOLD
    delta_bps: float = 0.0
    price: float = 0.0
    queue_ahead: float = 0.0
    residual: float = 0.0
    post_time: float = 0.0


@dataclass
class FillRecord:
    t: float
    side: Side
    price: float
    size: float
    mid_at_fill: float
    is_maker: bool
    delta_bps: float
    regime: Optional[str] = None
    mid_at_markout: Optional[float] = None
    markout_bps: Optional[float] = None


# ── Initialization and environment step ──────────────────────────────────


def init_state(mid0: float, params: MicrostructureParams) -> MicrostructureState:
    return MicrostructureState(
        mid=mid0,
        spread_bps=math.exp(params.spread_log_mean),
        y_obi=params.obi_target,
        t=0.0,
    )


def step_environment(
    state: MicrostructureState,
    params: MicrostructureParams,
    dt_s: float,
    rng: random.Random,
) -> None:
    """Advance OBI, spread, mid by dt_s. Mutates state in place."""
    state.y_obi = (
        (1.0 - params.obi_phi) * params.obi_target
        + params.obi_phi * state.y_obi
        + rng.gauss(0.0, params.obi_vol)
    )
    state.y_obi = max(-params.obi_clip, min(params.obi_clip, state.y_obi))

    log_s = (
        (1.0 - params.spread_log_phi) * params.spread_log_mean
        + params.spread_log_phi * math.log(state.spread_bps)
        + rng.gauss(0.0, params.spread_log_vol)
    )
    state.spread_bps = max(
        params.spread_min_bps, min(params.spread_max_bps, math.exp(log_s))
    )

    drift_bps = (
        params.mid_drift_bps_per_step
        + params.mid_drift_y_coupling_bps * state.y_obi
    )
    shock_bps = rng.gauss(0.0, params.mid_vol_bps_per_step)
    state.mid = max(1e-9, state.mid * (1.0 + (drift_bps + shock_bps) / 10_000.0))

    state.t += dt_s


# ── Quote management and fills ────────────────────────────────────────────


def queue_ahead_at(delta_bps: float, params: MicrostructureParams) -> float:
    return max(
        0.0,
        params.touch_depth_usd
        + params.queue_growth_per_bps * max(0.0, delta_bps),
    )


def post_quote(
    intent: ExecutionIntent,
    state: MicrostructureState,
    params: MicrostructureParams,
) -> QuoteState:
    """Build a fresh resting limit from intent. Returns empty QuoteState if
    intent is HOLD, IOC, or has no clip."""
    if intent.side is Side.HOLD or intent.order_type is OrderType.IOC:
        return QuoteState()
    if intent.clip_size <= 0:
        return QuoteState()
    if intent.side is Side.SELL:
        delta_bps = intent.delta_a_bps
        sign = 1.0
    else:
        delta_bps = intent.delta_b_bps
        sign = -1.0
    if delta_bps is None:
        return QuoteState()
    price = state.mid * (1.0 + sign * delta_bps / 10_000.0)
    return QuoteState(
        posted=True,
        side=intent.side,
        delta_bps=delta_bps,
        price=price,
        queue_ahead=queue_ahead_at(delta_bps, params),
        residual=intent.clip_size,
        post_time=state.t,
    )


def _poisson_sample(lam: float, rng: random.Random) -> int:
    if lam <= 0:
        return 0
    if lam > 30.0:
        return max(0, int(round(rng.gauss(lam, math.sqrt(lam)))))
    L = math.exp(-lam)
    k = 0
    p = 1.0
    while True:
        k += 1
        p *= rng.random()
        if p <= L:
            return k - 1


def step_quote_and_fills(
    quote: QuoteState,
    state: MicrostructureState,
    params: MicrostructureParams,
    dt_s: float,
    rng: random.Random,
) -> tuple[QuoteState, list[FillRecord]]:
    """Advance the resting quote by dt_s. Returns (updated_quote, fills).

    Fill mechanism:
      1. queue_ahead decays via cancellations
      2. aggressors arrive Poisson(rate · dt) on the side that hits us
      3. each aggressor's size consumes from queue_ahead first; any excess
         partially or fully fills our residual (partial fills supported)
    """
    fills: list[FillRecord] = []
    if not quote.posted or quote.residual <= 0.0:
        return quote, fills

    quote.queue_ahead *= math.exp(-params.queue_cancel_rate_per_s * dt_s)

    if quote.side is Side.SELL:
        rate = params.aggressor_arrival_rate_per_s * params.aggressor_buy_prob
    else:
        rate = params.aggressor_arrival_rate_per_s * (1.0 - params.aggressor_buy_prob)

    n_arrivals = _poisson_sample(rate * dt_s, rng)
    for _ in range(n_arrivals):
        size = max(
            0.0,
            rng.lognormvariate(
                math.log(max(1e-6, params.aggressor_size_mean_usd)),
                params.aggressor_size_cv,
            ),
        )
        if size <= quote.queue_ahead:
            quote.queue_ahead -= size
            continue
        excess = size - quote.queue_ahead
        quote.queue_ahead = 0.0
        fill_size = min(quote.residual, excess)
        if fill_size > 0.0:
            fills.append(
                FillRecord(
                    t=state.t,
                    side=quote.side,
                    price=quote.price,
                    size=fill_size,
                    mid_at_fill=state.mid,
                    is_maker=True,
                    delta_bps=quote.delta_bps,
                )
            )
            quote.residual -= fill_size
        if quote.residual <= 1e-9:
            return QuoteState(), fills
    return quote, fills


def execute_ioc(
    intent: ExecutionIntent,
    state: MicrostructureState,
    params: MicrostructureParams,
) -> list[FillRecord]:
    """IOC takes liquidity at the touch + half-spread cost. Always fills
    intent.clip_size. Returns one FillRecord."""
    if intent.side is Side.HOLD or intent.clip_size <= 0:
        return []
    sign = 1.0 if intent.side is Side.SELL else -1.0
    half_spread = state.spread_bps / 2.0
    price = state.mid * (1.0 - sign * half_spread / 10_000.0)
    return [
        FillRecord(
            t=state.t,
            side=intent.side,
            price=price,
            size=intent.clip_size,
            mid_at_fill=state.mid,
            is_maker=False,
            delta_bps=-half_spread,
        )
    ]


# ── Markout resolution ────────────────────────────────────────────────────


def resolve_markout(fill: FillRecord, future_mid: float) -> None:
    """Compute and store the post-fill markout in the FillRecord. Sign
    convention: positive = good for our side."""
    if fill.is_maker is False and fill.size <= 0:
        return
    if fill.price <= 0:
        return
    if fill.side is Side.SELL:
        markout = (fill.price - future_mid) / fill.price * 10_000.0
    else:
        markout = (future_mid - fill.price) / fill.price * 10_000.0
    fill.mid_at_markout = future_mid
    fill.markout_bps = markout


# ── Sanity check ──────────────────────────────────────────────────────────


if __name__ == "__main__":
    rng = random.Random(0)
    p = MicrostructureParams(obi_target=0.0, mid_drift_y_coupling_bps=0.0)
    s = init_state(100.0, p)

    print(f"Initial: mid={s.mid}, spread_bps={s.spread_bps:.3f}, y={s.y_obi:.3f}\n")

    print(f"{'t':>6} | {'mid':>8} | {'spread':>7} | {'y_obi':>8}")
    print("-" * 45)
    for i in range(20):
        step_environment(s, p, dt_s=10.0, rng=rng)
        print(f"{s.t:>6.0f} | {s.mid:>8.4f} | {s.spread_bps:>7.3f} | {s.y_obi:>8.4f}")

    print("\nQueue depth ladder:")
    for delta in (0.0, 1.0, 2.0, 5.0, 10.0):
        print(f"  δ={delta:>5.1f} bps  →  queue_ahead = ${queue_ahead_at(delta, p):>7.2f}")

    print("\nAggressor sample sizes (n=10):")
    for _ in range(10):
        size = rng.lognormvariate(math.log(p.aggressor_size_mean_usd), p.aggressor_size_cv)
        print(f"  ${size:>7.2f}")
