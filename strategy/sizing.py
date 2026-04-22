"""
strategy/sizing.py — Kelly-OU fractional sizing.

Closed-form f* for an OU-modeled mean-reverting signal (Lv & Meister, 2009,
arXiv 0903.2910). Pure function, no state, no side effects — trivially unit
testable and safe to call per-bar.

Model:
    dX_t = -θ·X_t dt + σ·dW_t           (OU process, mean-reverting)
    z    = X_t / σ_stationary           (standardized current deviation)
    φ    = exp(-θ·Δt)                   (AR(1) coefficient, Δt = 1 bar)
    f*   = |z| · (1 − φ)                (per-bar reversion-weighted fraction)

Using the (1−φ) form rather than the raw Lv/Meister f*=z·θ/σ² because:
  (a) (1−φ) ∈ (0,1) is natively dimensionless and bar-referenced, so we don't
      need the caller to pre-scale σ into per-bar return units;
  (b) magnitude tracks half-life monotonically (short HL → large fraction,
      long HL → small fraction), which matches the empirical risk of θ-
      estimation error: shorter half-life OU is more statistically stable;
  (c) sidesteps the σ-units ambiguity that caused earlier sizing to return
      f→0 when σ was in raw price units rather than log-return units.

σ is retained in the signature only as a pathological-input guard
(sigma ≤ sigma_floor returns 0).

Applied defensively:
  * Clamp |f*| ≤ `cap` so a single signal can't demand more than the risk-path
    base allocation.
  * Scale by fractional-Kelly `k` ∈ [0.15, 0.35] (literature default 0.25) to
    trade a bit of compound growth for much lower realized variance.
  * Return 0 on any invalid input (σ≤floor, θ≤0, non-finite) so the caller
    falls through to whatever default sizing path exists.

The function returns |f*|-style magnitude ∈ [0, cap]; the caller applies the
trade's own sign (long vs short) separately.
"""

from __future__ import annotations

import math


def kelly_fraction(
    z: float,
    theta: float,
    sigma: float,
    k: float = 0.25,
    cap: float = 1.0,
    sigma_floor: float = 1e-6,
) -> float:
    """
    Fractional-Kelly multiplier for a mean-reverting trade.

    Parameters
    ----------
    z         : current z-score of the signal (sign preserved in |·| below)
    theta     : mean-reversion speed (1/half-life, same time units as σ)
    sigma     : residual std-dev of the process
    k         : fractional-Kelly coefficient (0.25 = quarter-Kelly default)
    cap       : upper bound on returned magnitude (1.0 = 100% of base notional)
    sigma_floor : treat σ ≤ this as pathological; return 0

    Returns
    -------
    float ∈ [0, cap] — multiplier to apply to the base notional budget.
    Returns 0 on invalid inputs rather than raising; the caller can cleanly
    fall back to fixed sizing.
    """
    if not (math.isfinite(z) and math.isfinite(theta) and math.isfinite(sigma)):
        return 0.0
    if theta <= 0.0:
        return 0.0
    if sigma <= sigma_floor:
        return 0.0
    if k <= 0.0 or cap <= 0.0:
        return 0.0

    # f* = |z| · (1 − φ), with φ = exp(−θ·Δt) and Δt = 1 bar baked into θ.
    # Dimensionless; no dependence on σ beyond the pathological-input guard.
    phi = math.exp(-theta)
    f_star = abs(z) * (1.0 - phi)
    scaled = k * f_star
    if scaled >= cap:
        return float(cap)
    return float(scaled)
