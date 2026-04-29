# Phase A Postmortem — net_cap timeline did not lift ρ as predicted

**Date:** 2026-04-26
**Implementation:** `scripts/z_entry_replay_gated.py:simulate_portfolio_gated()`
**Activation flag:** `PORTFOLIO_TIMELINE=1` (default off; legacy path unchanged)

## Headline

`autoresearch_gated/validation_report.md` predicted Phase A (net_cap timeline)
would lift portfolio ρ from **0.17 → 0.40**. Measured outcome: ρ peaks at
**0.23** at `MAX_NET=$1000` (5× live's actual cap of $200) and is **0.10**
at the live setting. **The predicted 0.40 was a model assumption, not a
measurement, and the model was wrong.**

## Sweep results (NOTIONAL=$80 fixed)

| MAX_NET_NOTIONAL | ρ |
|---:|---:|
| $100 | −0.08 |
| $200 (live) | 0.10 |
| $500 | 0.19 |
| **$1000** (peak) | **0.23** |
| $2000 | 0.18 |
| $5000 | 0.15 |
| ∞ (baseline, no G6) | 0.17 |

(NOTIONAL=$50: ρ=0.16. NOTIONAL=$100: ρ=−0.10. NOTIONAL≥$250 with cap=$200:
degenerate, no trades fire.)

## Why Phase A didn't deliver

The replay's candidate set is broader than live's filtered set. The live
engine evaluates net_cap **after** OBI / trend / momentum / flip-guard
gates have already pre-filtered most candidates. By the time entries reach
net_cap in production, most have been thinned to the ~2K rejections logged
over 14d.

The replay's `simulate_portfolio_gated()` runs net_cap on a much larger
candidate set (~23,500 blocks at $200 cap, 11× live's 2K). This
over-suppresses entries that live wouldn't have evaluated for net_cap
because they got blocked by upstream gates. The result: replay's
position-trajectory diverges from live's, the wrong entries get blocked,
sign-mismatches grow, ρ falls.

## What's structurally correct (and what's not)

Verified correct:
* Backward-compatibility: at `PORTFOLIO_TIMELINE=0`, output is identical
  to the legacy per-symbol path (deterministic across 3 runs).
* Timeline walk at non-binding cap (`MAX_NET=$100k`) reproduces legacy
  SCORE −1581.04 exactly. The new function is logically equivalent to
  the old when net_cap is non-binding.

Discovered wrong (vs validation report's expectation):
* Net_cap-modeling-alone is not the gap. The gap is "replay's candidate
  set ≠ live's filtered set." Adding the gate without first reproducing
  live's pre-filter trajectory makes ρ worse.

## Implications for future autoresearch rounds

1. **Trust ρ measurements, not model predictions.** The validation
   report's per-fix ρ forecasts are speculative until tested.
2. **`autoresearch_obi_theta/CAVEAT.md`'s ρ ≥ 0.80 gate remains correct.**
   Live promotion of replay-tuned parameters stays blocked.
3. **The path to higher ρ is harder than projected.** Likely candidates:
   - Restrict replay to ticks where live actually had a candidate signal
     (use logged `signal_tick` events as the candidate set, not all bars).
   - Per-symbol NOTIONAL calibrated from logged live `qty` field.
   - Audit-replay rather than predictive-replay (validate gate logic
     against logged live decisions, don't simulate alternative
     trajectories).

## Code state

`simulate_portfolio_gated()` remains in the file behind
`PORTFOLIO_TIMELINE=1`. **Default is off** (`PORTFOLIO_TIMELINE=0`); legacy
behavior unchanged. The function compiles, runs deterministically, and
matches legacy output at non-binding caps. Future iterations can revive
it without re-implementing the timeline walk.

To revive:
```bash
PORTFOLIO_TIMELINE=1 NOTIONAL_PER_TRADE=80 MAX_NET_NOTIONAL=200 \
    venv/bin/python3 scripts/z_entry_replay_gated.py
```

## Effort accounting

* Implementation + smoke testing: ~3 hours
* Param sweep (validate_replay_fit): ~1 hour
* Postmortem: ~30 min
* Total: ~4.5 hours

## Lesson saved to memory

See `feedback_validation_report_predictions_speculative.md` (companion
memory note) for the meta-lesson: don't trust documented ρ-lift predictions
without measuring them.
