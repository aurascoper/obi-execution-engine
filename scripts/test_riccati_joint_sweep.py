#!/usr/bin/env python3
"""scripts/test_riccati_joint_sweep.py — Joint (c_p, c_κ, c_λ) parameter sweep.

Task 15 per operator direction 2026-04-29 morning. The κ-only sweep proved
that scaling κ alone is insufficient (terminal-penalty p still blows up the
inventory feedback term, and incorrect λ leaves residual). This script
sweeps all THREE coefficients jointly using the dimensionally-consistent
rules derived in docs/kappa_scaling_notes.md plus the operator's λ
correction:

    p = c_p · γ / T          (terminal-penalty scaling)
    κ = c_κ · γ / T²         (Y-feedback boundedness)
    λ = c_λ · γ / T³         (running risk-aversion; AC-style)

Sweep grid (default):
    c_p     ∈ [1.0, 10.0, 100.0]
    c_κ     ∈ [0.1, 1.0, 10.0]
    c_λ     ∈ [1.0, 10.0, 100.0]

That's 27 (c_p, c_κ, c_λ) triplets × 3 Y regimes = 81 BL solves.

Pass criteria per triplet (all four must hold):
    1. ZERO CAP HITS — α stays naturally below |x|/τ across all 3 Y regimes
    2. ZERO RESIDUAL — final inventory |x_T| ≤ $1.00
    3. Y-ASYMMETRY  — max(Y=+0.8) > max(Y=0) > max(Y=-0.8) for the
                       midpoint inventory completion (proves OFI feedback
                       is doing real work, not collapsed to a constant)
    4. RK4 STABLE   — solve_riccati returns finite (A, B, C, F) across τ

Read-only. No engine state. No imports from execution/.

Usage:
  venv/bin/python3 scripts/test_riccati_joint_sweep.py
  venv/bin/python3 scripts/test_riccati_joint_sweep.py --c-p 0.1,1,10,100,1000
"""

from __future__ import annotations

import argparse
import math
import sys
import warnings
from itertools import product
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from strategy.optimal_rate import OFIParams, OptimalRate  # noqa: E402

# Suppress numpy overflow warnings during the sweep — diverging configs
# trigger them, but the sweep is designed to identify those configs by
# their failure to terminate cleanly.
warnings.filterwarnings("ignore", category=RuntimeWarning)


DEFAULT_BETA = 0.00820
DEFAULT_SIGMA = 0.016
DEFAULT_ETA_BPS = 0.0177
DEFAULT_INVENTORY = 10_000.0
DEFAULT_HORIZON_S = 3600.0
DEFAULT_N_STEPS = 60


def fmt_money(x: float) -> str:
    if not math.isfinite(x):
        return f"{'+inf' if x > 0 else '-inf':>10s}"
    if abs(x) >= 10000:
        return f"${x:>9,.0f}"
    if abs(x) >= 100:
        return f"${x:>9.2f}"
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
    dt = horizon / n_steps
    inv = initial_inventory
    cap_hits = 0
    max_abs_rate = 0.0
    inv_at_mid = None
    rate_at_mid = None
    mid_step = n_steps // 2

    for i in range(n_steps + 1):
        t = i * dt
        tau = max(0.0, horizon - t)

        if tau == 0.0:
            rate = inv / max(dt, 1e-9) if abs(inv) > 1e-9 else 0.0
            uncapped = rate
        else:
            try:
                rate = solver.alpha(tau, inv, y_static)
                uncapped = solver.alpha_uncapped(tau, inv, y_static)
            except Exception:
                return {
                    "diverged": True,
                    "cap_hits": -1,
                    "max_abs_rate": float("inf"),
                    "final_inv": float("nan"),
                    "completion_pct": 0.0,
                    "inv_at_mid": None,
                    "rate_at_mid": None,
                }

        if not math.isfinite(rate):
            return {
                "diverged": True,
                "cap_hits": -1,
                "max_abs_rate": float("inf"),
                "final_inv": inv,
                "completion_pct": 0.0,
                "inv_at_mid": inv_at_mid,
                "rate_at_mid": rate_at_mid,
            }

        if abs(rate - uncapped) > 1e-9:
            cap_hits += 1

        if i == mid_step:
            inv_at_mid = inv
            rate_at_mid = rate

        max_abs_rate = max(max_abs_rate, abs(rate))

        if i < n_steps and tau > 0:
            inv -= rate * dt
            if abs(inv) < 1e-6:
                inv = 0.0

    return {
        "diverged": False,
        "cap_hits": cap_hits,
        "max_abs_rate": max_abs_rate,
        "final_inv": inv,
        "completion_pct": 100.0 * (initial_inventory - inv) / initial_inventory
        if initial_inventory != 0
        else 0.0,
        "inv_at_mid": inv_at_mid,
        "rate_at_mid": rate_at_mid,
    }


def evaluate_triplet(
    c_p: float,
    c_kappa: float,
    c_lambda: float,
    args,
    eta_frac: float,
) -> dict:
    """Run one (c_p, c_κ, c_λ) configuration across 3 Y regimes."""
    T = args.horizon_s
    p = c_p * eta_frac / T
    kappa = c_kappa * eta_frac / (T**2)
    lam = c_lambda * eta_frac / (T**3)

    out: dict = {
        "c_p": c_p,
        "c_kappa": c_kappa,
        "c_lambda": c_lambda,
        "p": p,
        "kappa": kappa,
        "lam": lam,
    }

    try:
        params = OFIParams(
            gamma=eta_frac,
            beta=args.beta,
            sigma=args.sigma,
            eta=args.eta_leakage,
            kappa=kappa,
            lam=lam,
            p=p,
        ).validated()
        solver = OptimalRate(params, T=T, n_steps=args.n_rk4_steps)
        out["rk4_ok"] = True
    except (ValueError, RuntimeError) as e:
        out["rk4_ok"] = False
        out["err"] = str(e)[:100]
        return out

    runs = {}
    for label, y in [
        ("balanced", 0.0),
        ("favorable", args.y_favorable),
        ("toxic", args.y_toxic),
    ]:
        runs[label] = run_or_trajectory(solver, args.inventory, T, args.n_steps, y)
    out["runs"] = runs

    # Aggregate verdict
    diverged = any(r["diverged"] for r in runs.values())
    if diverged:
        out["verdict"] = "diverged"
        return out

    max_residual = max(abs(r["final_inv"]) for r in runs.values())
    max_rate = max(r["max_abs_rate"] for r in runs.values())
    total_cap_hits = sum(r["cap_hits"] for r in runs.values())

    # Y-asymmetry: midpoint completion ordering
    fav_done = (
        (args.inventory - runs["favorable"]["inv_at_mid"])
        if runs["favorable"]["inv_at_mid"] is not None
        else 0
    )
    bal_done = (
        (args.inventory - runs["balanced"]["inv_at_mid"])
        if runs["balanced"]["inv_at_mid"] is not None
        else 0
    )
    tox_done = (
        (args.inventory - runs["toxic"]["inv_at_mid"])
        if runs["toxic"]["inv_at_mid"] is not None
        else 0
    )
    asymmetry_spread = fav_done - tox_done

    terminal_ok = max_residual <= 1.00
    no_cap_hits = total_cap_hits == 0
    asymmetry_ok = (fav_done > bal_done > tox_done) and (asymmetry_spread > 1.0)
    rate_bounded = (
        max_rate < args.inventory
    )  # absurd-bound: never trade more than inventory in 1 second

    out.update(
        {
            "max_residual": max_residual,
            "max_rate": max_rate,
            "total_cap_hits": total_cap_hits,
            "asymmetry_spread": asymmetry_spread,
            "fav_mid_done": fav_done,
            "bal_mid_done": bal_done,
            "tox_mid_done": tox_done,
            "terminal_ok": terminal_ok,
            "no_cap_hits": no_cap_hits,
            "asymmetry_ok": asymmetry_ok,
            "rate_bounded": rate_bounded,
            "all_pass": terminal_ok and no_cap_hits and asymmetry_ok and rate_bounded,
        }
    )
    if out["all_pass"]:
        out["verdict"] = "PASS"
    elif diverged:
        out["verdict"] = "diverged"
    else:
        out["verdict"] = "fail"
    return out


def parse_floats(s: str) -> list[float]:
    return [float(x.strip()) for x in s.split(",") if x.strip()]


def main() -> int:
    ap = argparse.ArgumentParser(description="Joint (c_p, c_κ, c_λ) sweep")
    ap.add_argument("--inventory", type=float, default=DEFAULT_INVENTORY)
    ap.add_argument("--horizon-s", type=float, default=DEFAULT_HORIZON_S)
    ap.add_argument("--n-steps", type=int, default=DEFAULT_N_STEPS)
    ap.add_argument("--beta", type=float, default=DEFAULT_BETA)
    ap.add_argument("--sigma", type=float, default=DEFAULT_SIGMA)
    ap.add_argument("--eta-bps", type=float, default=DEFAULT_ETA_BPS)
    ap.add_argument("--eta-leakage", type=float, default=0.001)
    ap.add_argument(
        "--c-p", default="1.0,10.0,100.0", help="comma-separated c_p values"
    )
    ap.add_argument(
        "--c-kappa", default="0.1,1.0,10.0", help="comma-separated c_κ values"
    )
    ap.add_argument(
        "--c-lambda", default="1.0,10.0,100.0", help="comma-separated c_λ values"
    )
    ap.add_argument("--y-favorable", type=float, default=0.8)
    ap.add_argument("--y-toxic", type=float, default=-0.8)
    ap.add_argument("--n-rk4-steps", type=int, default=512)
    ap.add_argument("--show-top", type=int, default=10)
    args = ap.parse_args()

    eta_frac = args.eta_bps / 1.0e4
    cps = parse_floats(args.c_p)
    cks = parse_floats(args.c_kappa)
    cls = parse_floats(args.c_lambda)
    n_triplets = len(cps) * len(cks) * len(cls)

    print("=" * 110)
    print("Joint (c_p, c_κ, c_λ) sweep — task 15")
    print("=" * 110)
    print(
        f"Common: x₀=${args.inventory:,.2f}, T={args.horizon_s:.0f}s, n_steps={args.n_steps}"
    )
    print(f"        β={args.beta:.5f}, σ={args.sigma:.4f}, η={args.eta_bps:.4f} bps/$")
    print(f"        γ (= η_frac) = {eta_frac:.4e}")
    print(f"        η_leakage    = {args.eta_leakage:.4f}")
    print()
    print("Scaling rules:")
    print(f"        p = c_p · γ/T  =  c_p × {eta_frac / args.horizon_s:.4e}")
    print(f"        κ = c_κ · γ/T² =  c_κ × {eta_frac / args.horizon_s**2:.4e}")
    print(f"        λ = c_λ · γ/T³ =  c_λ × {eta_frac / args.horizon_s**3:.4e}")
    print()
    print(f"Grid:    c_p     ∈ {cps}")
    print(f"         c_κ     ∈ {cks}")
    print(f"         c_λ     ∈ {cls}")
    print(f"Total:   {n_triplets} triplets × 3 Y regimes = {n_triplets * 3} BL solves")
    print()

    # ── Run sweep ─────────────────────────────────────────────────────────
    results: list[dict] = []
    for i, (c_p, c_k, c_l) in enumerate(product(cps, cks, cls), start=1):
        print(
            f"  [{i:>2}/{n_triplets}] c_p={c_p:>6.1f} c_κ={c_k:>6.2f} c_λ={c_l:>7.2f}  ",
            end="",
            flush=True,
        )
        r = evaluate_triplet(c_p, c_k, c_l, args, eta_frac)
        results.append(r)
        if r.get("verdict") == "PASS":
            print(
                f"✅ PASS   resid={fmt_money(r['max_residual'])}  asym={r['asymmetry_spread']:.2f}"
            )
        elif r.get("verdict") == "diverged":
            print("❌ DIVERGED")
        else:
            t_ok = "✓" if r.get("terminal_ok") else "✗"
            c_ok = "✓" if r.get("no_cap_hits") else "✗"
            a_ok = "✓" if r.get("asymmetry_ok") else "✗"
            print(
                f"  fail [term:{t_ok} cap:{c_ok} asym:{a_ok}]  resid={fmt_money(r.get('max_residual', float('nan')))}  cap_hits={r.get('total_cap_hits', '?')}"
            )

    # ── Verdict tally ─────────────────────────────────────────────────────
    print()
    print("=" * 110)
    pass_set = [r for r in results if r.get("verdict") == "PASS"]
    print(f"Passing configurations: {len(pass_set)} / {n_triplets}")
    print("=" * 110)

    if pass_set:
        # Rank by asymmetry spread (more Y differentiation = better)
        pass_set.sort(key=lambda r: -r["asymmetry_spread"])
        print(
            f"\nTop {min(args.show_top, len(pass_set))} passing configs (ranked by asymmetry spread):"
        )
        print()
        print(
            f"{'rank':>4s} {'c_p':>6s} {'c_κ':>6s} {'c_λ':>7s}   "
            f"{'p':>10s} {'κ':>10s} {'λ':>10s}  "
            f"{'residual':>10s} {'asym':>8s} {'fav_mid':>10s} {'tox_mid':>10s}"
        )
        print("-" * 110)
        for rank, r in enumerate(pass_set[: args.show_top], start=1):
            print(
                f"{rank:>4d} {r['c_p']:>6.1f} {r['c_kappa']:>6.2f} {r['c_lambda']:>7.2f}   "
                f"{r['p']:>10.3e} {r['kappa']:>10.3e} {r['lam']:>10.3e}  "
                f"{fmt_money(r['max_residual']):>10s} {r['asymmetry_spread']:>7.2f} "
                f"{fmt_money(r['fav_mid_done'])} {fmt_money(r['tox_mid_done'])}"
            )
    else:
        print()
        print("⚠️  No triplet passed all four criteria.")
        print()
        print("Closest near-passes (configs that hit ≥3 of 4 criteria):")
        scored = [
            (
                r,
                sum(
                    [
                        r.get("terminal_ok", False),
                        r.get("no_cap_hits", False),
                        r.get("asymmetry_ok", False),
                        r.get("rate_bounded", False),
                    ]
                ),
            )
            for r in results
            if r.get("verdict") not in ("diverged",)
        ]
        scored.sort(key=lambda x: (-x[1], abs(x[0].get("max_residual", float("inf")))))
        print()
        print(
            f"{'score':>5s} {'c_p':>6s} {'c_κ':>6s} {'c_λ':>7s}   "
            f"{'residual':>10s} {'cap_hits':>8s} {'asym':>8s} {'verdict':>10s}"
        )
        print("-" * 90)
        for r, score in scored[: args.show_top]:
            t_ok = "✓" if r.get("terminal_ok") else "✗"
            c_ok = "✓" if r.get("no_cap_hits") else "✗"
            a_ok = "✓" if r.get("asymmetry_ok") else "✗"
            print(
                f"{score:>5d}/4 {r['c_p']:>5.1f} {r['c_kappa']:>5.2f} {r['c_lambda']:>6.2f}   "
                f"{fmt_money(r.get('max_residual', float('nan'))):>10s} "
                f"{r.get('total_cap_hits', '?'):>8} "
                f"{r.get('asymmetry_spread', 0.0):>7.2f}  "
                f"[t:{t_ok} c:{c_ok} a:{a_ok}]"
            )

    return 0 if pass_set else 1


if __name__ == "__main__":
    sys.exit(main())
