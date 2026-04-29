"""math_core/schedulers.py — γ-independent target inventory generators.

Reference: Almgren & Chriss (2000); Guéant (2016) "The Financial Mathematics of
Market Liquidity"; Cartea, Jaimungal, Penalva (2015) chs. 6-7.

This module is the **scheduler** half of the Scheduler/Quoter architecture.
Each function maps wall-clock time t ∈ [0, T] to a target inventory q*(t)
USD that the system should hold at that instant, given a starting inventory
X. The scheduler answers "where should I be?"; a separate quoter answers
"how do I post orders to track that target?".

Critical design constraint: NONE of these schedulers reference the
temporary-impact coefficient γ (or any per-fee/per-rebate parameter). They
optimize completion shape only. This is the structural fix for the
Bechler-Ludkovski falsification recorded at
docs/regularized_riccati_validation_findings.md — when γ ≈ 10⁻⁶ in the
maker-rebate regime, any 1/γ in the rate denominator blows up. By moving
all γ-dependence to the quoter layer (spread / posting / fill-intensity),
the scheduler stays well-posed at any γ.

This module is NOT imported by strategy/, execution/, or any live-trading
path. Keep it pure-math so trajectory validation runs without touching
engine state.

Three families implemented:

  TWAP — q*(t) = X · (1 − t/T)
    Linear glide. Parameter-free. Unconditionally stable. Control group.

  Exponential decay — q*(t) = X · (e^(−ρt) − e^(−ρT)) / (1 − e^(−ρT))
    Single-knob front-loading. The renormalization ensures q*(T) = 0
    exactly without a terminal flush. ρ is urgency (1/s); larger ρ ⇒
    more front-loaded. ρ → 0 recovers TWAP.

  Sinh-ratio (Almgren-Chriss shape) — q*(t) = X · sinh(κ(T−t)) / sinh(κT)
    The AC closed-form inventory curve, but with κ as a pure shape knob
    divorced from any fee/impact parameter. κ → 0 recovers TWAP. Smooth
    glide to exactly 0 at t = T.

Sign convention: X > 0 ⇒ liquidate-long (q* decreases monotonically toward
0). For acquisition (cover short), pass X < 0 and the curves return
negative q*(t) decaying toward 0 from below.
"""

from __future__ import annotations

import math
from typing import Callable


# ── Validation helpers ───────────────────────────────────────────────────


def _validate_inputs(initial_inventory: float, T: float, t: float) -> None:
    for name, v in (("initial_inventory", initial_inventory), ("T", T), ("t", t)):
        if not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError(f"{name} must be a finite real number, got {v!r}")
    if T <= 0:
        raise ValueError(f"T must be strictly positive, got {T}")
    if t < 0 or t > T:
        raise ValueError(f"t must be in [0, T]=[0, {T}], got {t}")


# ── Scheduler 1: Pure TWAP ────────────────────────────────────────────────


def twap(initial_inventory: float, T: float, t: float, **kwargs) -> float:
    """Time-weighted-average-price schedule. q*(t) = X · (1 − t/T).

    Parameter-free. Returns the linear glide from X at t=0 to 0 at t=T.
    Unaffected by **kwargs; accepts and ignores them so callers can use a
    uniform `scheduler(**params)` interface.
    """
    _validate_inputs(initial_inventory, T, t)
    return initial_inventory * (1.0 - t / T)


# ── Scheduler 2: Exponential decay ────────────────────────────────────────


def exponential(
    initial_inventory: float,
    T: float,
    t: float,
    *,
    rho: float = 2.0,
    **kwargs,
) -> float:
    """Renormalized exponential decay. Boundary-clean: q*(0)=X, q*(T)=0.

    Args:
        initial_inventory: X (USD). Sign carries through.
        T: total horizon (s).
        t: elapsed time (s), 0 ≤ t ≤ T.
        rho: dimensionless urgency parameter — interpreted as ρ·T. Default
             rho = 2.0 means half-life ≈ T·ln(2)/2 ≈ 0.347·T. rho → 0
             recovers TWAP (limit handled explicitly to avoid 0/0).

    Math:
        Let r = ρ/T (rate per second). Then
            q*(t) = X · (e^(−rt) − e^(−rT)) / (1 − e^(−rT))
        which is a smooth, monotone-decreasing curve from X at t=0 to 0 at
        t=T. Equivalent dimensionless form: with τ = t/T and ρ̄ = ρ,
            q*(τ) / X = (e^(−ρ̄τ) − e^(−ρ̄)) / (1 − e^(−ρ̄)).
    """
    _validate_inputs(initial_inventory, T, t)
    if not isinstance(rho, (int, float)) or not math.isfinite(rho) or rho < 0:
        raise ValueError(f"rho must be a finite non-negative number, got {rho!r}")
    if rho < 1e-12:
        return twap(initial_inventory, T, t)
    tau = t / T
    num = math.exp(-rho * tau) - math.exp(-rho)
    den = 1.0 - math.exp(-rho)
    return initial_inventory * (num / den)


# ── Scheduler 3: Sinh-ratio (Almgren-Chriss shape) ────────────────────────


def sinh_ratio(
    initial_inventory: float,
    T: float,
    t: float,
    *,
    kappa: float = 2.0,
    **kwargs,
) -> float:
    """Almgren-Chriss inventory curve, parameterized by pure shape knob κ.

    Args:
        initial_inventory: X (USD).
        T: total horizon (s).
        t: elapsed time (s), 0 ≤ t ≤ T.
        kappa: dimensionless shape parameter — interpreted as κ·T. Default
               kappa = 2.0 gives a moderately front-loaded glide. kappa → 0
               recovers TWAP. Larger kappa ⇒ more front-loaded.

    Math:
        Let k = κ/T. Then
            q*(t) = X · sinh(k(T−t)) / sinh(kT).
        Unlike standard AC, here κ is decoupled from any (λ, η, γ) — it is
        a pure curve-shape parameter chosen by completion preference, not
        derived from an impact model.
    """
    _validate_inputs(initial_inventory, T, t)
    if not isinstance(kappa, (int, float)) or not math.isfinite(kappa) or kappa < 0:
        raise ValueError(f"kappa must be a finite non-negative number, got {kappa!r}")
    if kappa < 1e-12:
        return twap(initial_inventory, T, t)
    tau = t / T
    return initial_inventory * (math.sinh(kappa * (1.0 - tau)) / math.sinh(kappa))


# ── Unified menu (Task 17 sets the table; factory comes in Task 18) ───────


SCHEDULERS: dict[str, Callable[..., float]] = {
    "twap": twap,
    "exponential": exponential,
    "sinh_ratio": sinh_ratio,
}


def get_target_inventory(
    name: str,
    initial_inventory: float,
    T: float,
    t: float,
    **kwargs,
) -> float:
    """Dispatch by name. The Quoter layer should call only this — never the
    underlying functions directly — so the family can be swapped without
    rewriting the quoter."""
    if name not in SCHEDULERS:
        raise KeyError(f"unknown scheduler {name!r}; choices: {sorted(SCHEDULERS)}")
    return SCHEDULERS[name](initial_inventory, T, t, **kwargs)


# ── Sanity check ──────────────────────────────────────────────────────────


if __name__ == "__main__":
    X = 10_000.0
    T = 3600.0
    sample_times = (0.0, 1800.0, 3600.0)

    print(f"X = ${X:,.2f}  T = {T:.0f}s\n")
    print(f"{'t (s)':>8} | {'TWAP':>12} | {'Exp ρ=2':>12} | {'Sinh κ=2':>12}")
    print("-" * 56)
    for t in sample_times:
        q_twap = twap(X, T, t)
        q_exp = exponential(X, T, t, rho=2.0)
        q_sinh = sinh_ratio(X, T, t, kappa=2.0)
        print(
            f"{t:>8.0f} | "
            f"${q_twap:>10,.2f} | "
            f"${q_exp:>10,.2f} | "
            f"${q_sinh:>10,.2f}"
        )

    print("\nMonotonicity / boundary checks:")
    for name in ("twap", "exponential", "sinh_ratio"):
        q0 = get_target_inventory(name, X, T, 0.0)
        qT = get_target_inventory(name, X, T, T)
        prev = q0
        monotone = True
        for i in range(1, 121):
            ti = T * i / 120.0
            qi = get_target_inventory(name, X, T, ti)
            if qi > prev + 1e-9:
                monotone = False
                break
            prev = qi
        print(
            f"  {name:>12}: q*(0)=${q0:,.2f}  q*(T)=${qT:,.4f}  "
            f"monotone_dec={monotone}"
        )

    print("\nLimit checks (should match TWAP):")
    for name, kw in (("exponential", {"rho": 0.0}), ("sinh_ratio", {"kappa": 0.0})):
        q_mid = get_target_inventory(name, X, T, 1800.0, **kw)
        q_twap_mid = twap(X, T, 1800.0)
        print(f"  {name} at zero shape param: ${q_mid:,.2f}  (TWAP: ${q_twap_mid:,.2f})")

    print("\nAcquisition (X<0) — should mirror sign:")
    for name in ("twap", "exponential", "sinh_ratio"):
        q_mid = get_target_inventory(name, -X, T, 1800.0)
        print(f"  {name:>12} at t=1800: ${q_mid:,.2f}")
