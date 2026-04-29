"""math_core/regularized_riccati.py — γ-regularized inventory-control scheduler.

Status: CANDIDATE (not yet baseline). Provisional sandbox for the
maker-rebate regime where γ → 0. NOT a theorem-preserving extension of
Bechler-Ludkovski — it is an engineering regularization to test whether
bounded, monotone, terminally-clean liquidation can be recovered when
the natural impact term collapses.

NOT imported by strategy/, execution/, or any live-trading path.

Reference and rationale:
  * BL's verification theorem (Bechler & Ludkovski 2014/2015) requires
    γ > 0 for well-posedness. No published BL result covers γ → 0.
  * Crypto-execution work (Zhuo et al. 2023; Avellaneda-Stoikov 2008)
    drops impact entirely and adds inventory/fill-uncertainty terms to
    bound trading speed.
  * Hybrid scheduler-quoter frameworks (Guo & Jin 2025; Di Giacinto et
    al. 2024) couple liquidation with quoting through coupled Riccati
    systems whose well-posedness comes from the inventory-cost term,
    NOT the impact term.

Regularized objective (this module's actual model):

    J = E[ ∫₀ᵀ (γ α² + φ q² + κ Y²) dt + p · q_T² ]

with γ̂ = max(γ, ε) used in all denominators where γ would otherwise
appear, and ε ≥ 0 a γ-INDEPENDENT regularization floor.

Reduced ODE system in time-to-go τ = T − t (η_leak ≡ 0; B-coefficient
absorbed since it stays at 0 when η_leak = 0):

    A'(τ) = φ − A²/γ̂                                     A(0) = p
    C'(τ) = (σ²·A/γ̂) − (κ + A/γ̂)·C                       C(0) = 0

Optimal feedback (single asset, scalar OFI):

    α*(τ, q, Y) = (2A·q + C·Y) / (2γ̂)

Sign convention: α > 0 ⇒ liquidating long inventory.

Implementation notes:
  * A(τ) admits a closed form (the ODE A' = φ − A²/γ̂ is a Riccati of
    its own with constant coefficients):
        A_eq = √(φ·γ̂),    γ_AC = √(φ/γ̂)
        if A_eq < p:  A(τ) = A_eq · coth(γ_AC·τ + arctanh(A_eq/p))
        if A_eq > p:  A(τ) = A_eq · tanh(γ_AC·τ + arctanh(p/A_eq))
        if φ = 0:     A(τ) = p (constant)
  * Using the closed form avoids numerical stiffness near τ=0 where
    naive RK4 with finite step blows up (A_dot ≈ −p²/γ̂ initially).
  * C(τ) is integrated via RK4 using the closed-form A; bounded RK4
    handles it cleanly since A is bounded.

What this module DELIBERATELY does NOT do:
  * The full BL/AC system with η_leak ≠ 0 (live in strategy/optimal_rate.py)
  * Apply a hard rate cap on α (caller's responsibility)
  * Generate stochastic OU paths for Y (deterministic Y_static input)
  * Optimize p, ε, or φ — those are operator-tuned and swept externally
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class RegInputs:
    """γ-regularized objective parameters.

    All terms γ-independent except `gamma` itself, which can be ≪ epsilon
    in the maker-rebate regime — the floor takes over.

    Attributes:
        gamma     : measured impact coefficient (can be 1e-8 or smaller).
        phi       : γ-INDEPENDENT inventory-risk floor.
        epsilon   : minimum effective γ; γ̂ = max(gamma, epsilon).
        kappa     : OFI toxicity penalty (running cost, scalar Y²).
        sigma     : OU vol of the OFI process (drives C via σ²·A/γ̂ term).
        p         : terminal-inventory penalty.
        beta      : OU mean-reversion rate (passed through; affects the
                    OFI dynamics in the FULL model but is absorbed into
                    the C ODE here via −β·C term — set to 0 for a closed
                    AC-style sandbox).
    """

    gamma: float
    phi: float
    epsilon: float = 0.0
    kappa: float = 0.0
    sigma: float = 0.0
    beta: float = 0.0
    p: float = 1.0

    def gamma_hat(self) -> float:
        """The effective γ used in denominators."""
        return max(self.gamma, self.epsilon)

    def validate(self) -> None:
        if self.gamma < 0:
            raise ValueError(f"gamma must be ≥ 0; got {self.gamma}")
        if self.phi < 0:
            raise ValueError(f"phi must be ≥ 0; got {self.phi}")
        if self.epsilon < 0:
            raise ValueError(f"epsilon must be ≥ 0; got {self.epsilon}")
        if self.kappa < 0:
            raise ValueError(f"kappa must be ≥ 0; got {self.kappa}")
        if self.sigma < 0:
            raise ValueError(f"sigma must be ≥ 0; got {self.sigma}")
        if self.p <= 0:
            raise ValueError(f"p must be > 0; got {self.p}")
        if self.gamma_hat() <= 0:
            raise ValueError(
                f"gamma_hat = max(gamma, epsilon) must be > 0; got {self.gamma_hat()}"
            )


def solve_a_closed_form(tau: float, params: RegInputs) -> float:
    """Closed-form A(τ) for the regularized Riccati. Bounded for all τ ≥ 0."""
    g_hat = params.gamma_hat()
    if params.phi <= 0:
        return params.p
    a_eq = math.sqrt(params.phi * g_hat)
    g_ac = math.sqrt(params.phi / g_hat)
    if a_eq <= 0:
        return params.p
    if abs(a_eq - params.p) < 1e-15:
        return params.p
    if a_eq < params.p:
        # coth form — A decays from p to A_eq from above
        phi_0 = math.atanh(a_eq / params.p)
        arg = g_ac * tau + phi_0
        if arg > 50.0:
            return a_eq
        if arg < 1e-9:
            return params.p
        return a_eq * (math.cosh(arg) / math.sinh(arg))
    else:
        # tanh form — A grows from p to A_eq from below
        phi_0 = math.atanh(params.p / a_eq)
        arg = g_ac * tau + phi_0
        return a_eq * math.tanh(arg)


def solve_riccati_path(
    horizon_T: float,
    n_steps: int,
    params: RegInputs,
) -> dict:
    """Build A(τ), C(τ) tables on a uniform τ-grid in [0, T].

    A uses the closed form. C is RK4-integrated using the closed-form A.
    Returns dict with arrays plus diagnostics.
    """
    if horizon_T <= 0:
        raise ValueError(f"horizon_T must be > 0; got {horizon_T}")
    if n_steps < 16:
        raise ValueError(f"n_steps must be ≥ 16; got {n_steps}")
    params.validate()

    g_hat = params.gamma_hat()
    dtau = horizon_T / n_steps
    a_arr: list[float] = []
    for i in range(n_steps + 1):
        a_arr.append(solve_a_closed_form(i * dtau, params))

    # C(τ) — coupled RK4 with closed-form A from the table
    def c_rhs(a_val: float, c_val: float) -> float:
        return (
            (params.sigma * params.sigma * a_val / g_hat)
            - (params.kappa + a_val / g_hat) * c_val
            - params.beta * c_val  # β·C drag from BL OU dynamics
        )

    c_arr: list[float] = [0.0]
    c_cur = 0.0
    diverged = False
    for i in range(n_steps):
        a_i = a_arr[i]
        a_ip = a_arr[i + 1]
        a_mid = 0.5 * (a_i + a_ip)
        try:
            k1 = c_rhs(a_i, c_cur)
            k2 = c_rhs(a_mid, c_cur + 0.5 * dtau * k1)
            k3 = c_rhs(a_mid, c_cur + 0.5 * dtau * k2)
            k4 = c_rhs(a_ip, c_cur + dtau * k3)
            c_cur = c_cur + (dtau / 6.0) * (k1 + 2 * k2 + 2 * k3 + k4)
        except OverflowError:
            diverged = True
            c_cur = math.nan
        if not math.isfinite(c_cur):
            diverged = True
        c_arr.append(c_cur)

    return {
        "tau_grid": [i * dtau for i in range(n_steps + 1)],
        "A": a_arr,
        "C": c_arr,
        "gamma_hat": g_hat,
        "params": params,
        "horizon_T": horizon_T,
        "n_steps": n_steps,
        "all_a_finite": all(math.isfinite(x) for x in a_arr),
        "all_c_finite": not diverged,
    }


def trajectory(
    inventory_q0: float,
    horizon_T: float,
    n_steps: int,
    params: RegInputs,
    y_static: float = 0.0,
    apply_cap: bool = False,
) -> dict:
    """Forward-simulate inventory under α*(τ, q, Y).

    By default applies NO rate cap — the test is whether the natural
    regularized policy is bounded by itself. Set `apply_cap=True` to
    enforce |α| ≤ |q|/τ for diagnostic comparison.
    """
    sol = solve_riccati_path(horizon_T, n_steps, params)
    if not sol["all_a_finite"]:
        return _bail_out(sol, inventory_q0, "A diverged")

    a_table = sol["A"]
    c_table = sol["C"]
    g_hat = sol["gamma_hat"]

    dt = horizon_T / n_steps
    inv = inventory_q0
    cum_traded = 0.0
    cap_hits = 0
    max_abs_rate = 0.0
    sign_crossings = 0
    monotone = True
    initial_sign = 1.0 if inventory_q0 > 0 else (-1.0 if inventory_q0 < 0 else 0.0)
    points: list[dict] = []
    diverged = False

    for i in range(n_steps + 1):
        t = i * dt
        tau = horizon_T - t
        if tau < 0:
            tau = 0.0
        j = max(0, min(n_steps - i, n_steps))
        a_at = a_table[j]
        c_at = c_table[j]

        if not (math.isfinite(a_at) and math.isfinite(c_at)):
            diverged = True
            alpha = math.nan
        elif tau > 0:
            try:
                alpha_raw = (2.0 * a_at * inv + c_at * y_static) / (2.0 * g_hat)
            except OverflowError:
                alpha_raw = math.nan
            cap_at_tau = abs(inv) / tau if tau > 0 else math.inf
            if math.isfinite(alpha_raw) and abs(alpha_raw) > cap_at_tau:
                cap_hits += 1
                alpha = math.copysign(cap_at_tau, alpha_raw) if apply_cap else alpha_raw
            else:
                alpha = alpha_raw
        else:
            alpha = inv / max(dt, 1e-9) if abs(inv) > 1e-9 else 0.0

        if math.isfinite(alpha) and abs(alpha) > max_abs_rate:
            max_abs_rate = abs(alpha)

        # Sign crossing detection
        cur_sign = 1.0 if inv > 1e-9 else (-1.0 if inv < -1e-9 else 0.0)
        if initial_sign != 0 and cur_sign != 0 and cur_sign != initial_sign:
            sign_crossings += 1
            initial_sign = cur_sign  # reset, count further crossings

        points.append(
            {
                "t": t,
                "tau": tau,
                "inventory": inv,
                "rate": alpha,
                "A_at_tau": a_at,
                "C_at_tau": c_at,
                "cumulative_traded": cum_traded,
            }
        )

        if i < n_steps and tau > 0 and math.isfinite(alpha):
            traded = alpha * dt
            new_inv = inv - traded
            # Monotone if inventory moves toward 0 (or stays)
            if abs(new_inv) > abs(inv) + 1e-9:
                monotone = False
            inv = new_inv
            cum_traded += abs(traded)
            if abs(inv) < 1e-9:
                inv = 0.0
        elif not math.isfinite(alpha):
            diverged = True
            break

    return {
        "trajectory": points,
        "final_inventory": inv if not diverged else math.nan,
        "cumulative_traded": cum_traded,
        "cap_hits": cap_hits,
        "sign_crossings": sign_crossings,
        "max_abs_rate": max_abs_rate,
        "monotone": monotone and not diverged,
        "diverged": diverged,
        "all_finite": all(
            math.isfinite(s["rate"]) and math.isfinite(s["inventory"]) for s in points
        ),
        "gamma_hat": g_hat,
        "solver": {
            "all_a_finite": sol["all_a_finite"],
            "all_c_finite": sol["all_c_finite"],
        },
    }


def _bail_out(sol: dict, q0: float, reason: str) -> dict:
    return {
        "trajectory": [],
        "final_inventory": math.nan,
        "cumulative_traded": 0.0,
        "cap_hits": 0,
        "sign_crossings": 0,
        "max_abs_rate": math.inf,
        "monotone": False,
        "diverged": True,
        "all_finite": False,
        "gamma_hat": sol.get("gamma_hat"),
        "bail_reason": reason,
        "solver": {
            "all_a_finite": sol.get("all_a_finite"),
            "all_c_finite": sol.get("all_c_finite"),
        },
    }
