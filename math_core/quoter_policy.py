"""math_core/quoter_policy.py — pure-math quoter (TWAP-first build).

Reference: docs/quoter_design_spec.md (Task 18).
Predecessors:
  - math_core/schedulers.py — γ-independent target-inventory curves.
  - docs/regularized_riccati_validation_findings.md — BL falsification
    that motivated the scheduler/quoter split.

Job: consume scheduler target q*(t) + book/OFI snapshot + risk limits and
emit one ExecutionIntent per decision step. Three layers:

  Layer A — state reduction:    e_t, τ_t, u_t
  Layer B — regime classifier:  PASSIVE / TOUCH / CATCHUP
  Layer C — intent builder:     reservation price + offsets + clip + TTL

This module is pure-math and isolated from strategy/, execution/, and any
live path. It does not import anything from the engine. The quoter is
deliberately simple in this first build: transparent thresholds, no
optimization, no learned components, no cancel-replace state machine. Just
enough policy to test the tracking bar against the TWAP scheduler under
synthetic OFI / fill regimes.

Critical invariant (the structural fix from the BL falsification): NO
1/γ term anywhere. The reservation price is `m_t − θ_q·q_t − θ_e·e_t − θ_y·Y_t`
with all coefficients dimensionful in price-per-input, never `1/γ`.

Sign convention:
  initial_inventory > 0 → liquidating long (selling)
  initial_inventory < 0 → liquidating short (buying)
  e_t > 0 always means "behind on the required action" regardless of side.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Optional


# ── Enums ─────────────────────────────────────────────────────────────────


class Regime(str, Enum):
    PASSIVE = "passive"
    TOUCH = "touch"
    CATCHUP = "catchup"


class Side(str, Enum):
    SELL = "sell"
    BUY = "buy"
    HOLD = "hold"


class OrderType(str, Enum):
    POST_ONLY = "post_only"
    IOC = "ioc"


# ── Inputs / params / outputs ─────────────────────────────────────────────


@dataclass(frozen=True)
class QuoterInputs:
    """One snapshot's worth of state the quoter consumes."""

    t: float
    T: float
    q_t: float
    q_star_t: float
    mid: float
    touch_spread_bps: float
    y_toxicity: float
    initial_inventory: float

    def __post_init__(self) -> None:
        for name in ("t", "T", "q_t", "q_star_t", "mid", "touch_spread_bps", "y_toxicity"):
            v = getattr(self, name)
            if not isinstance(v, (int, float)) or not math.isfinite(v):
                raise ValueError(f"QuoterInputs.{name} must be finite, got {v!r}")
        if self.T <= 0:
            raise ValueError(f"T must be > 0, got {self.T}")
        if self.t < 0 or self.t > self.T + 1e-9:
            raise ValueError(f"t must be in [0, T], got {self.t} with T={self.T}")
        if self.mid <= 0:
            raise ValueError(f"mid must be > 0, got {self.mid}")


@dataclass(frozen=True)
class QuoterParams:
    """Tunable parameters. Defaults are deliberate-and-transparent, NOT
    optimized. The acceptance bar in docs/quoter_design_spec.md applies to
    these defaults; tuning is a separate task.
    """

    e_passive_frac: float = 0.05
    e_catchup_frac: float = 0.20
    tau_terminal_frac: float = 0.05
    toxicity_threshold: float = 0.30
    tau_min_s: float = 1.0

    alpha_q: float = 1e-4
    alpha_e: float = 5e-4
    alpha_y: float = 5e-4

    delta_passive_bps: float = 5.0
    delta_touch_bps: float = 0.0

    s_base: float = 50.0
    s_max: float = 500.0
    s_catchup_max: float = 2000.0
    eta_u: float = 1.0
    eta_e_pos: float = 0.05
    s_mult_passive: float = 1.0
    s_mult_touch: float = 2.0
    s_mult_catchup: float = 5.0

    ttl_passive_s: float = 60.0
    ttl_touch_s: float = 15.0
    ttl_catchup_s: float = 1.0


@dataclass(frozen=True)
class ExecutionIntent:
    side: Side
    regime: Regime
    order_type: OrderType
    delta_a_bps: Optional[float]
    delta_b_bps: Optional[float]
    clip_size: float
    ttl_s: float
    e_t: float
    tau_t: float
    u_t: float
    reservation_price: float


@dataclass(frozen=True)
class TrackingMetrics:
    """Per-run summary metrics for the acceptance bar. The synthetic harness
    populates these; the quoter itself does not produce them.

    `max_behind_e` is the one-sided (positive-only) tracking miss — being
    *ahead* of schedule is a deliberate policy outcome under toxic OFI, not
    a tracking failure. The acceptance bar uses `max_behind_e`. `max_abs_e`
    is retained for diagnostics only.
    """

    max_abs_e: float
    max_behind_e: float
    terminal_q: float
    sign_crossings: int
    catchup_step_fraction: float
    passive_step_fraction: float
    touch_step_fraction: float
    maker_fill_count: int
    taker_fill_count: int
    forced_terminal_flush: bool
    completion_time_s: Optional[float]


# ── Layer A: state reduction ──────────────────────────────────────────────


def _liquidation_side(initial_inventory: float, q_t: float) -> Side:
    if abs(q_t) < 1e-9:
        return Side.HOLD
    if initial_inventory > 0:
        return Side.SELL if q_t > 0 else Side.HOLD
    if initial_inventory < 0:
        return Side.BUY if q_t < 0 else Side.HOLD
    return Side.HOLD


def reduce_state(inp: QuoterInputs, params: QuoterParams) -> dict:
    sign0 = 1.0 if inp.initial_inventory >= 0 else -1.0
    e_signed = sign0 * (inp.q_t - inp.q_star_t)
    tau_t = max(0.0, inp.T - inp.t)
    u_t = max(0.0, e_signed) / max(tau_t, params.tau_min_s)
    return {
        "e_t": e_signed,
        "tau_t": tau_t,
        "u_t": u_t,
        "side": _liquidation_side(inp.initial_inventory, inp.q_t),
    }


# ── Layer B: regime classifier ────────────────────────────────────────────


def classify_regime(
    state: dict,
    inp: QuoterInputs,
    params: QuoterParams,
) -> Regime:
    if state["side"] is Side.HOLD:
        return Regime.PASSIVE

    e_norm = abs(state["e_t"]) / max(abs(inp.initial_inventory), 1.0)
    tau_frac = state["tau_t"] / inp.T if inp.T > 0 else 0.0
    behind = state["e_t"] > 0
    toxic = inp.y_toxicity > params.toxicity_threshold

    if behind and tau_frac < params.tau_terminal_frac:
        return Regime.CATCHUP
    if behind and e_norm > params.e_catchup_frac:
        return Regime.CATCHUP
    if behind and toxic and e_norm > params.e_passive_frac:
        return Regime.CATCHUP

    if behind and e_norm > params.e_passive_frac:
        return Regime.TOUCH
    if toxic:
        return Regime.TOUCH

    return Regime.PASSIVE


# ── Layer C: reservation price + intent builder ───────────────────────────


def reservation_price(
    inp: QuoterInputs,
    state: dict,
    params: QuoterParams,
) -> float:
    q_norm = max(abs(inp.initial_inventory), 1.0)
    inv_skew = params.alpha_q * (inp.q_t / q_norm)
    miss_skew = params.alpha_e * (state["e_t"] / q_norm)
    tox_skew = params.alpha_y * inp.y_toxicity
    return inp.mid * (1.0 - inv_skew - miss_skew - tox_skew)


def _clip_size(state: dict, regime: Regime, params: QuoterParams) -> float:
    mult = {
        Regime.PASSIVE: params.s_mult_passive,
        Regime.TOUCH: params.s_mult_touch,
        Regime.CATCHUP: params.s_mult_catchup,
    }[regime]
    base = (
        params.s_base * mult
        + params.eta_u * state["u_t"]
        + params.eta_e_pos * max(0.0, state["e_t"])
    )
    if regime is Regime.CATCHUP:
        return min(params.s_catchup_max, max(0.0, base))
    return min(params.s_max, max(0.0, base))


def build_intent(inp: QuoterInputs, params: QuoterParams) -> ExecutionIntent:
    state = reduce_state(inp, params)
    regime = classify_regime(state, inp, params)
    r_t = reservation_price(inp, state, params)
    side = state["side"]
    size = _clip_size(state, regime, params) if side is not Side.HOLD else 0.0

    delta_a: Optional[float] = None
    delta_b: Optional[float] = None
    if regime is Regime.PASSIVE:
        order_type = OrderType.POST_ONLY
        if side is Side.SELL:
            delta_a = params.delta_passive_bps
        elif side is Side.BUY:
            delta_b = params.delta_passive_bps
        ttl = params.ttl_passive_s
    elif regime is Regime.TOUCH:
        order_type = OrderType.POST_ONLY
        if side is Side.SELL:
            delta_a = params.delta_touch_bps
        elif side is Side.BUY:
            delta_b = params.delta_touch_bps
        ttl = params.ttl_touch_s
    else:
        order_type = OrderType.IOC
        ttl = params.ttl_catchup_s

    return ExecutionIntent(
        side=side,
        regime=regime,
        order_type=order_type,
        delta_a_bps=delta_a,
        delta_b_bps=delta_b,
        clip_size=size,
        ttl_s=ttl,
        e_t=state["e_t"],
        tau_t=state["tau_t"],
        u_t=state["u_t"],
        reservation_price=r_t,
    )


# ── Sanity check ──────────────────────────────────────────────────────────


if __name__ == "__main__":
    params = QuoterParams()
    print("Quoter sanity check (TWAP scheduler, $10k long liquidation):\n")
    print(f"{'t':>5} | {'q_t':>10} | {'q*':>10} | {'e_t':>10} | {'u_t':>8} | "
          f"{'regime':>8} | {'r_t':>10} | {'clip':>7} | type")
    print("-" * 110)

    X = 10_000.0
    T = 3600.0
    mid = 100.0

    scenarios = [
        ("on-track / neutral", 5000.0, 5000.0, 0.0),
        ("behind / neutral",   8500.0, 5000.0, 0.0),
        ("behind / toxic",     8500.0, 5000.0, 0.6),
        ("ahead / favorable",  3000.0, 5000.0, -0.4),
        ("terminal / behind",  500.0,  100.0,  0.0),
        ("done",               0.0,    0.0,    0.0),
    ]
    for label, q, q_star, y in scenarios:
        t = T * 0.5 if "terminal" not in label else T * 0.98
        inp = QuoterInputs(
            t=t, T=T, q_t=q, q_star_t=q_star, mid=mid,
            touch_spread_bps=2.0, y_toxicity=y, initial_inventory=X,
        )
        intent = build_intent(inp, params)
        delta = (
            f"a={intent.delta_a_bps}" if intent.delta_a_bps is not None
            else f"b={intent.delta_b_bps}" if intent.delta_b_bps is not None
            else "—"
        )
        print(
            f"{t:>5.0f} | {q:>10.1f} | {q_star:>10.1f} | {intent.e_t:>10.1f} | "
            f"{intent.u_t:>8.2f} | {intent.regime.value:>8} | "
            f"{intent.reservation_price:>10.4f} | {intent.clip_size:>7.1f} | "
            f"{intent.order_type.value} {delta}  [{label}]"
        )
