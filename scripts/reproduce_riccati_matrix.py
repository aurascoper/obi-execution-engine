#!/usr/bin/env python3
"""scripts/reproduce_riccati_matrix.py — Validation matrix driver.

Per operator direction 2026-04-29 (Paris): exercises the
math_core.regularized_riccati scheduler across the full validation grid
required for "candidate baseline" classification, and writes a
reproducible JSON artifact.

Grid (default):
    γ        ∈ {1e-8, 1e-7, 1e-6, 1e-5, 1e-4}
    φ_min    ∈ {1e-8, 1e-7, 1e-6, 1e-5, 1e-4}   (independent risk floor;
                                                  also used as ε)
    Y_static ∈ {-0.8, 0, +0.8}
    p        ∈ {0.1, 1.0}                       (terminal penalty)
    q₀       ∈ {10000.0}                        (single inventory; sweep more if needed)
    T        ∈ {3600.0}                         (single horizon; sweep more if needed)

Per-config criteria (all six required to "pass"):
    1. all rates finite
    2. monotone inventory decay (|q_t+1| ≤ |q_t| + ε)
    3. no sign crossing of inventory
    4. zero cap hits (uncapped policy stays naturally below |q|/τ)
    5. |q_T| ≤ pass_residual (default $1.00)
    6. OFI ordering: rate(Y=-0.8) ≥ rate(Y=0) ≥ rate(Y=+0.8) for liquidating
       (toxic Y demands faster execution; favorable Y waits)

Output:
    JSON written to logs/riccati_validation_matrix.json (gitignored by
    default; pass --commit-output to write to autoresearch_gated/ for
    durable storage). Also a stdout markdown summary.

Usage:
    venv/bin/python3 scripts/reproduce_riccati_matrix.py
    venv/bin/python3 scripts/reproduce_riccati_matrix.py --json autoresearch_gated/riccati_validation_matrix.json
    venv/bin/python3 scripts/reproduce_riccati_matrix.py --gammas 1e-6,1e-7
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import subprocess
import sys
from itertools import product
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from math_core.regularized_riccati import RegInputs, trajectory  # noqa: E402

# HIP-3 medians from calibration commit eb65e17 (carried for OU calibration use)
HIP3_BETA = 0.00820
HIP3_SIGMA = 0.016


def parse_floats(s: str) -> list[float]:
    return [float(x.strip()) for x in s.split(",") if x.strip()]


def evaluate_config(
    gamma: float,
    phi: float,
    epsilon: float,
    y_static: float,
    p: float,
    q0: float,
    T: float,
    n_steps: int,
    sigma: float,
    beta: float,
    kappa: float,
) -> dict:
    """Run one trajectory; return summary."""
    params = RegInputs(
        gamma=gamma,
        phi=phi,
        epsilon=epsilon,
        kappa=kappa,
        sigma=sigma,
        beta=beta,
        p=p,
    )
    out = trajectory(q0, T, n_steps, params, y_static=y_static, apply_cap=False)
    n_traj = len(out.get("trajectory", []))
    avg_rate = (
        sum(abs(s["rate"]) for s in out["trajectory"] if math.isfinite(s["rate"]))
        / n_traj
        if n_traj > 0
        else 0.0
    )
    return {
        "gamma": gamma,
        "phi": phi,
        "epsilon": epsilon,
        "gamma_hat": out.get("gamma_hat"),
        "y_static": y_static,
        "p": p,
        "q0": q0,
        "T": T,
        "n_steps": n_steps,
        "diverged": out.get("diverged", False),
        "all_finite": out.get("all_finite", False),
        "monotone": out.get("monotone", False),
        "sign_crossings": out.get("sign_crossings", 0),
        "cap_hits": out.get("cap_hits", -1),
        "max_abs_rate": (
            out.get("max_abs_rate")
            if math.isfinite(out.get("max_abs_rate", math.inf))
            else None
        ),
        "final_inventory": (
            out.get("final_inventory")
            if math.isfinite(out.get("final_inventory", math.nan))
            else None
        ),
        "cumulative_traded": out.get("cumulative_traded"),
        "avg_abs_rate": avg_rate if math.isfinite(avg_rate) else None,
    }


def grade_pair_ofi(results_for_pq: list[dict], pass_residual: float) -> dict:
    """Given the 3 Y-regime results for a (γ, φ, p) cell, compute pass criteria."""
    by_y = {r["y_static"]: r for r in results_for_pq}
    fav = by_y.get(0.8) or by_y.get(0.5) or {}
    bal = by_y.get(0.0) or {}
    tox = by_y.get(-0.8) or by_y.get(-0.5) or {}

    finite = all(r.get("all_finite") for r in results_for_pq)
    monotone = all(r.get("monotone") for r in results_for_pq)
    no_sign_cross = all((r.get("sign_crossings") or 0) == 0 for r in results_for_pq)
    no_cap_hits = all((r.get("cap_hits") or 0) == 0 for r in results_for_pq)
    final_residuals = [
        abs(r.get("final_inventory"))
        if r.get("final_inventory") is not None
        else math.inf
        for r in results_for_pq
    ]
    terminal_ok = all(rs <= pass_residual for rs in final_residuals)

    # OFI ordering: |α| should be ↑ as Y goes from favorable to toxic
    # (toxic Y means selling-into-asks is hurt → liquidate FASTER while we still can)
    def avg_or_inf(r):
        v = r.get("avg_abs_rate")
        return v if v is not None else math.inf

    ofi_ordered = (
        (
            avg_or_inf(tox) >= avg_or_inf(bal) >= avg_or_inf(fav)
            and (avg_or_inf(tox) - avg_or_inf(fav) > 1e-6)
        )
        if (fav and bal and tox)
        else False
    )

    all_pass = (
        finite
        and monotone
        and no_sign_cross
        and no_cap_hits
        and terminal_ok
        and ofi_ordered
    )

    return {
        "all_pass": all_pass,
        "finite": finite,
        "monotone": monotone,
        "no_sign_cross": no_sign_cross,
        "no_cap_hits": no_cap_hits,
        "terminal_ok": terminal_ok,
        "ofi_ordered": ofi_ordered,
        "max_residual": max(final_residuals) if final_residuals else math.inf,
        "y_results": {
            f"y={y_static:+.1f}": r
            for y_static, r in [
                (-0.8, tox),
                (0.0, bal),
                (0.8, fav),
            ]
            if r
        },
    }


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
    except Exception:
        return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser(description="Riccati validation matrix")
    ap.add_argument("--gammas", default="1e-8,1e-7,1e-6,1e-5,1e-4")
    ap.add_argument("--phis", default="1e-8,1e-7,1e-6,1e-5,1e-4")
    ap.add_argument("--y-vals", default="-0.8,0,0.8")
    ap.add_argument("--p-vals", default="0.1,1.0")
    ap.add_argument("--q0", type=float, default=10_000.0)
    ap.add_argument("--T", type=float, default=3600.0)
    ap.add_argument(
        "--n-steps",
        type=int,
        default=120,
        help="Trajectory grid points (default 120 = 30s/step at T=3600)",
    )
    ap.add_argument("--sigma", type=float, default=HIP3_SIGMA)
    ap.add_argument("--beta", type=float, default=HIP3_BETA)
    ap.add_argument("--kappa", type=float, default=0.0)
    ap.add_argument("--pass-residual", type=float, default=1.0)
    ap.add_argument(
        "--epsilon-mode",
        choices=("equals_phi", "fixed"),
        default="equals_phi",
        help="How ε is set relative to φ. equals_phi (default) sets ε=φ. "
        "fixed sets ε to --fixed-epsilon.",
    )
    ap.add_argument(
        "--fixed-epsilon",
        type=float,
        default=1e-6,
        help="Used when --epsilon-mode=fixed",
    )
    ap.add_argument("--json", default="logs/riccati_validation_matrix.json")
    ap.add_argument("--show-fails", type=int, default=10)
    args = ap.parse_args()

    gammas = parse_floats(args.gammas)
    phis = parse_floats(args.phis)
    y_vals = parse_floats(args.y_vals)
    p_vals = parse_floats(args.p_vals)

    n_cells = len(gammas) * len(phis) * len(p_vals)
    n_runs = n_cells * len(y_vals)

    print("# Riccati validation matrix")
    print("")
    print(f"git SHA: {_git_sha()}")
    print(f"timestamp: {dt.datetime.now(dt.timezone.utc).isoformat()}")
    print("")
    print(
        f"Grid: γ × φ × p × Y = {len(gammas)} × {len(phis)} × {len(p_vals)} × {len(y_vals)} = {n_runs} runs"
    )
    print(f"  γ values: {gammas}")
    print(f"  φ values: {phis}")
    print(f"  p values: {p_vals}")
    print(f"  Y values: {y_vals}")
    print(f"  q₀ = {args.q0}, T = {args.T}, n_steps = {args.n_steps}")
    print(f"  σ  = {args.sigma}, β = {args.beta}, κ = {args.kappa}")
    print(f"  ε mode: {args.epsilon_mode}", end="")
    if args.epsilon_mode == "fixed":
        print(f" (ε = {args.fixed_epsilon})")
    else:
        print(" (ε = φ)")
    print(f"  pass_residual: ${args.pass_residual:.4f}")
    print()

    cells: list[dict] = []
    n_pass_cells = 0
    n_diverged_cells = 0

    print(f"{'γ':>9s}  {'φ':>9s}  {'p':>5s}  pass  reason / first-fail")
    print("-" * 80)

    cell_index = 0
    for gamma, phi, p in product(gammas, phis, p_vals):
        cell_index += 1
        epsilon = phi if args.epsilon_mode == "equals_phi" else args.fixed_epsilon
        y_results = []
        for y in y_vals:
            r = evaluate_config(
                gamma=gamma,
                phi=phi,
                epsilon=epsilon,
                y_static=y,
                p=p,
                q0=args.q0,
                T=args.T,
                n_steps=args.n_steps,
                sigma=args.sigma,
                beta=args.beta,
                kappa=args.kappa,
            )
            y_results.append(r)

        grade = grade_pair_ofi(y_results, args.pass_residual)
        cell_record = {
            "cell_index": cell_index,
            "gamma": gamma,
            "phi": phi,
            "epsilon": epsilon,
            "p": p,
            "grade": {k: v for k, v in grade.items() if k != "y_results"},
            "max_residual": grade["max_residual"],
            "results": y_results,
        }
        cells.append(cell_record)

        if grade["all_pass"]:
            n_pass_cells += 1
            verdict = "PASS"
            extra = f"resid={grade['max_residual']:.4f}"
        else:
            failed = [
                k
                for k in (
                    "finite",
                    "monotone",
                    "no_sign_cross",
                    "no_cap_hits",
                    "terminal_ok",
                    "ofi_ordered",
                )
                if not grade.get(k)
            ]
            verdict = "FAIL"
            extra = f"missing: {','.join(failed)}; resid={grade['max_residual']:.2e}"
            if any(r["diverged"] for r in y_results):
                n_diverged_cells += 1

        print(f"{gamma:>9.1e}  {phi:>9.1e}  {p:>5.2f}  {verdict:<5s} {extra}")

    print()
    print("## Summary")
    print("")
    print(f"- total cells:    {n_cells}")
    print(f"- passing cells:  {n_pass_cells} / {n_cells}")
    print(f"- diverged cells: {n_diverged_cells}")
    print()

    # Write JSON artifact
    out_path = Path(args.json)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "git_sha": _git_sha(),
        "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "grid": {
            "gammas": gammas,
            "phis": phis,
            "p_vals": p_vals,
            "y_vals": y_vals,
            "q0": args.q0,
            "T": args.T,
            "n_steps": args.n_steps,
            "sigma": args.sigma,
            "beta": args.beta,
            "kappa": args.kappa,
            "epsilon_mode": args.epsilon_mode,
            "fixed_epsilon": args.fixed_epsilon
            if args.epsilon_mode == "fixed"
            else None,
            "pass_residual": args.pass_residual,
        },
        "summary": {
            "total_cells": n_cells,
            "passing_cells": n_pass_cells,
            "diverged_cells": n_diverged_cells,
        },
        "cells": cells,
    }
    out_path.write_text(json.dumps(payload, indent=2, default=str))
    print(f"JSON artifact: {out_path}")

    return 0 if n_pass_cells > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
