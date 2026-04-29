# Quoter Family Sweep — Task 20 Findings

**Status:** Gate 1 CLEARED. All three scheduler families pass the
six-criterion smoke bar against the unchanged quoter.
**Run:** `scripts/test_quoter_tracking.py --all-families` (10 seeds × 3
scenarios × 3 families = 90 runs).
**Artifact:** `autoresearch_gated/quoter_family_matrix.json`
**Date:** 2026-04-29
**Decision rule satisfied:** TWAP → exponential (ρ=2.0) → sinh-ratio (κ=2.0),
no early stop required.

## TL;DR

The scheduler/quoter architecture is **family-robust** under the synthetic
fill model: swapping the target inventory curve through
`get_target_inventory(...)` with the same QuoterParams produces a clean
6/6 pass for all three curves. Only the magnitude of behind-side miss
moves with curve front-loading; no criterion flips.

Toxic-completion time is **identical (1045s) across all three families**.
Once toxic OFI triggers TOUCH 29% of the time, the realized inventory
trajectory drops below every target curve, so the schedule shape stops
binding. Schedule shape only differentiates in non-toxic regimes.

## Per-family diagnostic table

| Family | Scenario | Terminal q | Max behind (max) | Regime mix | Completion | Maker fills | Forced flush |
|---|---|---:|---:|---|---:|---:|:---:|
| **TWAP** | neutral | $0.00 | $282.71 | PASSIVE 99.9% | 3440s | 194.7 | 0 |
| TWAP | toxic | $0.00 | $27.78 | 70.8% / 29.2% / 0% | **1045s** | 100.0 | 0 |
| TWAP | favorable | $0.00 | $282.71 | PASSIVE 99.9% | 3440s | 194.7 | 0 |
| **Exponential ρ=2** | neutral | $0.00 | $553.32 | PASSIVE 97.7% | 2752s | 159.6 | 0 |
| Exponential | toxic | $0.00 | $64.07 | 70.8% / 29.2% / 0% | **1045s** | 100.0 | 0 |
| Exponential | favorable | $0.00 | $553.32 | PASSIVE 97.7% | 2752s | 159.6 | 0 |
| **Sinh-ratio κ=2** | neutral | $0.00 | $543.53 | PASSIVE 98.8% | 2915s | 168.3 | 0 |
| Sinh-ratio | toxic | $0.00 | $57.47 | 70.8% / 29.2% / 0% | **1045s** | 100.0 | 0 |
| Sinh-ratio | favorable | $0.00 | $543.53 | PASSIVE 98.8% | 2915s | 168.3 | 0 |

Acceptance bound: max_behind ≤ $5,000 (50% of |q₀|). All values within
the bound by an order of magnitude.

## Per-family acceptance verdict

| Family | crit 1 | crit 2 | crit 3 | crit 4 | crit 5 | crit 6 | all_pass |
|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| TWAP | PASS | PASS | PASS | PASS (3.3×) | PASS | PASS | **TRUE** |
| Exponential | PASS | PASS | PASS | PASS (2.6×) | PASS | PASS | **TRUE** |
| Sinh-ratio | PASS | PASS | PASS | PASS (2.8×) | PASS | PASS | **TRUE** |

## Observations

### 1. Toxic completion is family-invariant

All three families finish toxic at exactly 1045s. The reason: under toxic
OFI the quoter spends ~29% of steps in TOUCH (clip multiplier 2× plus
δ=0 → ~0.95 fill probability per 10s window). Realized inventory falls
below every target curve, so the curve no longer binds. The
toxic→favorable speedup ratio (3.3×, 2.6×, 2.8×) varies only because
*favorable* completion shrinks with front-loading, not because toxic
behaves differently.

### 2. Max-behind tracks curve curvature

Front-loaded curves create higher early targets that the PASSIVE regime
must catch with its baseline rate. Until `e_t / |q₀|` crosses the 5%
PASSIVE→TOUCH threshold, the quoter stays in PASSIVE and accumulates
behind-error. TWAP's linear shape keeps early targets near realized,
so max_behind stays at $282; the front-loaded shapes push max_behind
up to ~$550. Still well within the $5,000 bound.

### 3. No regime calibration drift

The QuoterParams thresholds (`e_passive_frac=0.05`, `e_catchup_frac=0.20`,
toxicity_threshold=0.30) survive the family swap intact. No retuning
required. This is the cleanest possible Gate 1 result: the
scheduler/quoter contract held without touching any parameter.

### 4. Maker share holds in non-catchup

CATCHUP fraction is 0% for all front-loaded family runs (it was 0.14%
for TWAP). Maker fills 159-195 per run; taker fills ≤ 1. Rebate-capture
behavior intact across families.

### 5. No forced terminal flush in any of 90 runs

Every run completes within tolerance through the policy itself, not
through the simulator's terminal flush fallback. Clean.

## What this does NOT prove

This is exactly the same caveat list as Task 19, plus one new item:

- The fill model is still synthetic. `λ(δ,Y) = A·exp(-k·δ - α_y·Y⁺)`
  Bernoulli draws with no queue position, no L2 depth, no realized
  markout. Gate 2 work.
- The OFI scenarios are stylized (constant Y over the run). Real OFI
  has autocorrelation, regime shifts, and microstructure noise.
- All three families happen to share the same QuoterParams defaults.
  A different parameter regime (e.g., tighter `e_passive_frac`) might
  separate the families — that's a tuning question, not an architecture
  question.
- Adverse selection cost is implicit (mid drift in toxic) rather than
  per-fill markout. A passive fill that's immediately wrong is not yet
  punished in this harness.

## Gate status

| Gate | Status | Next |
|---|---|---|
| Gate 1: family sweep against quoter | **CLEARED** | Move on |
| Gate 2: realistic fill / queue / markout | not started | Required before live wiring |
| Gate 3: shadow telemetry in engine | not started | After Gate 2 |
| Gate 4: feature-flagged `_size_order()` | not started | After Gate 3 |

## Recommended next session

Pick one of:

- **Gate 2A — queue model:** add depth/queue-position state to the
  simulator. Fill probability conditional on queue depth ahead of our
  quote. Replay-driven L2 if available, otherwise stylized but
  autocorrelated.
- **Gate 2B — markout cost:** per-fill PnL using a forward midprice at
  e.g. t+5s. Maker-share criterion gets teeth: a fill is "good" only
  if markout doesn't reverse it.
- **Gate 2C — autocorrelated OFI:** drop constant Y, use AR(1) on Y_t
  with calibrated parameters from `signal_tick.obi`.

`_size_order()` and the live maker path remain frozen.

## Reproducibility

```bash
git checkout <SHA from artifact>
venv/bin/python3 scripts/test_quoter_tracking.py --all-families \
    --rho 2.0 --kappa 2.0 --seeds 10
diff <( jq . autoresearch_gated/quoter_family_matrix.json ) \
     <( jq . /tmp/replay.json )
```

Outputs are deterministic given seeds 0..9. Default kwargs match the
geometry in `math_core/schedulers.py`.
