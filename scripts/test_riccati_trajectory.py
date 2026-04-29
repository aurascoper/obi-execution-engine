#!/usr/bin/env python3
"""scripts/test_riccati_trajectory.py — Riccati sandbox driver.

Feeds the HIP-3 median (β, σ, η) parameters from tonight's calibration
(scripts/calibrate_bl_params.py --reference mid --taker-only) into the
math_core.riccati closed form and prints the optimal execution trajectory
for a $10,000 hypothetical order over a 60-minute horizon.

Read-only. No engine state. No imports from strategy/ or execution/.

Usage:
  venv/bin/python3 scripts/test_riccati_trajectory.py
  venv/bin/python3 scripts/test_riccati_trajectory.py --inventory 25000 --horizon-s 1800
  venv/bin/python3 scripts/test_riccati_trajectory.py --gamma-T 1.5    # set λ via target γT
  venv/bin/python3 scripts/test_riccati_trajectory.py --twap            # force λ=0
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from math_core.riccati import (  # noqa: E402
    BLInputs,
    gamma as gamma_fn,
    half_life_seconds,
    trajectory,
    trajectory_analytical,
)

# HIP-3 medians from calibrate_bl_params.py --reference mid --taker-only
# (commit eb65e17, 2026-04-29 ~04:50 UTC). β/σ are not used by the closed
# form but are passed through for parameter-doc purposes.
HIP3_MEDIAN_BETA = 0.00820  # 1/s  (half-life 84s on OBI)
HIP3_MEDIAN_SIGMA = 0.016
HIP3_MEDIAN_ETA_BPS_PER_DOLLAR = 0.0177  # bps/$, structural (mid-referenced)


def fmt_money(x: float) -> str:
    if abs(x) >= 1000:
        return f"${x:>10,.2f}"
    if abs(x) >= 1:
        return f"${x:>10.4f}"
    return f"${x:>10.6f}"


def fmt_sec(s: float | None) -> str:
    if s is None:
        return "—"
    if s < 60:
        return f"{s:.0f}s"
    if s < 3600:
        return f"{s / 60:.1f}m"
    return f"{s / 3600:.2f}h"


def derive_lambda_from_gamma_T(
    gamma_T_target: float, horizon_s: float, eta_bps: float
) -> float:
    """Pick λ such that γ·T = target, given η and T.

    γ = √(λ/η_frac)  ⇒  λ = (γ_T_target / T)² · η_frac
    """
    if gamma_T_target <= 0:
        return 0.0
    eta_frac = eta_bps / 1.0e4
    g_target = gamma_T_target / horizon_s
    return (g_target**2) * eta_frac


def main() -> int:
    ap = argparse.ArgumentParser(description="Riccati trajectory sandbox driver")
    ap.add_argument(
        "--inventory",
        type=float,
        default=10_000.0,
        help="Initial dollar inventory to liquidate (default $10,000)",
    )
    ap.add_argument(
        "--horizon-s",
        type=float,
        default=3600.0,
        help="Total execution horizon, seconds (default 3600 = 60 min)",
    )
    ap.add_argument(
        "--n-steps",
        type=int,
        default=60,
        help="Trajectory points (default 60 = one per minute)",
    )
    ap.add_argument(
        "--beta",
        type=float,
        default=HIP3_MEDIAN_BETA,
        help="OFI mean-reversion rate, 1/s (default = HIP-3 median)",
    )
    ap.add_argument(
        "--sigma",
        type=float,
        default=HIP3_MEDIAN_SIGMA,
        help="OFI driving-noise vol (default = HIP-3 median)",
    )
    ap.add_argument(
        "--eta-bps",
        type=float,
        default=HIP3_MEDIAN_ETA_BPS_PER_DOLLAR,
        help="Temp impact, bps per $ (default = HIP-3 mid-referenced taker median)",
    )
    ap.add_argument(
        "--gamma-T",
        type=float,
        default=2.0,
        help="Pick λ such that γ·T = this (default 2.0; higher = more front-loaded)",
    )
    ap.add_argument(
        "--lambda-direct",
        type=float,
        default=None,
        help="Specify λ directly; overrides --gamma-T",
    )
    ap.add_argument(
        "--twap", action="store_true", help="Force λ=0 (TWAP / linear decay)"
    )
    ap.add_argument(
        "--print-every",
        type=int,
        default=5,
        help="Print every Nth step (default 5; negative = print all)",
    )
    ap.add_argument(
        "--euler-vs-analytical",
        action="store_true",
        help="Compare forward-Euler integration to analytical sinh formula",
    )
    args = ap.parse_args()

    # Resolve λ
    if args.twap:
        lam = 0.0
    elif args.lambda_direct is not None:
        lam = args.lambda_direct
    else:
        lam = derive_lambda_from_gamma_T(args.gamma_T, args.horizon_s, args.eta_bps)

    inputs = BLInputs(
        beta=args.beta,
        sigma=args.sigma,
        eta_bps_per_dollar=args.eta_bps,
        risk_aversion_lambda=lam,
    )

    g = gamma_fn(inputs)
    hl = half_life_seconds(inputs)

    # Header
    print("=" * 78)
    print("Bechler-Ludkovski Riccati sandbox — optimal liquidation trajectory")
    print("=" * 78)
    print("Inputs (HIP-3 mid-referenced taker median, calibration commit eb65e17):")
    print(
        f"  β  (OU rate)           = {inputs.beta:.5f} /s    (half-life {fmt_sec(math.log(2) / inputs.beta) if inputs.beta > 0 else '—'})"
    )
    print(f"  σ  (OU vol)            = {inputs.sigma:.4f}")
    print(f"  η  (temp impact)       = {inputs.eta_bps_per_dollar:.4f} bps/$")
    print(f"  λ  (inventory risk)    = {inputs.risk_aversion_lambda:.4e}")
    print()
    print("Derived:")
    print(f"  γ = √(λ/η_frac)        = {g:.6e} /s")
    print(f"  γ·T                    = {g * args.horizon_s:.4f}")
    print(f"  half-life on inventory = {fmt_sec(hl)}")
    print()
    print("Order:")
    print(f"  initial inventory      = ${args.inventory:,.2f}")
    print(
        f"  horizon                = {fmt_sec(args.horizon_s)} ({args.horizon_s:.0f}s)"
    )
    print(f"  n_steps                = {args.n_steps}")
    print(
        f"  regime                 = {'TWAP (linear)' if g <= 0 else 'Almgren-Chriss closed form'}"
    )
    print()

    # Trajectory
    traj = trajectory_analytical(
        args.inventory, args.horizon_s, inputs, n_steps=args.n_steps
    )

    print(
        f"{'t':>8s}  {'t_rem':>8s}  {'inventory':>12s}  {'rate ($/s)':>12s}  {'rate ($/min)':>14s}  {'cum traded':>14s}  {'frac done':>10s}"
    )
    print("-" * 90)
    every = args.print_every if args.print_every > 0 else 1
    for i, step in enumerate(traj):
        if i % every != 0 and i != len(traj) - 1:
            continue
        frac = (
            (args.inventory - step["inventory"]) / args.inventory
            if args.inventory != 0
            else 0.0
        )
        print(
            f"{step['t']:>7.0f}s  "
            f"{step['t_remaining']:>7.0f}s  "
            f"{fmt_money(step['inventory'])}  "
            f"{fmt_money(step['rate_usd_per_sec'])}  "
            f"{fmt_money(step['rate_usd_per_sec'] * 60):>14s}  "
            f"{fmt_money(step['cumulative_traded']):>14s}  "
            f"{100 * frac:>8.1f}%"
        )

    print("-" * 90)
    final = traj[-1]
    print(f"end-of-horizon residual inventory: ${final['inventory']:.4f}")
    print(f"total traded:                       ${final['cumulative_traded']:,.2f}")
    print(
        f"completion:                         {100 * (args.inventory - final['inventory']) / args.inventory:.4f}%"
    )

    # Optional Euler-vs-analytical sanity check
    if args.euler_vs_analytical:
        print()
        print("=" * 78)
        print("Forward-Euler integration vs analytical sinh formula")
        print("=" * 78)
        traj_e = trajectory(
            args.inventory, args.horizon_s, inputs, n_steps=args.n_steps
        )
        max_diff = max(
            abs(a["inventory"] - e["inventory"]) for a, e in zip(traj, traj_e)
        )
        print(f"max |x_analytical − x_euler| = ${max_diff:.6f}")
        print("(small ⇒ closed form is internally consistent with the discrete update)")

    print()
    print("Notes:")
    print("- This is the deterministic mean path. Stochastic OBI fluctuations would")
    print("  perturb the rate via the (currently disabled) Y-feedback term.")
    print(
        "- λ here was synthesized to set γ·T = {gamma_T:.2f}; in production the operator".format(
            gamma_T=g * args.horizon_s
        )
    )
    print("  picks λ to match the desired front-loading aggressiveness.")
    print("- Never wired to _size_order or signals.py — this is sandbox only.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
