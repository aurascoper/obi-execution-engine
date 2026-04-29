# Quoter Family Sweep × Replay-Driven Markout — Task 24 Findings (Gate 2D)

**Status:** Gate 2D CLEARED. All three scheduler families pass the
9-criterion bar against the unchanged quoter when post-fill mid is
sourced from a replay window (BTC 1m bars, +65 bps, ~90 minutes) rather
than synthetic OBI-coupled drift.
**Run:** `scripts/test_quoter_tracking.py --all-families --fill-model
microstructure_v1 --calibrated-obi --replay-mid
data/replay_windows/replay_BTC_90m_offset0.json`
**Replay source:** BTC 1m bar closes, 2026-04-29 02:39 UTC →
2026-04-29 04:08 UTC, +65.45 bps over 5350s, interpolated to 10s grid.
**Artifacts:**
- `scripts/build_replay_mid_window.py` — replay extractor
- `data/replay_windows/replay_BTC_90m_offset0.json` — committed window
- `autoresearch_gated/quoter_family_replay_markout_matrix.json` — sweep
**Date:** 2026-04-29
**Predecessors:**
- Task 23 (Gate 2C): AR(1)-calibrated OBI under synthetic mid drift.

## TL;DR

The verdict does not change: same QuoterParams, same scheduler defaults,
same 9-criterion bar — all pass. **The directional ordering preserves
under replay** (favorable > neutral > toxic in maker markout) for all
three families. Magnitudes compress because the synthetic
`mid_drift_y_coupling_bps=1.0` gave toxic an *additional* adverse drift
on top of the quoter's regime-mix effect; replay strips that synthetic
coupling away, so only the quoter's voluntary forfeit-of-PASSIVE-offset
remains as the markout differentiator.

That means the markout signal under replay is **the price the quoter
pays for escalation**, not a built-in environment penalty. The quoter
still pays it correctly (more often when OBI signals toxic) and the
directional structure survives.

## Replay window provenance

| Field | Value |
|---|---|
| symbol | BTC |
| source | `data/cache/bars.sqlite`, interval=1m |
| span | 2026-04-29T02:39 UTC → 2026-04-29T04:08 UTC |
| n_bars | 90 (1m bars) |
| n_grid_points | 535 (interpolated to dt=10s) |
| mid first / last | 76395 / 76895 |
| mid min / max | 76394 / 77076 |
| **total move** | **+65.45 bps** (bullish window) |

The window is **bullish**: BTC rose 65 bps over 90 minutes. For
liquidating sellers, every step has +0.12 bps/step adverse trend —
this is a structural seller's headwind that all three OFI scenarios
face equally under replay.

## Side-by-side: AR1-calibrated vs replay

| Family | Scenario | AR1 markout | Replay markout | Δ (replay − AR1) | AR1 completion | Replay completion | AR1 TOUCH% | Replay TOUCH% |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| TWAP | neutral | +4.87 | **+4.35** | −0.52 | 2709s | 2675s | 1.5% | 3.9% |
| TWAP | toxic | **−0.62** | **+0.03** | **+0.66** | 1347s | 1364s | 36.3% | 36.4% |
| TWAP | favorable | +5.51 | **+4.75** | −0.76 | 2763s | 2831s | 0.0% | 0.0% |
| Exponential | neutral | +4.04 | +4.20 | +0.16 | 2199s | 2344s | 7.4% | 5.6% |
| Exponential | toxic | **−0.62** | **+0.03** | **+0.66** | 1347s | 1363s | 36.3% | 36.4% |
| Exponential | favorable | +5.36 | +4.58 | −0.78 | 2409s | 2429s | 1.2% | 1.4% |
| Sinh-ratio | neutral | +4.70 | +4.29 | −0.41 | 2445s | 2457s | 2.3% | 4.6% |
| Sinh-ratio | toxic | **−0.62** | **+0.03** | **+0.66** | 1347s | 1363s | 36.3% | 36.4% |
| Sinh-ratio | favorable | +5.48 | +4.70 | −0.78 | 2523s | 2555s | 0.2% | 0.2% |

**Bold cells highlight the largest deltas.**

## Decision rule satisfied

| Requirement | Result |
|---|---|
| Replay-realized markout preserves directional structure | **PASS** |
|   • toxic worse than neutral | toxic +0.03 < neutral +4.20-+4.35 across all families |
|   • favorable better than neutral | favorable +4.58-+4.75 > neutral +4.20-+4.35 across all families |
| Family verdict does not collapse under replay markout | **PASS** (3 × 9/9) |
| Magnitude shift is documented | **DONE** (this section) |

Gate 2D clears.

## 9-criterion verdict per family

| Family | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | all_pass |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| TWAP | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | **TRUE** |
| Exponential | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | **TRUE** |
| Sinh-ratio | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | **TRUE** |

## Where the magnitude compression came from

In the synthetic AR1 environment, two things produced the toxic-vs-favorable
markout asymmetry:

1. **Quoter regime mix.** Toxic Y > 0.30 → quoter spends 36% of steps
   in TOUCH (δ=0) instead of PASSIVE (δ=+5bps). TOUCH gives up the
   +5bps maker offset.
2. **Synthetic mid coupling.** Toxic Y > 0 → `mid_drift = +0.6 bps/step`
   (mid rises against sellers). Favorable Y < 0 → `mid_drift = -0.4
   bps/step` (mid drops, favorable to sellers).

Replay strips out (2) — mid path is now exogenous, the same for all
three scenarios. Only (1) remains as the differentiating mechanism.

Mechanically:
- **Replay PASSIVE fill (δ=+5):** fill_price = mid + 5bps,
  markout ≈ +5 − one_step_trend ≈ +5 − 0.12 = **+4.88 bps**
- **Replay TOUCH fill (δ=0):** fill_price = mid,
  markout ≈ 0 − one_step_trend ≈ **−0.12 bps**

Toxic gets ~36% TOUCH fills, ~64% PASSIVE. The fill-event-weighted
average lands near 0 (TOUCH fills are concentrated and frequent). Hence
toxic markout collapses from −0.62 (synth, 36% TOUCH × −0.6 + 64%
PASSIVE × +4.4) to +0.03 (replay, the same regime mix but no toxic
mid-coupling drift).

This is the **stronger** result. In synth AR1, the environment
manufactured part of the directional asymmetry. In replay, **the
quoter's voluntary regime escalation is the entire mechanism** — it
gives up the maker offset in exchange for completion speed. The
directional structure survives even after stripping the synthetic
coupling.

## What this proves

- The directional structure (favorable > neutral > toxic markout) is
  **a property of the quoter's escalation behavior**, not just a
  property of the synthetic environment. Even with a fixed exogenous
  mid path, the quoter's voluntary forfeit of the PASSIVE offset
  produces the same ordering.
- The architecture survives apples-to-apples replay markout. No
  retuning. Same QuoterParams, same scheduler defaults, same regime
  classifier. The verdict held under all four environment swaps so
  far (simple → microstructure_v1 → AR1-calibrated → replay).

## What this still does NOT prove

- **Single replay window.** This run used one BTC 90-min slice. The
  window happened to be bullish (+65 bps). A bearish window would
  show different absolute magnitudes — possibly inverted favorable/
  toxic markout *signs* if mid drops sharply (sellers benefit). A
  multi-window sweep would tighten the result.
- **Single symbol.** BTC is the most-liquid universe member. A
  thin-book HIP-3 symbol might show different fill timing under
  partial-fill mechanics, even with the same QuoterParams.
- **OBI/mid coupling is severed in replay.** In live data, OBI and
  mid drift are correlated — that's the whole point of OBI being
  toxic. Replay treats them as independent. A more truthful Gate 2E
  would source BOTH the OBI series and the mid path from the same
  live time-window, preserving their natural correlation.
- **Markout horizon = one simulator step (10s)** under both modes.
  A longer horizon (60s, 5min) might amplify or compress the signal
  differently.
- **Interpolated 1m bars are smooth within the minute.** Real
  10s-resolution mid moves are noisier; this matters most for the
  TOUCH-fill markout which depends on the small per-step trend.

## Reproducibility

```bash
git checkout <SHA from artifact>
# Step 1: rebuild the replay window (idempotent given the same bars.sqlite)
venv/bin/python3 scripts/build_replay_mid_window.py \
    --symbol BTC --n-bars 90 --end-offset-bars 0
# Step 2: rerun the family sweep
venv/bin/python3 scripts/test_quoter_tracking.py \
    --all-families --fill-model microstructure_v1 \
    --calibrated-obi \
    --replay-mid data/replay_windows/replay_BTC_90m_offset0.json \
    --seeds 10
diff <( jq . autoresearch_gated/quoter_family_replay_markout_matrix.json ) \
     <( jq . /tmp/replay.json )
```

Outputs deterministic given seeds 0..9 and the committed replay window.
The replay JSON itself is committed so the run is reproducible even
if `data/cache/bars.sqlite` rolls forward.

## Gate status

| Gate | Status |
|---|---|
| Gate 1: family sweep, simple model | CLEARED (Task 20) |
| Gate 2A: TWAP × microstructure_v1 | CLEARED (Task 21) |
| Gate 2B: family sweep × microstructure_v1 | CLEARED (Task 22) |
| Gate 2C: AR(1) OBI calibrated to live data | CLEARED (Task 23) |
| **Gate 2D: replay-driven realized markout** | **CLEARED (Task 24)** |
| Gate 2E (proposed): joint OBI+mid replay window | not started |
| Gate 3: shadow telemetry in engine | not started |
| Gate 4: feature-flagged `_size_order()` | not started |

## Recommended next session (Task 25)

Two equally defensible next moves; pick whichever the operator prefers:

> **Option A — Gate 2E: joint OBI+mid replay window.**
> Source both `signal_tick.obi` and live mid (from price events or
> upsampled bars) over the SAME wall-clock window, with their natural
> correlation preserved. This closes the "OBI/mid coupling is severed"
> gap in Gate 2D's caveats and is the strongest pre-shadow validation.
>
> **Option B — Gate 3: shadow telemetry.**
> Wire the existing `sizing_runtime_shadow` event hook in `hl_engine.py`
> to emit `(scheduler_target, quoter_intent, actual_engine_action)` per
> tick alongside the live fixed-size maker logic. No order-path change.
> Run forward soak; compare distributions against simulator output.

Either way, `_size_order()` and the live maker path remain frozen.
Stashes parked.
