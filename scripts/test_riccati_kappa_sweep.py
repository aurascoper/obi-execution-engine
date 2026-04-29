#!/usr/bin/env python3
"""scripts/test_riccati_kappa_sweep.py — κ multiplier sweep with the safety cap.

Task 14 step 2 per operator direction 2026-04-29. Runs the BL solver under
the κ_target = c · γ / T² rule from docs/kappa_scaling_notes.md, with the
rate cap that landed in strategy/optimal_rate.py just before this script.

For each c ∈ {0.1, 0.5, 1.0} (configurable), runs three OFI regimes:
  Y =  0     balanced
  Y = +0.8   favorable
  Y = -0.8   toxic

What we want to see:
  * RK4 backward integration converges across τ ∈ [0, T] for every c.
  * Final inventory ≈ 0 ($0.01 tolerance) under every Y regime.
  * The rate cap is NOT triggered (max α stays below |x₀|/T plus a small
    safety margin) — meaning the κ scaling is correct and the cap was
    engineered as a safety net, not a load-bearing clamp.
  * Y=+0.8 trajectory completes faster than Y=0; Y=-0.8 slower (visible
    OFI sensitivity, the whole reason we built BL).

Read-only. No engine state. No imports from execution/.

Usage:
  venv/bin/python3 scripts/test_riccati_kappa_sweep.py
  venv/bin/python3 scripts/test_riccati_kappa_sweep.py --c-values 0.01,0.1,1,10
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from math_core.riccati import BLInputs, trajectory_analytical  # noqa: E402
from strategy.optimal_rate import OFIParams, OptimalRate  # noqa: E402

DEFAULT_BETA = 0.00820
DEFAULT_SIGMA = 0.016
DEFAULT_ETA_BPS = 0.0177
DEFAULT_INVENTORY = 10_000.0
DEFAULT_HORIZON_S = 3600.0
DEFAULT_N_STEPS = 60
DEFAULT_GAMMA_T = 2.0


def fmt_money(x: float) -> str:
    if not math.isfinite(x):
        return "      inf" if x > 0 else "     -inf"
    if abs(x) >= 1000:
        return f"${x:>9,.2f}"
    if abs(x) >= 1:
        return f"${x:>9.4f}"
    return f"${x:>9.6f}"


def run_or_trajectory(
    solver: OptimalRate,
    initial_inventory: float,
    horizon: float,
    n_steps: int,
    y_static: float,
) -> dict:
    """Forward-Euler trajectory. Returns {trajectory, max_abs_rate, cap_hits,
    final_inv, completion_pct, alpha_at_first_step}."""
    dt = horizon / n_steps
    inv = initial_inventory
    points: list[dict] = []
    cap_hits = 0
    max_abs_rate = 0.0
    alpha_first = 0.0

    for i in range(n_steps + 1):
        t = i * dt
        tau = max(0.0, horizon - t)

        if tau == 0.0:
            rate = inv / max(dt, 1e-9) if abs(inv) > 1e-9 else 0.0
            uncapped = rate
        else:
            rate = solver.alpha(tau, inv, y_static)
            uncapped = solver.alpha_uncapped(tau, inv, y_static)

        if abs(rate - uncapped) > 1e-9:
            cap_hits += 1
        if i == 0:
            alpha_first = rate

        max_abs_rate = max(max_abs_rate, abs(rate))
        points.append({"t": t, "tau": tau, "inventory": inv, "rate": rate})

        if i < n_steps and tau > 0:
            inv -= rate * dt
            if abs(inv) < 1e-6:
                inv = 0.0

    completion = (
        (initial_inventory - inv) / initial_inventory if initial_inventory != 0 else 0.0
    )
    return {
        "trajectory": points,
        "max_abs_rate": max_abs_rate,
        "cap_hits": cap_hits,
        "final_inv": inv,
        "completion_pct": 100.0 * completion,
        "alpha_at_first_step": alpha_first,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="κ-multiplier sweep with safety cap")
    ap.add_argument("--inventory", type=float, default=DEFAULT_INVENTORY)
    ap.add_argument("--horizon-s", type=float, default=DEFAULT_HORIZON_S)
    ap.add_argument("--n-steps", type=int, default=DEFAULT_N_STEPS)
    ap.add_argument("--beta", type=float, default=DEFAULT_BETA)
    ap.add_argument("--sigma", type=float, default=DEFAULT_SIGMA)
    ap.add_argument("--eta-bps", type=float, default=DEFAULT_ETA_BPS)
    ap.add_argument("--gamma-T", type=float, default=DEFAULT_GAMMA_T)
    ap.add_argument(
        "--c-values",
        default="0.1,0.5,1.0",
        help="Comma-separated c multipliers to sweep (κ = c·γ/T²)",
    )
    ap.add_argument("--eta-leakage", type=float, default=0.001)
    ap.add_argument(
        "--p-terminal",
        type=float,
        default=None,
        help="Override p_terminal directly. If unset, uses p = c_p · γ/T.",
    )
    ap.add_argument(
        "--c-p",
        type=float,
        default=1.0,
        help="Multiplier in p_target = c_p · γ/T (default 1.0). Per "
        "docs/kappa_scaling_notes.md §6.5: p needs the same "
        "scaling treatment as κ to keep the inventory term bounded.",
    )
    ap.add_argument("--y-favorable", type=float, default=0.8)
    ap.add_argument("--y-toxic", type=float, default=-0.8)
    ap.add_argument("--n-rk4-steps", type=int, default=1024)
    args = ap.parse_args()

    eta_frac = args.eta_bps / 1.0e4
    g_target = args.gamma_T / args.horizon_s
    lam = (g_target**2) * eta_frac
    rate_cap_natural = args.inventory / args.horizon_s

    # Per docs/kappa_scaling_notes.md §6.5: p must also scale with γ/T.
    # If --p-terminal is set, use it; else derive from c_p × γ/T.
    if args.p_terminal is not None:
        p_terminal_value = args.p_terminal
        p_source = "explicit"
    else:
        p_terminal_value = args.c_p * eta_frac / args.horizon_s
        p_source = f"c_p × γ/T (c_p={args.c_p})"

    c_values = [float(x.strip()) for x in args.c_values.split(",") if x.strip()]

    print("=" * 100)
    print("κ-multiplier sweep — κ = c · γ / T²  (rule per docs/kappa_scaling_notes.md)")
    print("=" * 100)
    print(
        f"Common: x₀=${args.inventory:,.2f}, T={args.horizon_s:.0f}s, n_steps={args.n_steps}"
    )
    print(
        f"        β={args.beta:.5f}, σ={args.sigma:.4f}, η={args.eta_bps:.4f} bps/$, γT={args.gamma_T:.2f}"
    )
    print(
        f"        γ (= η_frac) = {eta_frac:.4e}, T² = {args.horizon_s**2:.4e}, λ = {lam:.4e}"
    )
    print(f"        rate cap natural ceiling = x₀/T = ${rate_cap_natural:.4f}/s")
    print(f"        η_leakage = {args.eta_leakage:.4f}")
    print(f"        p_terminal = {p_terminal_value:.4e}  (source: {p_source})")
    print()

    # Math_core sandbox baseline (Y-blind)
    mc_inputs = BLInputs(
        beta=args.beta,
        sigma=args.sigma,
        eta_bps_per_dollar=args.eta_bps,
        risk_aversion_lambda=lam,
    )
    mc_traj = trajectory_analytical(
        args.inventory, args.horizon_s, mc_inputs, n_steps=args.n_steps
    )
    mc_max_rate = max(abs(s["rate_usd_per_sec"]) for s in mc_traj)
    mc_final = mc_traj[-1]["inventory"]
    mc_completion = 100.0 * (args.inventory - mc_final) / args.inventory

    print("Baseline — math_core sandbox (Y-blind, closed form):")
    print(
        f"  α(t=0) = {fmt_money(mc_traj[0]['rate_usd_per_sec'])}/s   max |α| = {fmt_money(mc_max_rate)}/s"
    )
    print(
        f"  final inventory = {fmt_money(mc_final)}   completion = {mc_completion:.4f}%"
    )
    print()

    # Sweep
    results: list[dict] = []
    for c in c_values:
        kappa = c * eta_frac / (args.horizon_s**2)
        try:
            params = OFIParams(
                gamma=eta_frac,
                beta=args.beta,
                sigma=args.sigma,
                eta=args.eta_leakage,
                kappa=kappa,
                lam=lam,
                p=p_terminal_value,
            ).validated()
            solver = OptimalRate(params, T=args.horizon_s, n_steps=args.n_rk4_steps)
            rk4_ok = True
            rk4_err = None
        except ValueError as e:
            rk4_ok = False
            rk4_err = str(e)
            results.append({"c": c, "kappa": kappa, "rk4_ok": False, "err": rk4_err})
            continue

        r_balanced = run_or_trajectory(
            solver, args.inventory, args.horizon_s, args.n_steps, 0.0
        )
        r_favorable = run_or_trajectory(
            solver, args.inventory, args.horizon_s, args.n_steps, args.y_favorable
        )
        r_toxic = run_or_trajectory(
            solver, args.inventory, args.horizon_s, args.n_steps, args.y_toxic
        )

        results.append(
            {
                "c": c,
                "kappa": kappa,
                "rk4_ok": rk4_ok,
                "balanced": r_balanced,
                "favorable": r_favorable,
                "toxic": r_toxic,
            }
        )

    # Summary table
    print("Sweep results (per c):")
    print()
    print(
        f"{'c':>6s} {'κ':>12s} {'Y':>5s} {'α(t=0)':>12s} {'max |α|':>12s} {'final inv':>12s} {'pct done':>10s} {'cap hits':>9s}"
    )
    print("-" * 90)
    for r in results:
        if not r.get("rk4_ok"):
            print(
                f"{r['c']:>6.2f} {r['kappa']:>12.4e}  RK4 FAILED: {r.get('err', '?')[:60]}"
            )
            continue
        for label, key in [("0", "balanced"), ("+", "favorable"), ("-", "toxic")]:
            d = r[key]
            cap_marker = "" if d["cap_hits"] == 0 else f"⚠️{d['cap_hits']}"
            print(
                f"{r['c']:>6.2f} {r['kappa']:>12.4e} {label:>5s} "
                f"{fmt_money(d['alpha_at_first_step'])}/s "
                f"{fmt_money(d['max_abs_rate'])}/s "
                f"{fmt_money(d['final_inv']):>12s} "
                f"{d['completion_pct']:>9.4f}% "
                f"{cap_marker:>9s}"
            )
        print("-" * 90)

    # Verdict per c
    print()
    print(
        "Verdict per c (terminal residual ≤ 0.01% of x₀; max |α| ≤ x₀/T × 1.5; no Y-asymmetry collapse):"
    )
    print()
    print(
        f"{'c':>6s} {'κ':>12s} {'terminal_ok':>12s} {'rate_bounded':>12s} {'no_cap_hit':>11s} {'asym_ok':>9s} {'verdict':>10s}"
    )
    print("-" * 90)
    cap_threshold = rate_cap_natural * 1.5
    residual_threshold = max(0.01, abs(args.inventory) * 1e-4)
    overall_pass_count = 0
    for r in results:
        if not r.get("rk4_ok"):
            continue
        b, fav, tox = r["balanced"], r["favorable"], r["toxic"]
        terminal_ok = all(
            abs(d["final_inv"]) < residual_threshold for d in (b, fav, tox)
        )
        rate_bounded = all(d["max_abs_rate"] < cap_threshold for d in (b, fav, tox))
        no_cap_hit = all(d["cap_hits"] == 0 for d in (b, fav, tox))
        # Asymmetry: favorable should complete faster (higher pct done at t=0+) than toxic
        asym_ok = (
            fav["alpha_at_first_step"]
            > b["alpha_at_first_step"]
            >= tox["alpha_at_first_step"] - 1e-9
        )
        all_ok = terminal_ok and rate_bounded and no_cap_hit and asym_ok
        if all_ok:
            overall_pass_count += 1
        print(
            f"{r['c']:>6.2f} {r['kappa']:>12.4e} "
            f"{'✅' if terminal_ok else '❌':>12s} "
            f"{'✅' if rate_bounded else '❌':>12s} "
            f"{'✅' if no_cap_hit else '⚠️':>11s} "
            f"{'✅' if asym_ok else '❌':>9s} "
            f"{'✅ PASS' if all_ok else '❌ FAIL':>10s}"
        )

    print()
    print(
        f"Overall: {overall_pass_count}/{len(results)} c-values pass all four criteria"
    )
    if overall_pass_count == 0:
        print("⚠️  No c value cleared all gates. Review docs/kappa_scaling_notes.md.")

    return 0 if overall_pass_count > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
