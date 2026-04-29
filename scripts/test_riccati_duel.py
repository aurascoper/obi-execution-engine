#!/usr/bin/env python3
"""scripts/test_riccati_duel.py — Riccati Convergence Duel.

Side-by-side A/B test of two Riccati implementations:
  * math_core/riccati.py — closed-form Almgren-Chriss (unconditionally
    stable; sandbox; inventory-only)
  * strategy/optimal_rate.py — full Bechler-Ludkovski with OFI feedback;
    RK4 numerical integration of the 4-coefficient Riccati system

Same fundamental parameters fed to both. The BL solver is run under three
OFI regimes:
  Y =  0     balanced (no OFI pressure)
  Y = +0.8   favorable (heavy bid-side pressure when liquidating longs)
  Y = -0.8   toxic    (heavy ask-side pressure when liquidating longs)

Y is held STATIC across each trajectory as a stress test — the worst case
for the RK4 solver is sustained extreme Y; if α stays bounded and inventory
converges to ~0 at terminal, the solver is mathematically respecting the
boundary.

Read-only. No engine state. No imports from execution/.

Usage:
  venv/bin/python3 scripts/test_riccati_duel.py
  venv/bin/python3 scripts/test_riccati_duel.py --eta-leakage 0.005
  venv/bin/python3 scripts/test_riccati_duel.py --kappa 0.5
  venv/bin/python3 scripts/test_riccati_duel.py --gamma-T 3.0
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

# HIP-3 medians, mid-referenced taker (calibration commit eb65e17)
DEFAULT_BETA = 0.00820
DEFAULT_SIGMA = 0.016
DEFAULT_ETA_BPS = 0.0177
DEFAULT_INVENTORY = 10_000.0
DEFAULT_HORIZON_S = 3600.0
DEFAULT_N_STEPS = 60
DEFAULT_GAMMA_T = 2.0


def fmt_money(x: float) -> str:
    if not math.isfinite(x):
        return f"{'inf' if x > 0 else '-inf':>10s}"
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
) -> list[dict]:
    """Forward-Euler trajectory using strategy.optimal_rate.OptimalRate.

    Y is held constant at `y_static` throughout the trajectory. This is a
    deliberate stress test: the optimizer treats Y as a stochastic OU but
    we feed it the worst-case constant. Stability under static-extreme Y
    is sufficient (and stronger than) stability under realistic Y noise.
    """
    out: list[dict] = []
    dt = horizon / n_steps
    inv = initial_inventory
    cumulative_traded = 0.0

    for i in range(n_steps + 1):
        t = i * dt
        tau = horizon - t
        if tau < 0:
            tau = 0.0

        if tau == 0.0:
            # Terminal step: residual force-flush handled outside the solver
            rate = inv / max(dt, 1e-9) if abs(inv) > 1e-9 else 0.0
            regime = "terminal_flush"
        else:
            rate = solver.alpha(tau, inv, y_static)
            regime = "bl_rk4"

        out.append(
            {
                "t": t,
                "tau": tau,
                "inventory": inv,
                "rate": rate,
                "y_static": y_static,
                "regime": regime,
                "cumulative_traded": cumulative_traded,
            }
        )

        if i < n_steps and tau > 0:
            traded = rate * dt
            inv -= traded
            cumulative_traded += abs(traded)
            if abs(inv) < 1e-6:
                inv = 0.0

    return out


def stability_check(label: str, traj: list[dict], horizon: float) -> dict:
    """Compute stability metrics for one trajectory."""
    rates = [s["rate"] for s in traj]
    invs = [s["inventory"] for s in traj]
    finite_rates = all(math.isfinite(r) for r in rates)
    finite_invs = all(math.isfinite(i) for i in invs)
    max_abs_rate = max(abs(r) for r in rates if math.isfinite(r))
    terminal_inv = invs[-1]
    initial_inv = invs[0]
    pct_complete = (
        100.0 * (initial_inv - terminal_inv) / initial_inv if initial_inv != 0 else 0.0
    )
    rate_finite_at_t1 = math.isfinite(rates[1]) if len(rates) > 1 else True

    return {
        "label": label,
        "terminal_inventory": terminal_inv,
        "pct_complete": pct_complete,
        "max_abs_rate": max_abs_rate,
        "all_rates_finite": finite_rates,
        "all_inv_finite": finite_invs,
        "rate_finite_at_first_step": rate_finite_at_t1,
        "diverged": not (finite_rates and finite_invs),
        "respects_terminal": abs(terminal_inv) < max(0.01, abs(initial_inv) * 1e-4),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Riccati A/B duel — sandbox vs ghost RK4")
    ap.add_argument("--inventory", type=float, default=DEFAULT_INVENTORY)
    ap.add_argument("--horizon-s", type=float, default=DEFAULT_HORIZON_S)
    ap.add_argument("--n-steps", type=int, default=DEFAULT_N_STEPS)
    ap.add_argument("--beta", type=float, default=DEFAULT_BETA)
    ap.add_argument("--sigma", type=float, default=DEFAULT_SIGMA)
    ap.add_argument("--eta-bps", type=float, default=DEFAULT_ETA_BPS)
    ap.add_argument(
        "--gamma-T",
        type=float,
        default=DEFAULT_GAMMA_T,
        help="Pick λ so that γT = this; same λ used by both solvers",
    )
    ap.add_argument(
        "--eta-leakage",
        type=float,
        default=0.001,
        help="Self-leakage coef in optimal_rate (BL η in Y dynamics). "
        "Not calibrated from logs; stress-test parameter. 0 ⇒ Y has no "
        "effect on α (math_core ≡ optimal_rate at Y=0).",
    )
    ap.add_argument(
        "--kappa",
        type=float,
        default=0.0,
        help="OFI toxicity penalty (in cost integral). 0 = no Y² penalty.",
    )
    ap.add_argument(
        "--p-terminal",
        type=float,
        default=1.0,
        help="Terminal-inventory penalty p in optimal_rate.py. "
        "Larger = stronger forced-liquidation.",
    )
    ap.add_argument("--y-favorable", type=float, default=0.8)
    ap.add_argument("--y-toxic", type=float, default=-0.8)
    ap.add_argument("--print-every", type=int, default=5)
    ap.add_argument(
        "--n-rk4-steps",
        type=int,
        default=1024,
        help="RK4 grid density inside optimal_rate.solve_riccati",
    )
    args = ap.parse_args()

    # Derive λ such that γ·T = args.gamma_T in the math_core (η-frac) world
    eta_frac = args.eta_bps / 1.0e4
    g_target = args.gamma_T / args.horizon_s if args.horizon_s > 0 else 0.0
    lam = (g_target**2) * eta_frac

    # ── math_core sandbox ─────────────────────────────────────────────────
    mc_inputs = BLInputs(
        beta=args.beta,
        sigma=args.sigma,
        eta_bps_per_dollar=args.eta_bps,
        risk_aversion_lambda=lam,
    )
    mc_traj = trajectory_analytical(
        args.inventory, args.horizon_s, mc_inputs, n_steps=args.n_steps
    )

    # ── strategy/optimal_rate ghost ───────────────────────────────────────
    # Note: in optimal_rate.OFIParams, "gamma" is the impact coef (= our η_frac);
    # "eta" is the OFI self-leakage from trader's own activity (different from
    # our calibrated temp impact). "lam" is inventory risk; "kappa" is OFI
    # toxicity penalty; "p" is terminal-inventory penalty.
    or_params = OFIParams(
        gamma=eta_frac,
        beta=args.beta,
        sigma=args.sigma,
        eta=args.eta_leakage,
        kappa=args.kappa,
        lam=lam,
        p=args.p_terminal,
    ).validated()

    print("=" * 100)
    print(
        "Riccati Convergence Duel — math_core (closed-form) vs strategy/optimal_rate (RK4 BL)"
    )
    print("=" * 100)
    print("Common inputs:")
    print(f"  inventory                        = ${args.inventory:,.2f}")
    print(
        f"  horizon                          = {args.horizon_s:.0f}s ({args.horizon_s / 60:.1f} min)"
    )
    print(f"  n_steps (trajectory)             = {args.n_steps}")
    print(f"  β (OU rate)                      = {args.beta:.5f} /s")
    print(f"  σ (OU vol)                       = {args.sigma:.4f}")
    print(f"  η_temp_impact (bps/$)            = {args.eta_bps:.4f}")
    print(f"  γT target                        = {args.gamma_T:.2f}")
    print(f"  λ (derived)                      = {lam:.4e}")
    print()
    print("strategy/optimal_rate-only:")
    print(
        f"  η self-leakage                   = {args.eta_leakage:.4f}  (0 ⇒ Y has no effect on α)"
    )
    print(f"  κ (OFI toxicity penalty)         = {args.kappa:.4f}")
    print(f"  p (terminal penalty)             = {args.p_terminal:.4f}")
    print(f"  RK4 inner grid steps             = {args.n_rk4_steps}")
    print()

    # Build the BL solver (RK4 stable solve once; query alpha() at each τ)
    try:
        solver = OptimalRate(or_params, T=args.horizon_s, n_steps=args.n_rk4_steps)
    except ValueError as e:
        print(f"⚠️  optimal_rate.solve_riccati FAILED: {e}")
        print("⚠️  RK4 diverged before trajectory simulation could begin.")
        return 1
    print("✅  RK4 backward integration converged across τ ∈ [0, T]")

    # Verify boundary: A(0)=p, B(0)=0, C(0)=0, F(0)=0
    A0, B0, C0, F0 = solver.coeffs_at(0.0)
    print(f"   τ=0 boundary check:  A={A0:.6e}  B={B0:.6e}  C={C0:.6e}  F={F0:.6e}")
    boundary_ok = (
        abs(A0 - args.p_terminal) < 1e-9
        and abs(B0) < 1e-9
        and abs(C0) < 1e-9
        and abs(F0) < 1e-9
    )
    print(f"   τ=0 boundary respects A(0)=p, B=C=F=0: {'✅' if boundary_ok else '❌'}")
    print()

    # ── Run the three OFI regimes ─────────────────────────────────────────
    or_y0 = run_or_trajectory(solver, args.inventory, args.horizon_s, args.n_steps, 0.0)
    or_yp = run_or_trajectory(
        solver, args.inventory, args.horizon_s, args.n_steps, args.y_favorable
    )
    or_yn = run_or_trajectory(
        solver, args.inventory, args.horizon_s, args.n_steps, args.y_toxic
    )

    # ── Side-by-side trajectory print ─────────────────────────────────────
    print("=" * 130)
    print("Trajectory (every {}th step):".format(args.print_every))
    print("-" * 130)
    print(
        f"{'t':>6s} {'τ':>6s}  | "
        f"{'mc_inv':>10s} {'or_y=0':>10s} {'or_y=+':>10s} {'or_y=-':>10s} | "
        f"{'mc_rate':>10s} {'or_y=0_r':>10s} {'or_y=+_r':>10s} {'or_y=-_r':>10s}"
    )
    print("-" * 130)
    every = max(1, args.print_every)
    for i in range(len(mc_traj)):
        if i % every != 0 and i != len(mc_traj) - 1:
            continue
        mc = mc_traj[i]
        a = or_y0[i]
        b = or_yp[i]
        c = or_yn[i]
        print(
            f"{mc['t']:>5.0f}s {mc['t_remaining']:>5.0f}s  | "
            f"{fmt_money(mc['inventory'])} {fmt_money(a['inventory'])} "
            f"{fmt_money(b['inventory'])} {fmt_money(c['inventory'])} | "
            f"{fmt_money(mc['rate_usd_per_sec'])} {fmt_money(a['rate'])} "
            f"{fmt_money(b['rate'])} {fmt_money(c['rate'])}"
        )

    print("-" * 130)
    print()

    # ── Stability checks ──────────────────────────────────────────────────
    sc_mc = stability_check(
        "math_core (closed-form)",
        [{"inventory": s["inventory"], "rate": s["rate_usd_per_sec"]} for s in mc_traj],
        args.horizon_s,
    )
    sc_y0 = stability_check(
        "optimal_rate Y=0 (balanced)",
        [{"inventory": s["inventory"], "rate": s["rate"]} for s in or_y0],
        args.horizon_s,
    )
    sc_yp = stability_check(
        f"optimal_rate Y=+{args.y_favorable} (favorable)",
        [{"inventory": s["inventory"], "rate": s["rate"]} for s in or_yp],
        args.horizon_s,
    )
    sc_yn = stability_check(
        f"optimal_rate Y={args.y_toxic} (toxic)",
        [{"inventory": s["inventory"], "rate": s["rate"]} for s in or_yn],
        args.horizon_s,
    )

    print("=" * 100)
    print("Stability verdict")
    print("=" * 100)
    print(
        f"{'variant':<42s} {'pct done':>10s} {'residual':>14s} {'max |rate|':>14s} {'finite?':>9s} {'terminal?':>11s}"
    )
    print("-" * 100)
    for sc in (sc_mc, sc_y0, sc_yp, sc_yn):
        print(
            f"{sc['label']:<42s} "
            f"{sc['pct_complete']:>9.4f}% "
            f"{fmt_money(sc['terminal_inventory']):>14s} "
            f"{fmt_money(sc['max_abs_rate']):>14s} "
            f"{('✅' if sc['all_rates_finite'] else '❌ DIVERGED'):>9s} "
            f"{('✅' if sc['respects_terminal'] else '❌'):>11s}"
        )

    overall_ok = all(
        s["all_rates_finite"] and s["respects_terminal"]
        for s in (sc_mc, sc_y0, sc_yp, sc_yn)
    )
    print()
    print(
        f"OVERALL: {'✅  All four trajectories are bounded and converge to ≈0 at T' if overall_ok else '❌  At least one trajectory diverged or missed the boundary'}"
    )

    if overall_ok:
        # Show that Y monotonicity holds: favorable Y completes faster than balanced; toxic Y slower.
        # Compare cumulative completion at midpoint.
        mid = args.n_steps // 2
        mc_done = (args.inventory - mc_traj[mid]["inventory"]) / args.inventory
        y0_done = (args.inventory - or_y0[mid]["inventory"]) / args.inventory
        yp_done = (args.inventory - or_yp[mid]["inventory"]) / args.inventory
        yn_done = (args.inventory - or_yn[mid]["inventory"]) / args.inventory
        print()
        print(f"At midpoint t={args.horizon_s / 2:.0f}s, fraction completed:")
        print(f"  math_core (closed-form):  {100 * mc_done:>6.2f}%")
        print(f"  optimal_rate Y=0:         {100 * y0_done:>6.2f}%")
        print(
            f"  optimal_rate Y=+{args.y_favorable}:      {100 * yp_done:>6.2f}%  (should ≥ Y=0 if favorable)"
        )
        print(
            f"  optimal_rate Y={args.y_toxic}:      {100 * yn_done:>6.2f}%  (should ≤ Y=0 if toxic)"
        )
        monotone = yn_done <= y0_done <= yp_done
        print(
            f"  Monotonicity (toxic ≤ balanced ≤ favorable): {'✅' if monotone else '⚠️  not strictly monotone — review BL sign convention'}"
        )

    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
