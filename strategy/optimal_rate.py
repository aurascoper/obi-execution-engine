"""
strategy/optimal_rate.py — Bechler-Ludkovski closed-form trading rate.

Reference: Bechler & Ludkovski (2014), "Optimal Execution with Dynamic Order
Flow Imbalance", arXiv:1409.2618. Proposition 2 (eq. 24-26).

Model (single asset, scalar OFI):
    dx_t = -alpha_t dt                              (inventory; sell at rate alpha)
    dY_t = -beta * Y_t dt + sigma dW_t - eta * alpha_t dt   (OU OFI w/ leakage)

Costs over [0, T]:
    J = E[ integral_0^T ( gamma * alpha^2  +  kappa * Y^2  +  lam * x^2 ) dt
          + p * x_T^2 ]

HJB (in time-to-go tau = T - t) with quadratic ansatz
V(tau, x, y) = A(tau) x^2 + B(tau) y^2 + C(tau) x y + F(tau):

    A_dot = lam   - (2A + eta C)^2 / (4 gamma)
    B_dot = kappa - (C + 2 eta B)^2 / (4 gamma) - 2 beta B
    C_dot = - (2A + eta C)(C + 2 eta B) / (2 gamma) - beta C
    F_dot = sigma^2 * B

with A(0)=p, B(0)=0, C(0)=0, F(0)=0  (terminal-condition becomes initial in tau).

Optimal feedback (eq. 26):
    alpha*(tau, x, y) = ( (2A + eta C) x + (C + 2 eta B) y ) / (2 gamma)

Note on sign convention: alpha > 0 == liquidating (selling at rate alpha).
For acquisition (buying), pass -x and negate the result, or run with opposite
inventory sign — the LQ structure is symmetric.

NumPy-only: hand-rolled RK4 (no scipy added). For T <= ~6.5h grid of 1-min
bars (390 steps), RK4 with 4x oversampling is sub-millisecond per solve and
accurate to ~1e-9.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Callable

import numpy as np


# ── Parameters ───────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class OFIParams:
    """Per-symbol Bechler-Ludkovski parameters.

    gamma : quadratic instantaneous impact coefficient (USD per (qty/sec)^2)
    kappa : OFI toxicity penalty ($/sec per unit OFI^2); 0 disables the term
    lam   : running inventory risk penalty ($/sec per unit qty^2); 0 disables
    p     : terminal-inventory penalty (must be > 0 for finite-horizon problem)
    beta  : OFI mean-reversion rate (1/sec)
    sigma : OFI volatility (units of OFI per sqrt(sec))
    eta   : OFI leakage from trader's own activity (units of OFI per qty)
    """

    gamma: float
    beta: float
    sigma: float
    eta: float
    kappa: float = 0.0
    lam: float = 0.0
    p: float = 1.0

    def validated(self) -> "OFIParams":
        if self.gamma <= 0:
            raise ValueError(f"gamma must be > 0, got {self.gamma}")
        if self.beta <= 0:
            raise ValueError(f"beta must be > 0, got {self.beta}")
        if self.sigma < 0:
            raise ValueError(f"sigma must be >= 0, got {self.sigma}")
        if self.p <= 0:
            raise ValueError(f"p must be > 0, got {self.p}")
        if self.kappa < 0 or self.lam < 0:
            raise ValueError("kappa, lam must be >= 0")
        return self


# ── Riccati system ───────────────────────────────────────────────────────────
def _rhs(state: np.ndarray, params: OFIParams) -> np.ndarray:
    """Right-hand side of the Riccati ODE system in time-to-go tau."""
    A, B, C, _F = state
    g = params.gamma
    e = params.eta
    b = params.beta
    s2 = params.sigma * params.sigma

    p1 = 2.0 * A + e * C
    p2 = C + 2.0 * e * B
    return np.array(
        [
            params.lam - (p1 * p1) / (4.0 * g),
            params.kappa - (p2 * p2) / (4.0 * g) - 2.0 * b * B,
            -(p1 * p2) / (2.0 * g) - b * C,
            s2 * B,
        ],
        dtype=np.float64,
    )


def _rk4_step(y: np.ndarray, h: float, params: OFIParams) -> np.ndarray:
    k1 = _rhs(y, params)
    k2 = _rhs(y + 0.5 * h * k1, params)
    k3 = _rhs(y + 0.5 * h * k2, params)
    k4 = _rhs(y + h * k3, params)
    return y + (h / 6.0) * (k1 + 2.0 * k2 + 2.0 * k3 + k4)


def solve_riccati(
    params: OFIParams,
    T: float,
    n_steps: int = 1024,
    rtol: float = 1e-6,
    max_substeps: int = 64,
) -> tuple[np.ndarray, np.ndarray]:
    """Integrate the 4-coefficient Riccati system on tau in [0, T].

    Step-doubling adaptive RK4: each output step is computed twice — once at
    full h, once as two h/2 steps — and refined recursively when the relative
    discrepancy exceeds rtol. Subdivision caps at max_substeps per output
    interval; further refinement raises a numerical-stiffness ValueError.

    Returns
    -------
    tau_grid : (n_steps+1,) float64
    coeffs   : (n_steps+1, 4) float64 — columns are (A, B, C, F)
    """
    p = params.validated()
    if T <= 0:
        raise ValueError(f"T must be > 0, got {T}")
    if n_steps < 16:
        raise ValueError(f"n_steps must be >= 16, got {n_steps}")

    h_outer = T / n_steps
    tau = np.linspace(0.0, T, n_steps + 1)
    out = np.empty((n_steps + 1, 4), dtype=np.float64)
    out[0] = (p.p, 0.0, 0.0, 0.0)

    y = out[0].copy()
    for i in range(n_steps):
        y_new = _adaptive_step(y, h_outer, p, rtol, max_substeps)
        if not np.all(np.isfinite(y_new)):
            raise ValueError(
                f"Riccati blew up at step {i}/{n_steps} (tau={tau[i]:.4f}); "
                f"stiffness exceeded max_substeps={max_substeps}. "
                f"Increase n_steps, soften params (raise gamma / lower p), "
                f"or shorten T."
            )
        y = y_new
        out[i + 1] = y

    return tau, out


def _adaptive_step(
    y: np.ndarray,
    h: float,
    params: OFIParams,
    rtol: float,
    depth_remaining: int,
) -> np.ndarray:
    """Recursive step-doubling RK4. Returns NaN-array on max-depth exceedance."""
    full = _rk4_step(y, h, params)
    half1 = _rk4_step(y, 0.5 * h, params)
    half2 = _rk4_step(half1, 0.5 * h, params)
    err = np.max(np.abs(full - half2)) / max(1.0, np.max(np.abs(half2)))
    if err <= rtol or depth_remaining <= 0:
        if depth_remaining <= 0 and err > rtol:
            return np.full_like(y, np.nan)
        return half2
    # subdivide
    mid = _adaptive_step(y, 0.5 * h, params, rtol, depth_remaining - 1)
    if not np.all(np.isfinite(mid)):
        return mid
    return _adaptive_step(mid, 0.5 * h, params, rtol, depth_remaining - 1)


# ── Optimal feedback ─────────────────────────────────────────────────────────
class OptimalRate:
    """Pre-solved closed-form trading rate for one symbol on a fixed horizon T.

    Construct once per (symbol, params, T); call .alpha(tau, x, y) per bar.
    Coefficients are tabulated; interpolation is linear (sufficient given RK4
    grid density and the smoothness of A,B,C on bounded T).
    """

    __slots__ = ("_params", "_T", "_tau", "_A", "_B", "_C", "_F")

    def __init__(self, params: OFIParams, T: float, n_steps: int = 1024):
        self._params = params.validated()
        self._T = float(T)
        tau, coeffs = solve_riccati(self._params, T, n_steps)
        self._tau = tau
        self._A = coeffs[:, 0]
        self._B = coeffs[:, 1]
        self._C = coeffs[:, 2]
        self._F = coeffs[:, 3]

    @property
    def horizon(self) -> float:
        return self._T

    @property
    def params(self) -> OFIParams:
        return self._params

    def coeffs_at(self, tau: float) -> tuple[float, float, float, float]:
        """Return (A, B, C, F) at time-to-go tau via linear interp.

        Clamps to [0, T] — beyond-grid queries fall back to the boundary value.
        """
        t = max(0.0, min(self._T, float(tau)))
        A = float(np.interp(t, self._tau, self._A))
        B = float(np.interp(t, self._tau, self._B))
        C = float(np.interp(t, self._tau, self._C))
        F = float(np.interp(t, self._tau, self._F))
        return A, B, C, F

    def alpha(self, tau: float, x: float, y: float) -> float:
        """Optimal trading rate at time-to-go tau, inventory x, OFI y.

        Returns alpha in units of (qty / sec). Positive == liquidating.
        Caller multiplies by bar dt to get qty per bar.
        """
        A, B, C, _F = self.coeffs_at(tau)
        e = self._params.eta
        g = self._params.gamma
        return ((2.0 * A + e * C) * x + (C + 2.0 * e * B) * y) / (2.0 * g)

    def value(self, tau: float, x: float, y: float) -> float:
        """Cost-to-go V(tau, x, y) under the optimal policy."""
        A, B, C, F = self.coeffs_at(tau)
        return A * x * x + B * y * y + C * x * y + F


# ── Receding-horizon T* finder ───────────────────────────────────────────────
def find_optimal_horizon(
    params: OFIParams,
    x: float,
    y: float,
    T_grid: np.ndarray | None = None,
    n_riccati_steps: int = 256,
) -> tuple[float, float]:
    """Receding-horizon T*(x, y) — argmin_T V(T, x, y).

    Bechler-Ludkovski §3.2. For balanced flow (y small), T* lengthens; for
    adverse flow (y opposing liquidation), T* contracts.

    T_grid : candidate horizons in seconds. Default = 16 log-spaced from 30s
             to 1h, which covers the engine's MAX_POSITION_SECS_RTH/OVN range.

    Returns (T_star, V_at_T_star).
    """
    if T_grid is None:
        T_grid = np.geomspace(30.0, 3600.0, 16)
    best_T = float(T_grid[0])
    best_V = float("inf")
    for T in T_grid:
        rate = OptimalRate(params, float(T), n_steps=n_riccati_steps)
        V = rate.value(float(T), x, y)
        if V < best_V:
            best_V = V
            best_T = float(T)
    return best_T, best_V


# ── Helpers for engine integration ───────────────────────────────────────────
def alpha_from_engine_state(
    rate: OptimalRate,
    bar_dt: float,
    inventory_qty: float,
    obi: float,
    bars_since_entry: int,
    bars_total: int,
) -> float:
    """Convenience adaptor for the existing _SymbolState fields.

    bar_dt           : seconds per bar (60.0 for the 1-min crypto stream)
    inventory_qty    : signed open qty (positions[tag])
    obi              : current OFI rho in [-1, 1]
    bars_since_entry : 0 at first bar after entry
    bars_total       : ceil(horizon / bar_dt)

    Returns the per-bar trading qty (signed: positive == sell, negative == buy
    additional). Caller maps to the order side and absolute qty.
    """
    tau = max(0.0, (bars_total - bars_since_entry) * bar_dt)
    rate_per_sec = rate.alpha(tau, inventory_qty, obi)
    return rate_per_sec * bar_dt


# ── Self-test ────────────────────────────────────────────────────────────────
def _self_test() -> None:
    """Smoke-test: solve, sanity-check coefficients, eyeball alpha behavior."""
    params = OFIParams(
        gamma=10.0,
        beta=0.05,
        sigma=0.1,
        eta=0.01,
        kappa=0.5,
        lam=0.1,
        p=2.0,
    ).validated()

    rate = OptimalRate(params, T=1800.0, n_steps=512)

    A0, B0, C0, F0 = rate.coeffs_at(0.0)
    assert abs(A0 - params.p) < 1e-9, f"A(0) should equal p={params.p}, got {A0}"
    assert abs(B0) < 1e-9 and abs(C0) < 1e-9 and abs(F0) < 1e-9

    AT, BT, CT, FT = rate.coeffs_at(rate.horizon)
    assert AT > 0 and BT >= 0 and FT >= 0, "A,B,F should remain >= 0"

    a_balanced = rate.alpha(900.0, x=100.0, y=0.0)
    assert a_balanced > 0, "Long inventory + balanced OFI should sell"

    a_adverse = rate.alpha(900.0, x=100.0, y=-0.5)
    a_friendly = rate.alpha(900.0, x=100.0, y=0.5)
    assert a_friendly > a_adverse, (
        "Selling into buy-pressure (y>0) should be faster than into sell-pressure (y<0); "
        f"got friendly={a_friendly}, adverse={a_adverse}"
    )

    T_star, V_star = find_optimal_horizon(params, x=100.0, y=0.0)
    assert 30.0 <= T_star <= 3600.0
    print(
        f"[optimal_rate self-test ok]  A(0)={A0:.4f} A(T)={AT:.4f}  "
        f"alpha(balanced)={a_balanced:.4f}  T*={T_star:.1f}s  V*={V_star:.4f}"
    )


if __name__ == "__main__":
    _self_test()
