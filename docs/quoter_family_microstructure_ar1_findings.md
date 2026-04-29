# Quoter Family Sweep × Microstructure_v1 (AR1-Calibrated) — Task 23 Findings (Gate 2C)

**Status:** Gate 2C CLEARED. All three scheduler families pass the
9-criterion bar against the unchanged quoter when the OBI AR(1) process
is replaced with parameters fit from live `signal_tick.obi` history.
**Run:** `scripts/test_quoter_tracking.py --all-families --fill-model
microstructure_v1 --calibrated-obi`
**Calibration source:** `logs/hl_engine.jsonl` — 100 symbols, 1,093,138
signal_tick events.
**Artifacts:**
- `scripts/calibrate_obi_ar1.py` — fit driver
- `config/obi_ar1.json` — pooled + per-symbol parameters
- `autoresearch_gated/quoter_family_microstructure_ar1_matrix.json` — full sweep
**Date:** 2026-04-29
**Predecessors:**
- Task 21 (Gate 2A): TWAP × microstructure_v1 — 9/9
- Task 22 (Gate 2B): family sweep × microstructure_v1 — 3 × 9/9

## TL;DR

The verdict does not change: same QuoterParams, same scheduler defaults,
same 9-criterion bar — all pass. But the **calibrated environment is
strictly stricter than v1 on the toxic side**: markout is 2.6× more
adverse, the quoter responds with proportionally more escalation,
completion is faster. This is the test the architecture should pass —
under more realistic OBI dynamics the quoter responds correctly without
retuning, exactly because its threshold and reservation-skew logic is
written against OBI level (not OBI vol).

## Calibration result (config/obi_ar1.json)

| Parameter | v1 default | Calibrated (median across 100 symbols) | Delta |
|---|---:|---:|---|
| obi_phi | 0.9200 | **0.9526** | +0.033 (more persistent) |
| obi_target (mu of natural data) | 0.0000 | +0.0008 | ≈ 0 (validates "natural baseline ≈ 0") |
| obi_vol | 0.1000 | **0.0561** | −0.044 (LESS noisy per dt=10s) |
| obi_clip | 0.99 | 0.999 | ≈ same (OBI is bounded) |

Per-symbol ranges: phi ∈ [0.86, 0.999], mu ∈ [-0.33, +0.26],
sigma ∈ [0.003, 0.20].

**Important note on what was NOT calibrated:** the scenario `obi_target`
fields (toxic +0.6, neutral 0, favorable −0.4) are imposed test
conditions, not natural baselines. The calibrated `mu` (+0.0008) is
informational — it confirms the natural OBI baseline is ≈ 0, which
matches the neutral-scenario assumption. The scenario imposition is
what makes "toxic feel toxic"; calibration only changes how the OBI
process *moves around* whatever target the scenario imposes.

## Side-by-side comparison vs Gate 2B

| Family | Scenario | Toxic completion | | Toxic markout (bps) | | Toxic TOUCH% | | Max behind (max) | |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| | | **v1** | **AR1** | **v1** | **AR1** | **v1** | **AR1** | **v1** | **AR1** |
| TWAP | neutral | 2390s | 2709s | +3.99 | +4.87 | 8.5% | 1.5% | $62 | $62 |
| TWAP | **toxic** | **1431s** | **1347s** | **−0.24** | **−0.62** | 34.5% | **36.3%** | $11 | $11 |
| TWAP | favorable | 2763s | 2763s | +5.51 | +5.51 | 0% | 0% | $62 | $62 |
| Exponential | neutral | 2203s | 2199s | +4.02 | +4.04 | 8.1% | 7.4% | $573 | $573 |
| Exponential | **toxic** | **1429s** | **1347s** | **−0.23** | **−0.62** | 34.3% | **36.3%** | $271 | **$106** |
| Exponential | favorable | 2408s | 2409s | +5.34 | +5.36 | 1.3% | 1.2% | $590 | $590 |
| Sinh-ratio | neutral | 2285s | 2445s | +4.06 | +4.70 | 7.7% | 2.3% | $476 | $524 |
| Sinh-ratio | **toxic** | **1429s** | **1347s** | **−0.23** | **−0.62** | 34.4% | **36.3%** | $184 | **$72** |
| Sinh-ratio | favorable | 2523s | 2523s | +5.47 | +5.48 | 0.2% | 0.2% | $576 | $576 |

**Bold cells highlight where calibration produced material deltas.**

## Five empirical deltas

### Δ1. Toxic markout is 2.6× more adverse

v1 reports −0.23 to −0.24 bps; AR1 reports −0.62 across all three
families. Mechanism: φ=0.9526 vs 0.92 means the AR(1) OBI process
mean-reverts more slowly. Once OBI is near the toxic target +0.6, it
*stays* there — accumulating more cumulative adverse mid drift between
fills. Lower σ (0.056 vs 0.10) keeps OBI close to the target rather
than wandering above/below it. Result: each fill happens deeper into
the adverse drift than v1 estimated.

This is the right direction for a calibration: reality is harder than
the synthetic v1 default suggested.

### Δ2. Toxic completion is ~6% faster

1347s vs 1429s. Quoter spends MORE time in TOUCH (36.3% vs 34.4%) and
larger TOUCH clips finish faster. Why more TOUCH? Because OBI dwells
longer above the toxicity threshold (0.30) under higher persistence.
The quoter classifier hits TOUCH on more steps without any threshold
retuning.

### Δ3. Toxic TOUCH share +2pp

A small but consistent shift (~34% → ~36%). The mechanism is
straightforward: AR(1) with higher φ has longer dwell times at any
level. With OBI target = +0.6 well above threshold = +0.30, the higher
persistence concentrates more steps at clearly-toxic levels.

### Δ4. Toxic max-behind tightens for front-loaded curves

| Family | v1 max behind (toxic) | AR1 max behind (toxic) | Δ |
|---|---:|---:|---:|
| Exponential | $271 | $106 | -61% |
| Sinh-ratio | $184 | $72 | -61% |

Mechanism: with calibrated lower σ, OBI tracks the toxic target more
tightly. The quoter sees consistent toxicity from t=0, escalates to
TOUCH earlier in the run, and prevents the front-loaded curves' early
target from outpacing fills. v1's higher noise allowed brief
"non-toxic" excursions where the quoter dropped to PASSIVE and let
behind-error accumulate.

### Δ5. Neutral TOUCH share drops for TWAP/sinh

| Family | v1 neutral TOUCH | AR1 neutral TOUCH |
|---|---:|---:|
| TWAP | 8.5% | 1.5% |
| Exponential | 8.1% | 7.4% |
| Sinh-ratio | 7.7% | 2.3% |

Mechanism: in neutral with target 0 and lower σ, the AR(1) random walk
crosses the +0.30 threshold less often. v1's higher noise produced
spurious threshold breaches; calibration reduces them. Exponential is
less affected because front-loading produces real schedule misses
(behind-driven TOUCH, not toxic-driven TOUCH).

This is a quietly important result: the neutral scenario becomes more
cleanly neutral under calibration. The 8% TOUCH share in v1-neutral
was partly a spurious artifact of σ being too high.

## 9-criterion verdict per family

| Family | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | all_pass |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| TWAP | PASS | PASS | PASS | PASS (2.05×) | PASS | PASS | PASS | PASS | PASS | **TRUE** |
| Exponential | PASS | PASS | PASS | PASS (1.79×) | PASS | PASS | PASS | PASS | PASS | **TRUE** |
| Sinh-ratio | PASS | PASS | PASS | PASS (1.87×) | PASS | PASS | PASS | PASS | PASS | **TRUE** |

Toxic→favorable speedup ratios are slightly larger than v1 (1.93/1.69/1.77 →
2.05/1.79/1.87) because toxic completion sped up while favorable
completion was unchanged.

## What this proves

- The quoter's threshold-and-reservation-skew design is robust to
  realistic OBI dynamics. We changed φ from 0.92 to 0.9526 and σ from
  0.10 to 0.056 — meaningful changes in the dynamics — and the verdict
  did not flip. The architecture survives calibration.
- The calibrated environment is **harder** than v1 on the toxic side
  (markout 2.6× worse). The quoter compensates by escalating more, not
  by being lucky. This is exactly the closed-loop behavior the design
  intended.

## What this still does NOT prove

- Markout is still computed against simulated mid drift, not against
  replay/L2 prices. Gate 2D will close that.
- The OBI calibration pools 100 symbols' AR(1) fits with a median.
  Per-symbol fit dispersion is real (φ ranges 0.86 to 0.999, σ ranges
  0.003 to 0.20). A future task could partition the universe and
  test per-symbol-class calibration. Out of scope for Gate 2C.
- AR(1) on resampled 10s grid forward-fills sparse ticks. Some symbols
  emit signal_ticks at sub-second cadence; others have gaps of minutes.
  The 10s grid is consistent with our simulator dt; a finer grid might
  reveal different short-horizon dynamics.
- The fill model is otherwise unchanged. Aggressor flow, queue depth,
  spread, mid drift — all v1.

## Reproducibility

```bash
# Step 1: refit calibration (idempotent given the same log)
venv/bin/python3 scripts/calibrate_obi_ar1.py
# Step 2: rerun the family sweep
venv/bin/python3 scripts/test_quoter_tracking.py \
    --all-families --fill-model microstructure_v1 --calibrated-obi --seeds 10
diff <( jq . autoresearch_gated/quoter_family_microstructure_ar1_matrix.json ) \
     <( jq . /tmp/replay.json )
```

The calibration JSON is committed; the family-sweep output is
deterministic given seeds 0..9 and the calibrated config.

## Gate status

| Gate | Status |
|---|---|
| Gate 1: family sweep, simple model | CLEARED (Task 20) |
| Gate 2A: TWAP × microstructure_v1 | CLEARED (Task 21) |
| Gate 2B: family sweep × microstructure_v1 | CLEARED (Task 22) |
| **Gate 2C: AR(1) OBI calibrated to live data** | **CLEARED (Task 23)** |
| Gate 2D: replay/L2-driven realized markout | not started |
| Gate 3: shadow telemetry in engine | not started |
| Gate 4: feature-flagged `_size_order()` | not started |

## Recommended next session (Task 24)

> **Gate 2D — replay-driven realized markout.**
>
> Replace the `mid_drift_y_coupling_bps` synthetic mid drift with a
> replay-driven mid path: read consecutive 1s/5s mid ticks from
> `data/bars.sqlite` (or the ohlcv-bars hydrate output), align with
> a chosen replay window, and compute markout against actual realized
> future-mid rather than the AR(1)-coupled drift.
>
> Sub-tasks:
>   1. add `scripts/build_replay_mid_window.py` to extract a
>      reproducible replay window into a JSON or numpy file
>   2. add a "replay" mid-path option to MicrostructureParams /
>      run_scenario_microstructure
>   3. rerun the sweep against the replay window; compare markout
>      to the AR1-calibrated synthetic baseline
>
> If replay-realized markout signal is consistent with the synthetic
> markout direction, Gate 2D clears.

`_size_order()` and the live maker path remain frozen. Stashes parked.
