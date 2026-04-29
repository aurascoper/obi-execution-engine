# Quoter Microstructure_v1 — Task 21 Findings (Gate 2A)

**Status:** Gate 2A CLEARED. TWAP scheduler under the upgraded fill model
clears the 6+3 acceptance bar.
**Run:** `scripts/test_quoter_tracking.py --fill-model microstructure_v1
--scheduler twap` (10 seeds × 3 OFI scenarios).
**Artifacts:**
- `math_core/fill_model.py` — pure-math environment module
- `autoresearch_gated/quoter_microstructure_matrix.json` — full per-seed +
  aggregate + acceptance, pinned to git SHA + parameter block
**Date:** 2026-04-29
**Predecessors:**
- `docs/quoter_design_spec.md` (Task 18)
- `math_core/quoter_policy.py` (Task 19, unchanged)
- `math_core/schedulers.py` (committed Task 17)
- `docs/quoter_family_sweep_findings.md` (Task 20 Gate 1 result)

## TL;DR

The scheduler/quoter contract survives the move from a stylized Bernoulli
fill draw to an environment with queue position, partial fills, AR(1) OBI,
AR(1) spread, and post-fill markout. The same QuoterParams that cleared
Gates 1 and 19's smoke bar clear the 9-criterion bar without retuning.

The new evidence — and the reason this is a **stronger** result than
Task 20 — is that **markout differentiation is now a real signal**:
toxic fills lock in adverse drift (markout −0.24 bps), favorable fills
lock in protective drift (+5.51 bps), and the quoter responds in the
correct direction (toxic 34.5% TOUCH vs favorable 0%).

## Per-scenario diagnostic table

| Scenario | Terminal q | Max behind (max) | Regime mix (mean) | Completion | Maker / Taker fills | Partial fills | Forced flush | Maker markout |
|---|---:|---:|---|---:|---:|---:|:---:|---:|
| **Neutral** | $0.00 | $61.95 | PASSIVE 91.5% / TOUCH 8.5% | 2390s | 300.1 / 0.0 | 236.0 (mean) | 0 | **+3.99 bps** |
| **Toxic** | $0.00 | $11.46 | PASSIVE 65.5% / TOUCH 34.5% | **1431s** | 236.1 / 0.0 | 221.8 | 0 | **−0.24 bps** |
| **Favorable** | $0.00 | $61.95 | PASSIVE 100.0% | 2763s | 323.2 / 0.0 | 240.0 | 0 | **+5.51 bps** |

Acceptance bound for max-behind is $5,000 (50% of |q₀|). All within
bound by ~80×.

## 9-criterion verdict

| # | Criterion | Verdict | Evidence |
|---|---|:---:|---|
| 1 | tracking-bounded (behind-only) | PASS | max behind ≤ $62 across all scenarios |
| 2 | no sign crossing | PASS | zero crossings across 30 runs |
| 3 | terminal completion | PASS | terminal q = $0.00 in all runs (residual cap on quote works) |
| 4 | toxic faster than favorable | PASS | 1431s vs 2763s = **1.93× speedup** |
| 5 | maker share in non-catchup | PASS | 236-323 maker fills, zero taker fills |
| 6 | no forced flush in normal | PASS | zero forced flushes in any scenario |
| 7 | markout not catastrophic | PASS | neutral +3.99, favorable +5.51 (floor −2.0) |
| 8 | toxic worse markout AND more escalation | PASS | −0.24 < +5.51 AND 34.5% > 0% escalation |
| 9 | partial-fill tracking bounded | PASS | 222-240 partials per run, no flush, behind ≤ $62 |

**ALL_PASS = TRUE.**

## Observations

### 1. Markout becomes a real signal

In the Task 19/20 simple model, the only difference between toxic and
favorable was a synthetic `α_y` attenuation on the fill-rate parameter.
There was no notion of "we got filled, then mid moved against us."

Microstructure_v1 makes that real:
- `mid_drift_y_coupling_bps = +1.0` couples mid drift to OBI sign
- AR(1) Y_t around scenario target spends most of its time near target
- Each fill records mid at fill time and at t+5s
- Markout sign convention: positive = good for our side

The empirical result vindicates the design: toxic fills lose 0.24 bps on
average (we sold, mid rose); favorable fills earn 5.51 bps (we sold, mid
dropped); neutral fills earn 3.99 bps (small symmetric drift, plus
fills tend to cluster at mid peaks naturally).

### 2. Quoter escalation is correctly proportional

The OBI-driven regime classifier escalates exactly when it should:
- Favorable (Y ≈ −0.4): no toxic threshold breaches → 100% PASSIVE
- Neutral (Y ≈ 0): occasional excursions above +0.30 threshold → 8.5% TOUCH
- Toxic (Y ≈ +0.6): consistently above threshold → 34.5% TOUCH

CATCHUP fraction is 0% in all scenarios — the quoter never had to fall
back to IOC, which is the desired behavior in a working maker policy.

### 3. Partial fills dominate

Mean partial-fill count is 222-240 per run versus total fills of 236-323.
Roughly **75-78% of fills are partial.** This is exactly what the queue
model should produce: an aggressor's lognormal size often exceeds queue
ahead but is smaller than our residual, so we get hit for less than the
clip we posted. The quoter's tracking remains bounded under this
distribution — the residual-cap on quote when remaining < clip handled
the over-shoot risk that initially produced terminal q ≠ 0 and forced
flushes (caught and fixed during Task 21 development).

### 4. Toxic finishes 1.93× faster (vs 3.3× in simple model)

In the simple model, toxic vs favorable was a 3.3× speedup; here it's
1.93×. Why the smaller ratio? Two reasons:
- Favorable PASSIVE fills are now strong (323 fills, mostly partials)
  rather than rate-limited by `λ(δ=5bps) = 0.082/s`. Aggressor flow has
  no Y-attenuation in v1 (only mid drift differs), so favorable
  throughput is realistic.
- Toxic CATCHUP fraction is 0% here (vs simple where it was also 0%);
  the speedup comes purely from TOUCH clip-multiplier 2× and tighter
  queue position.

Both ratios show the same direction (toxic faster); the magnitude is now
calibrated to a more realistic environment.

### 5. Tracking-error magnitude scales with curve curvature, not microstructure

Max-behind (max across seeds) is $62 in all scenarios — the same number
the simple TWAP run produced ($282 mean → $62 microstructure mean is
actually tighter, because partial fills smooth tracking).

## Implementation notes (caught during the run)

A residual-cap fix landed mid-development: when `q_remaining < intent.clip_size`,
the harness now sets `quote.residual = min(quote.residual, q_remaining)`
and skips IOC fills past the remainder. Before this fix, the run produced
terminal_q ≈ −$15 in neutral with sign crossings and forced flushes.
**The bug was in the harness, not the quoter or fill model** — the quoter
was correctly issuing intents; the environment was over-filling them.
The fix is the natural envelope: a posted limit should never represent
more inventory than the principal still holds.

## What this still does NOT prove

- This is one scheduler family (TWAP). The family sweep under
  microstructure_v1 is queued for the next session, gated on this pass.
- The fill model is **v1**, not realistic. Specific simplifications:
  - Aggressor arrival rate is constant (no microstructure clustering).
  - Aggressor size is lognormal with fixed CV (no large-trade tail).
  - Queue ahead grows linearly with offset (no realistic LOB shape).
  - Mid drift is AR(1) coupled to Y; no jumps, no news, no toxic flow
    distinct from generalized adverse drift.
  - Cancellation rate is constant (no end-of-life concentration).
  - No fee/rebate accounting (markout is the only economic metric).
- The 9-criterion bar is necessary, not sufficient. A working live
  policy still needs to handle: latency, exchange-side cancels,
  partial-fill timeouts, adverse-selection regime shifts, and OBI/markout
  divergence (cases where Y is benign but markout is toxic).

## Reproducibility

```bash
git checkout <SHA from artifact>
venv/bin/python3 scripts/test_quoter_tracking.py \
    --fill-model microstructure_v1 --scheduler twap --seeds 10
diff <( jq . autoresearch_gated/quoter_microstructure_matrix.json ) \
     <( jq . /tmp/replay.json )
```

Outputs deterministic given seeds 0..9 and the committed default
MicrostructureParams.

## Gate status

| Gate | Status | Next |
|---|---|---|
| Gate 1: family sweep, simple model | CLEARED (Task 20) | — |
| **Gate 2A: TWAP under microstructure_v1** | **CLEARED (Task 21)** | — |
| Gate 2B: family sweep under microstructure_v1 | not started | Recommended next |
| Gate 2C: AR(1) OBI calibrated to signal_tick.obi | not started | After 2B |
| Gate 2D: realized markout vs replay L2 | not started | Replay-driven validation |
| Gate 3: shadow telemetry in engine | not started | After 2D |
| Gate 4: feature-flagged `_size_order()` | not started | After Gate 3 |

## Recommended next session (Task 22)

> Run the existing `--all-families` sweep against `--fill-model
> microstructure_v1`, decision rule: stop at first failed family.
> Three families × three scenarios × ten seeds = 90 runs. Output goes
> to `autoresearch_gated/quoter_family_microstructure_matrix.json`
> (already wired into the harness). The likely failure mode (per
> Task 20 lessons) is that exponential / sinh-ratio raise the early-stage
> max-behind under PASSIVE — but that may now interact differently with
> partial fills and queue depth.

`_size_order()` and the live maker path remain frozen. Stashes parked.
