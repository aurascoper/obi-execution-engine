# Quoter Family Sweep × Microstructure_v1 — Task 22 Findings (Gate 2B)

**Status:** Gate 2B CLEARED. All three scheduler families pass the
9-criterion bar against the unchanged quoter under the upgraded fill
model.
**Run:** `scripts/test_quoter_tracking.py --all-families --fill-model
microstructure_v1` (10 seeds × 3 OFI scenarios × 3 families = 90 runs).
**Artifact:** `autoresearch_gated/quoter_family_microstructure_matrix.json`
**Date:** 2026-04-29
**Predecessors:**
- Task 20: family sweep (simple model) — 6/6 per family (3.3× / 2.6× / 2.8×)
- Task 21: TWAP × microstructure_v1 — 9/9 (1.93× speedup, markout signal real)

## TL;DR

The scheduler/quoter contract holds under both axes simultaneously:
swap the target inventory curve **and** swap the fill model from stylized
Bernoulli to queue-position-aware microstructure with markout. Same
QuoterParams, no retuning, decision-rule stop-at-first-failure not
triggered.

The **same structural pattern** that emerged in Tasks 20 (toxic
completion is family-invariant) and 21 (markout differentiates) appears
again here: toxic completes in ~1430s regardless of family, markout signal
is consistent across families (~−0.23 / ~+4 / ~+5.4 bps). What does
change with curve curvature is the magnitude of behind-only miss — but
that scales gracefully and stays well inside the $5k bound.

## Per-family per-scenario diagnostic table

| Family | Scenario | Terminal q | Max behind (max) | Regime mix (mean, P/T/C) | Completion | Maker / Taker | Partials | Maker markout |
|---|---|---:|---:|---|---:|---:|---:|---:|
| **TWAP** | neutral | $0.00 | $61.95 | 91.5% / 8.5% / 0% | 2390s | 300.1 / 0.0 | 236.0 | +3.99 bps |
| TWAP | toxic | $0.00 | $11.46 | 65.5% / 34.5% / 0% | **1431s** | 236.1 / 0.0 | 221.8 | **−0.24 bps** |
| TWAP | favorable | $0.00 | $61.95 | 100.0% / 0% / 0% | 2763s | 323.2 / 0.0 | 240.0 | **+5.51 bps** |
| **Exponential ρ=2** | neutral | $0.00 | $572.84 | 91.8% / 8.1% / 0.03% | 2203s | 278.8 / 0.1 | 233.3 | +4.02 bps |
| Exponential | toxic | $0.00 | $271.47 | 65.7% / 34.4% / 0% | **1429s** | 236.0 / 0.0 | 221.9 | **−0.23 bps** |
| Exponential | favorable | $0.00 | $589.73 | 98.7% / 1.3% / 0% | 2408s | 294.0 / 0.0 | 243.0 | **+5.34 bps** |
| **Sinh-ratio κ=2** | neutral | $0.00 | $476.06 | 92.3% / 7.7% / 0% | 2285s | 289.0 / 0.0 | 237.2 | +4.06 bps |
| Sinh-ratio | toxic | $0.00 | $184.34 | 65.6% / 34.4% / 0% | **1429s** | 236.2 / 0.0 | 222.1 | **−0.23 bps** |
| Sinh-ratio | favorable | $0.00 | $576.46 | 99.8% / 0.2% / 0% | 2523s | 302.0 / 0.0 | 242.5 | **+5.47 bps** |

Acceptance bound for max-behind is $5,000 (50% of |q₀|). All within
bound by 8-450×.

## 9-criterion verdict per family

| Family | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | all_pass |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| TWAP | PASS | PASS | PASS | PASS (1.93×) | PASS | PASS | PASS | PASS | PASS | **TRUE** |
| Exponential | PASS | PASS | PASS | PASS (1.69×) | PASS | PASS | PASS | PASS | PASS | **TRUE** |
| Sinh-ratio | PASS | PASS | PASS | PASS (1.77×) | PASS | PASS | PASS | PASS | PASS | **TRUE** |

**Decision rule (stop at first failed family) not triggered. Gate 2B cleared.**

## Cross-family observations

### 1. Toxic completion remains family-invariant (1429-1431s)

The same finding from Task 20 carries over: once the quoter spends ~34%
of toxic steps in TOUCH (clip multiplier 2× plus near-touch fills),
realized inventory falls below every target curve and schedule shape
stops binding. Variation across families is single-digit seconds — well
inside seed noise.

### 2. Markout signal is family-invariant

| Family | Toxic markout | Neutral markout | Favorable markout |
|---|---:|---:|---:|
| TWAP | −0.24 bps | +3.99 bps | +5.51 bps |
| Exponential | −0.23 bps | +4.02 bps | +5.34 bps |
| Sinh-ratio | −0.23 bps | +4.06 bps | +5.47 bps |

The markout differentiation is essentially identical across families.
Markout is a property of (when we fill, where the mid drifts after) —
the schedule shape doesn't materially shift fill timing within
microstructure_v1's parameter envelope.

### 3. Max-behind tracks curve curvature (as in Task 20)

| Family | Neutral max behind | Favorable max behind |
|---|---:|---:|
| TWAP | $62 | $62 |
| Exponential | $573 | $590 |
| Sinh-ratio | $476 | $576 |

Front-loaded curves push the early target above what PASSIVE-rate fills
deliver, so behind-error builds up. The PASSIVE→TOUCH escalation kicks
in at e_norm > 5%; in this regime that boundary maps to ~$500 of miss,
which is exactly what we observe. Once TOUCH activates (8% of the time
in neutral), the over-delivery brings tracking back. No criterion at
risk.

### 4. Touch share is identical across families in toxic

In toxic, all three families show 34.4-34.5% TOUCH share. The
toxicity-triggered escalation does not depend on the schedule curve —
y_obi crosses the 0.30 threshold at the same rate regardless of where
the quoter thinks it should be on the inventory glide path. This is
the architectural reward: OFI handling lives in the quoter, schedule
handling lives in the scheduler, the two don't entangle.

### 5. One taker fill in exponential/neutral

Exponential triggered one CATCHUP step (0.03% of the run) and one
taker fill across 10 seeds. The taker markout was −1.42 bps, which
**makes architectural sense**: CATCHUP is the regime where we accept
worse markout to guarantee completion. Crit 7's −2.0 bps floor still
held. No impact on the family verdict.

## What this still does NOT prove

The same caveats from Task 21 apply, plus:

- This sweep used the **same** scheduler shape parameters (ρ=2.0,
  κ=2.0) as Tasks 17/20. A rougher shape (e.g., ρ=4.0, κ=4.0) might
  separate the families. That's a tuning question, not architecture.
- The fill model is still v1. Specific gaps:
  - constant aggressor arrival rate
  - lognormal aggressor sizes with no large-trade tail
  - linear queue-depth model
  - Y_t and mid drift coupled by a single gain
  - no fee/rebate accounting
- The 9-criterion bar is necessary, not sufficient. Live wiring still
  requires:
  - Gate 2C: AR(1) OBI calibrated to `signal_tick.obi`
  - Gate 2D: realized markout vs replay L2
  - Gate 3: shadow telemetry in engine
  - Gate 4: feature-flagged `_size_order()`

## Reproducibility

```bash
git checkout <SHA from artifact>
venv/bin/python3 scripts/test_quoter_tracking.py \
    --all-families --fill-model microstructure_v1 --seeds 10
diff <( jq . autoresearch_gated/quoter_family_microstructure_matrix.json ) \
     <( jq . /tmp/replay.json )
```

Outputs deterministic given seeds 0..9 and the committed defaults.

## Gate status

| Gate | Status |
|---|---|
| Gate 1: family sweep, simple model | CLEARED (Task 20) |
| Gate 2A: TWAP × microstructure_v1 | CLEARED (Task 21) |
| **Gate 2B: family sweep × microstructure_v1** | **CLEARED (Task 22)** |
| Gate 2C: AR(1) OBI calibrated to signal_tick.obi | not started |
| Gate 2D: realized markout vs replay L2 | not started |
| Gate 3: shadow telemetry in engine | not started |
| Gate 4: feature-flagged `_size_order()` | not started |

## Recommended next session (Task 23)

> **Gate 2C — calibrate AR(1) OBI to live data.**
>
> Replace the synthetic OBI process (`obi_phi`, `obi_target`, `obi_vol`,
> `obi_clip`) with parameters fit from `signal_tick.obi` history.
> Three sub-tasks:
>
> 1. add a `scripts/calibrate_obi_ar1.py` that reads from `data/bars.sqlite`
>    or the existing OBI logs, fits AR(1), and writes `config/obi_ar1.json`
> 2. extend `MicrostructureParams` to load these defaults
> 3. rerun the family sweep and compare per-scenario completion +
>    markout vs the v1 results
>
> If the calibrated process changes the verdict in any direction
> (better OR worse), document the delta. If verdict unchanged,
> 2C is cleared.

`_size_order()` and the live maker path remain frozen. Stashes parked.
