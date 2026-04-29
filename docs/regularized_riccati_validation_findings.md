# Regularized Riccati Validation — Honest Findings

**Status:** FALSIFICATION RESULT — the proposed γ-regularization scheme
does not clear the operator's six-criterion validation matrix.
**Run:** `scripts/reproduce_riccati_matrix.py` against
`math_core/regularized_riccati.py` at branch SHA `1fa85c6` (pre-this-commit).
**Artifact:** `autoresearch_gated/riccati_validation_matrix.json`
**Date:** 2026-04-29

## TL;DR

Operator hypothesis: a γ-INDEPENDENT inventory-risk floor φ in the cost
integral, combined with γ̂ = max(γ, ε), restores bounded, monotone,
terminally-clean liquidation at γ ≈ 10⁻⁶ with visible OFI asymmetry.

Empirical result on the spec'd 5×5×2×3 = 150-run matrix: **0/50 cells pass**.
38/50 cells produce numerical divergence (residual 10¹² to 10¹⁷⁹). The
remaining 12 fail on `monotone` and/or `ofi_ordered`.

This is a **successful falsification** — the scheme as specified is not
sufficient. The math reveals why, and points at what the next iteration
must structurally change.

## What was tested

The objective:

```
J = ∫₀ᵀ ( γ α² + φ q² + κ Y² ) dt + p · q_T²
```

with the BL-derived ODE system reduced to (η_leak ≡ 0, B absorbed):

```
A'(τ) = φ − A²/γ̂                           A(0) = p
C'(τ) = (σ²·A/γ̂) − (κ + A/γ̂)·C             C(0) = 0
α*(τ, q, Y) = (2·A·q + C·Y) / (2γ̂)
```

with `γ̂ = max(γ, ε)`, `ε = φ` in the default sweep.

Grid:
- `γ ∈ {1e-8, 1e-7, 1e-6, 1e-5, 1e-4}`
- `φ ∈ {1e-8, 1e-7, 1e-6, 1e-5, 1e-4}`
- `Y ∈ {-0.8, 0, +0.8}`
- `p ∈ {0.1, 1.0}`
- `q₀ = $10,000`, `T = 3600s`, `n_steps = 120`
- `σ = 0.016`, `β = 0.0082`, `κ = 0`

Pass criteria (six, all required):
1. all rates finite
2. monotone inventory decay
3. no sign crossing
4. zero cap hits (uncapped policy stays naturally below |q|/τ)
5. |q_T| ≤ $1.00
6. OFI ordering: `rate(Y=-0.8) ≥ rate(Y=0) ≥ rate(Y=+0.8)`

## Why it fails — diagnosis

### Failure mode 1: numerical divergence (38/50 cells)

In the regime where `γ ≪ φ` (regularization active, `γ̂ = φ`) AND `σ > 0`,
the C ODE has source term `σ²·A/γ̂` and decay rate `(κ + A/γ̂)`. With
`A ≈ A_eq = √(φγ̂) = φ` (since γ̂ = φ), source is `σ²` and decay is `1`.
Equilibrium `C_eq ≈ σ² ≈ 2.6×10⁻⁴`.

Then α's Y-component: `C·Y / (2γ̂) ≈ σ²·Y / (2φ)`. For φ = 10⁻⁴ and Y = 0.8:
`(2.6×10⁻⁴ × 0.8) / (2×10⁻⁴) ≈ 1.04 per dollar of inventory per second`.

That's $10,400/s on a $10,000 inventory in the very first timestep —
inventory crosses zero immediately, then α's q-component flips sign,
and the system runs away.

**Confirmed empirically:** even with `σ = 0` (OFI source killed),
divergence still happens for `γ < 1e-4` because the inventory term
`A·q/γ̂` alone is unbounded. With `A = p = 1`, `γ̂ = 1e-6`: α at t=0 =
`1 × $10,000 / 1e-6 = $10 billion/s`.

### Failure mode 2: "completes-but-fails" (8 cells)

In the regime `γ ≥ 1e-4` (γ̂ = γ, regularization inactive), the trajectory
completes terminally (residual ≈ $0) but:
- Cap is hit at every step (the natural policy demands rate > |q|/τ)
- No Y-asymmetry visible (the cap collapses any Y-contribution to a
  constant `|q|/τ` ceiling)

This matches what was already observed in the prior κ-only and joint
(c_p, c_κ, c_λ) sweeps. The cap is doing the work, not the math.

### Why the scheme can't work — structural argument

The α formula `α* = (2A·q + C·Y) / (2γ̂)` has `1/γ̂` as a multiplier on
EVERY component (inventory and OFI). Bounding `α` requires either:
- `γ̂` to be O(1) (i.e., ε ≫ γ in our regime — but then it's not BL anymore,
  it's just a different model with γ̂ = ε)
- OR every other coefficient (A, C) to scale as O(γ̂) so the ratios cancel

For A: `A_eq = √(φγ̂)`. To get `A/γ̂ = O(1/T)`, need `√(φ/γ̂) = O(1/T)`,
i.e., `φ = γ̂/T²`. That's our prior `c_κ × γ̂/T²` rule — collapses with γ.

For C: similar argument, but C also has `σ²·A/γ̂` source which produces
non-vanishing C even when A is small. The 1/γ̂ in α multiplies C up
again. There's no clean way to bound this within the BL framework with
arbitrary σ and tiny γ.

## What this means

The literature was right: **BL/AC requires γ > 0 in a non-trivial sense**.
Engineering regularization via `γ̂ = max(γ, ε)` doesn't recover the
mathematical structure — it just substitutes a different (larger) γ. At
that point we're not solving "BL at γ ≈ 10⁻⁶"; we're solving "BL at γ = ε"
and pretending we calibrated to ε.

The honest reading: in the maker-rebate regime, BL's HJB ansatz with a
quadratic running impact cost γα² is **operationally inappropriate**.
The math IS doing the right thing — it's saying "if trading is free,
trade infinitely fast." The fix isn't to fudge γ; it's to change the
cost structure.

## What the next iteration must change structurally

Two viable paths from the literature:

### Path A: replace the α formula entirely

Don't optimize a continuous trading rate at all. Adopt the
Avellaneda-Stoikov framework: optimize the quote spread `δ_b, δ_a`,
let fills arrive as Poisson(λ(δ)), and let inventory risk plus
non-execution risk define the well-posed problem. No 1/γ anywhere.
Reference: Avellaneda & Stoikov 2008, equation set on pp. 9-10.

### Path B: scheduler/quoter split (operator's prior recommendation)

Keep BL/AC for the inventory **target path** only — i.e., set a
γ-INDEPENDENT optimal q*(t) curve (essentially TWAP or a slight front-load
based on inventory variance). Implement that target via an AS-style
quoter that handles the OFI feedback at the order-placement layer, not
in the rate formula.

This is the path the prior session's notes already identified. The
validation matrix above confirms: there's no parameter setting inside
the BL framework that meets all six criteria simultaneously, so the
architectural split isn't optional.

## Deliverable inventory (committed alongside this doc)

| Path | Status |
|---|---|
| `math_core/regularized_riccati.py` | committed; closed-form A; RK4 C |
| `scripts/reproduce_riccati_matrix.py` | committed; reproducible driver |
| `autoresearch_gated/riccati_validation_matrix.json` | committed; full 150-run results, branch SHA, parameters, per-cell verdict |

The JSON artifact contains the per-cell trajectory diagnostics; anyone
on the team can reproduce by:

```bash
git checkout <SHA from artifact>
venv/bin/python3 scripts/reproduce_riccati_matrix.py \
    --json autoresearch_gated/riccati_validation_matrix.json
diff <( jq . autoresearch_gated/riccati_validation_matrix.json ) <( jq . /tmp/replay.json )
```

## Verdict on "candidate baseline" status

**Not granted.** The operator's six-criterion bar was correctly designed
to expose exactly this kind of false-positive — and it did. The
regularized scheme as specified is not a viable scheduler.

What we *do* have:
- A clear empirical refutation of the simple γ-regularization hypothesis
- A reproducible artifact + parameter block + branch SHA
- A diagnosis of the structural reason (1/γ̂ multiplies every term in α)
- A literature-aligned next direction (AS-quoter or Path B split)

What we don't have yet:
- A scheduler that meets all six criteria
- A working OFI-aware execution policy at γ ≈ 10⁻⁶
- The right path forward from this falsification

## Recommended next session

1. Stop trying to make BL work at γ ≈ 10⁻⁶. The math says no.
2. Implement a **γ-independent target q*(t)** curve in `math_core/`
   (essentially: TWAP, or sinh-ratio with a γ-independent decay rate).
   This is the scheduler.
3. Build a separate AS-style quoter that takes q*(t) as input and
   produces δ_b, δ_a at the order-book layer.
4. Test the *combined* policy against the same six criteria.

Until then, the live execution path stays on the existing fixed-size
maker logic. No `_size_order()` change is justified by anything in this
session's mathematical work.
