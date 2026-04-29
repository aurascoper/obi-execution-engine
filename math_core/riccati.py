"""math_core/riccati.py — Bechler-Ludkovski optimal-execution Riccati solver.

Reference: Bechler & Ludkovski (2014) arXiv:1409.2618.

This module is the **sandbox** Riccati implementation. It is NOT imported by
strategy/, execution/, or any live-trading path. The wired version lives at
strategy/optimal_rate.py. Keep this file pure-math so trajectory validation
runs without touching engine state.

Scope:
  * Inventory-only closed form (the simplification that yields the
    Almgren-Chriss tanh/coth solution under constant impact + inventory
    risk and forced terminal liquidation).
  * Backward-integration ODE solver (forward in time-to-go τ = T − t) for
    the inventory-feedback coefficient h(τ), which is the building block
    of any non-trivial Riccati closed form. Implemented for transparency
    and parameter-sweep utility, even though the closed form makes it
    redundant when (η, λ) are constant.

What this module deliberately does NOT do:
  * The Y-feedback term ν_Y(τ) · Y_t that BL adds for OFI-driven trading.
    That term requires (β, σ, κ, η_leak) — accepted in the dataclass for
    forward compatibility but currently ignored by the solver. The full
    closed-form OFI integration is in strategy/optimal_rate.py.
  * Time-varying η, λ, β, σ. All inputs assumed piecewise constant.
  * Numerical integration of stochastic paths. Trajectory generation
    here is the deterministic optimal-control mean path; for shadow
    Monte-Carlo, drive a separate simulator that calls optimal_rate()
    at each step.

Math (constant-coefficient Almgren-Chriss):

  HJB ansatz:    V(t, x) = h(τ) · x²,  τ = T − t
  Riccati:       dh/dτ = h²/η − λ
  Boundary:      h(τ→0) = ∞  (terminal liquidation forced)

  Equilibrium:   h_∞ = √(λ · η)
  Closed form:   h(τ) = √(λη) · coth(γ τ)         where  γ = √(λ/η)

  Optimal rate:  α*(τ, x) = h(τ) · x / η = γ · coth(γτ) · x      [USD/s]
  Inventory:     x*(t) = x₀ · sinh(γ(T−t)) / sinh(γT)

  TWAP limit (λ = 0):   α*(τ, x) = x / τ ;  x*(t) = x₀ (1 − t/T)

Sign convention:  α > 0 ⇒ liquidating (x decreasing toward 0). For
acquisition, pass −x₀ and negate the resulting rate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


# ── Inputs ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BLInputs:
    """Calibrated parameters per the BL paper, plus the inventory-risk
    coefficient λ which is a free knob (not directly observable from logs).

    Attributes:
        beta:                OFI mean-reversion rate (1/s). From AR(1) on
                             signal_tick.obi (see scripts/calibrate_bl_params.py).
                             NOT used in the inventory-only closed form;
                             accepted for forward-compatibility with the
                             Y-feedback extension.
        sigma:               OFI driving-noise vol (dimensionless). Same
                             provenance as beta. Same forward-compatibility.
        eta_bps_per_dollar:  Temporary linear price impact, bps per $1 of
                             notional. From mid-referenced taker-only fit.
                             USED in the closed form.
        risk_aversion_lambda: Inventory penalty coefficient. Positive ⇒
                             shorter half-life (faster liquidation early).
                             Zero ⇒ TWAP. Pick to set γ = √(λ/η) on a
                             desired time scale.
    """

    beta: float
    sigma: float
    eta_bps_per_dollar: float
    risk_aversion_lambda: float = 0.0

    def __post_init__(self) -> None:
        for name in ("beta", "sigma", "eta_bps_per_dollar"):
            v = getattr(self, name)
            if not isinstance(v, (int, float)) or not math.isfinite(v):
                raise ValueError(f"BLInputs.{name} must be a finite real number")
        if self.eta_bps_per_dollar < 0:
            raise ValueError("eta_bps_per_dollar must be non-negative")
        if self.risk_aversion_lambda < 0:
            raise ValueError("risk_aversion_lambda must be non-negative")


def _eta_fraction(inputs: BLInputs) -> float:
    """bps/$ → fraction (so cost ν² · η_frac is in $/s when ν is in $/s)."""
    return inputs.eta_bps_per_dollar / 1.0e4


# ── Closed-form (inventory-only) ──────────────────────────────────────────


def gamma(inputs: BLInputs) -> float:
    """γ = √(λ / η) — the AC time-scale (1/s). γ·T sets the curvature of
    the optimal trajectory: larger γT ⇒ more front-loaded. Returns 0.0 in
    the TWAP regime (λ = 0)."""
    eta = _eta_fraction(inputs)
    if inputs.risk_aversion_lambda <= 0 or eta <= 0:
        return 0.0
    return math.sqrt(inputs.risk_aversion_lambda / eta)


def half_life_seconds(inputs: BLInputs) -> float | None:
    """ln(2) / γ. None in the TWAP regime."""
    g = gamma(inputs)
    if g <= 0:
        return None
    return math.log(2.0) / g


def h_coefficient(t_remaining: float, inputs: BLInputs) -> float:
    """The Riccati solution h(τ) at time-to-go τ = t_remaining.

    Returns ∞ at τ ≤ 0 (terminal singularity). For τ > 0 returns the
    closed-form: h(τ) = √(λ η) · coth(γτ) (or +∞ if λ=0 since terminal
    is forced and the TWAP ν = x/τ corresponds to h(τ) = η/τ).
    """
    if t_remaining <= 0:
        return math.inf
    eta = _eta_fraction(inputs)
    if inputs.risk_aversion_lambda <= 0 or eta <= 0:
        # TWAP regime: ν = x/τ ⇒ h(τ) = η/τ
        return eta / t_remaining if eta > 0 else math.inf
    g = gamma(inputs)
    arg = g * t_remaining
    return math.sqrt(inputs.risk_aversion_lambda * eta) * (
        math.cosh(arg) / math.sinh(arg)
    )


def optimal_rate(
    inventory: float,
    t_remaining: float,
    inputs: BLInputs,
) -> dict:
    """Optimal instantaneous trading rate α*(τ, x).

    Args:
        inventory: signed dollar inventory. Positive ⇒ long ⇒ liquidate
                   means α > 0 (selling rate).
        t_remaining: τ = T − t in seconds. Strictly positive.
        inputs:    calibrated BL parameters.

    Returns:
        dict with:
          rate         — α* in $/s. Sign matches `inventory`.
          regime       — "twap_linear" | "almgren_chriss" | "terminal"
          gamma        — γ used (0 in TWAP regime)
          coth_arg     — γ·τ (None in TWAP)
          h_tau        — h(τ) value at this τ
          half_life_s  — ln(2)/γ (None in TWAP)

    Raises ValueError for negative t_remaining (caller must clip first).
    """
    if t_remaining < 0:
        raise ValueError(f"t_remaining must be non-negative, got {t_remaining}")
    if t_remaining == 0:
        # Terminal: liquidate everything instantaneously. Use a sentinel
        # large rate; caller is expected to handle the boundary outside
        # the integration loop.
        return {
            "rate": math.copysign(math.inf, inventory) if inventory != 0 else 0.0,
            "regime": "terminal",
            "gamma": gamma(inputs),
            "coth_arg": None,
            "h_tau": math.inf,
            "half_life_s": half_life_seconds(inputs),
        }

    eta = _eta_fraction(inputs)
    g = gamma(inputs)

    if inputs.risk_aversion_lambda <= 0 or eta <= 0:
        # TWAP / linear-decay regime
        return {
            "rate": inventory / t_remaining,
            "regime": "twap_linear",
            "gamma": 0.0,
            "coth_arg": None,
            "h_tau": h_coefficient(t_remaining, inputs),
            "half_life_s": None,
        }

    arg = g * t_remaining
    # Numerical guard: very large τ → coth(γτ) → 1 from above, near-flat
    # rate. Very small τ → coth(γτ) → 1/(γτ) → blows up; clip when needed.
    coth = math.cosh(arg) / math.sinh(arg)
    rate = g * coth * inventory
    return {
        "rate": rate,
        "regime": "almgren_chriss",
        "gamma": g,
        "coth_arg": arg,
        "h_tau": h_coefficient(t_remaining, inputs),
        "half_life_s": math.log(2.0) / g,
    }


# ── Trajectory simulation (deterministic mean path) ───────────────────────


def trajectory(
    initial_inventory: float,
    horizon_s: float,
    inputs: BLInputs,
    n_steps: int = 60,
) -> list[dict]:
    """Forward Euler integration of the deterministic optimal trajectory.

    Returns a list of n_steps+1 dicts, one per timestep, each containing:
      t                 — wall-clock seconds since trajectory start
      t_remaining       — T − t
      inventory         — x*(t)
      rate_usd_per_sec  — α*(t)
      cumulative_traded — running sum of |α*| · dt

    The Almgren-Chriss closed form admits an analytical x(t) (the sinh
    ratio); this Euler integration is provided for parameter-sweep
    diagnostics and for sanity-checking the analytical formula.
    """
    if horizon_s <= 0:
        raise ValueError(f"horizon_s must be positive, got {horizon_s}")
    if n_steps < 1:
        raise ValueError(f"n_steps must be ≥ 1, got {n_steps}")

    dt = horizon_s / n_steps
    out: list[dict] = []
    inv = initial_inventory
    cumulative = 0.0

    for i in range(n_steps + 1):
        t = i * dt
        t_rem = horizon_s - t
        if t_rem < 0:
            t_rem = 0.0

        if t_rem == 0:
            # Force-flatten residual at terminal step.
            rate = inv / max(dt, 1e-9) if abs(inv) > 1e-9 else 0.0
            regime = "terminal_flush"
        else:
            try:
                step = optimal_rate(inv, t_rem, inputs)
            except ValueError:
                rate = 0.0
                regime = "error"
            else:
                rate = step["rate"]
                regime = step["regime"]

        out.append(
            {
                "t": t,
                "t_remaining": t_rem,
                "inventory": inv,
                "rate_usd_per_sec": rate,
                "regime": regime,
                "cumulative_traded": cumulative,
            }
        )

        if i < n_steps:
            traded = rate * dt
            inv -= traded
            cumulative += abs(traded)
            # Sign-snap when very close to zero to avoid Euler drift past 0.
            if abs(inv) < 1e-6:
                inv = 0.0

    return out


def trajectory_analytical(
    initial_inventory: float,
    horizon_s: float,
    inputs: BLInputs,
    n_steps: int = 60,
) -> list[dict]:
    """Analytical Almgren-Chriss trajectory: x(t) = x₀ · sinh(γ(T−t)) / sinh(γT).

    Uses the closed form rather than Euler integration. Returns the same
    schema as `trajectory()` for direct comparison.
    """
    if horizon_s <= 0:
        raise ValueError(f"horizon_s must be positive, got {horizon_s}")
    if n_steps < 1:
        raise ValueError(f"n_steps must be ≥ 1, got {n_steps}")

    g = gamma(inputs)
    out: list[dict] = []
    dt = horizon_s / n_steps
    cumulative = 0.0
    prev_inv = initial_inventory

    for i in range(n_steps + 1):
        t = i * dt
        t_rem = horizon_s - t
        if t_rem < 0:
            t_rem = 0.0

        if g <= 0:
            inv = initial_inventory * (1.0 - t / horizon_s)
            rate = initial_inventory / horizon_s
        elif t_rem <= 0:
            inv = 0.0
            rate = 0.0
        else:
            inv = initial_inventory * (math.sinh(g * t_rem) / math.sinh(g * horizon_s))
            rate = (
                g
                * (math.cosh(g * t_rem) / math.sinh(g * horizon_s))
                * initial_inventory
            )

        if i > 0:
            traded = abs(prev_inv - inv)
            cumulative += traded

        out.append(
            {
                "t": t,
                "t_remaining": t_rem,
                "inventory": inv,
                "rate_usd_per_sec": rate,
                "regime": "twap_linear" if g <= 0 else "almgren_chriss_analytical",
                "cumulative_traded": cumulative,
            }
        )

        prev_inv = inv

    return out
