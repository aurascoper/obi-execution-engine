# Stage 3 Gate D + Expectation Bands — Review Note

**Status:** DRAFT for operator review. Steps 1 + 2 of the Stage 3 prerequisite list. **No risk-path edits.** Step 3 (MAX_NET reduce-only) **not yet started.**

**Generated:** 2026-04-29T~00:00Z
**Built artifacts:**
- `scripts/derive_expectation_bands.py` — derives per-symbol bands; reads `config/stage3_universe.json` for default-class coverage
- `scripts/gate_d_eval.py` — runs the forward-soak attribution check
- `config/stage3_universe.json` — 96-symbol declared Stage 3 universe (26 native + 70 HIP-3)
- `config/expectation_bands.json` — DRAFT bands covering full universe (Stage 2.5 live data + class-default fallback)

---

## How Gate D classifies PASS / PENDING / FAIL

```
FAIL    if  outlier_count_breach OR aggregate_floor_breach
PENDING if  insufficient_evaluable_symbols (n_evaluable < min_evaluable_symbols_for_pass)
PASS    if  n_outliers == 0 AND n_in_band ≥ 1 AND n_evaluable ≥ min_evaluable
```

Where:
- A symbol is **evaluable** if it has ≥ `min_round_trips_per_symbol` (default **3**) round-trips
- An evaluable symbol is **in_band** if its **median trip P&L** is inside the per-symbol band
- An evaluable symbol is **outlier** if its median is outside the band
- A symbol with < 3 round-trips is **pending_thin_sample** — it does NOT count toward in-band, outliers, or fail (per operator rule, 2026-04-28)
- `outlier_count_breach` ⇔ n_outliers > `max_outlier_symbols` (default 2)
- `aggregate_floor_breach` ⇔ Σ closed_pnl < `min_aggregate_pnl_usd` (default -$37.50)
- `insufficient_evaluable_symbols` ⇔ n_in_band + n_outliers < `min_evaluable_symbols_for_pass` (default 3)

The `min_evaluable_symbols_for_pass` guard prevents a "1 symbol in band, everything else thin" false-positive PASS. Operator can override in the bands config.

---

## Current run on Stage 2.5 soak data

```
decision:        pending
n_in_band:       1   (xyz:SILVER, 3 trips, median +$0.034 inside [-$0.099, +$0.201])
n_outliers:      0
n_pending_thin: 19   (LTC, XRP, BTC, xyz:TSM, xyz:EWY, ENA, NEAR, xyz:CL, ...
                     xyz:GOOGL, para:BTCD, etc. — all 1 round-trip each)
n_untracked:     0
aggregate_pnl:   +$0.49
floor_breach:    false
outlier_breach:  false
insufficient_evaluable_symbols: true
```

**The PENDING decision is correct.** A 4.2h soak with 23 round-trips across 20 symbols is structurally below the Gate D sample threshold — at most 1 symbol clears the N=3 bar.

---

## Under-sampled symbols (pending — exempt from pass/fail)

19 of 20 symbols have only 1 round-trip:

```
LTC, XRP, xyz:TSM, xyz:EWY, ENA, NEAR, xyz:CL, BTC, xyz:SP500,
xyz:NATGAS, POL, xyz:MU, ADA, UNI, xyz:TSLA, xyz:PLTR, xyz:GOOGL,
para:BTCD, [+ xyz:XYZ100 with 2 trips]
```

This means Gate D cannot evaluate Stage 2.5 alone — promotion to Stage 3 will require either:
- (a) a longer Stage 2.5 soak to accumulate ≥ 3 round-trips on most symbols, OR
- (b) accepting a Stage 3 run that is itself partly diagnostic (which makes the Stage 3 launch the Gate-D-evaluable soak)

**Recommendation:** option (b) — the Stage 3 run produces its own forward-soak data, evaluated against bands derived from Stage 2.5. That's a proper forward-attribution test even if Stage 2.5 itself was under-sampled for self-evaluation.

---

## Stage 2.5 live-first bands vs replay-fallback

Per the approved derivation rule:
- ≥ 5 live samples → `live_full` band (median ± 1.5 × IQR × sizing_ratio)
- 2-4 samples → `live_thin` band (median ± observed range × sizing_ratio)
- 1 sample → `live_single` band (single point ± 1.0 × |value| × sizing_ratio, padded)
- 0 samples → `default_class` (HIP-3 vs native default, scaled by sizing_ratio)

### Result of Stage 2.5 → Stage 3 derivation (96-symbol universe)

| band_source | count | example |
|---|---|---|
| `live_full` | **0** | (no symbol hit ≥ 5 round-trips) |
| `live_thin` | 2 | xyz:SILVER (n=3), xyz:XYZ100 (n=2) |
| `live_single` | 18 | LTC (n=1), XRP (n=1), BTC (n=1), ... |
| `default_class` | **76** | xyz:HIMS, xyz:HOOD, hyna:FARTCOIN, vntl:DEFENSE, ... — all symbols declared in `config/stage3_universe.json` that did NOT trade in the Stage 2.5 window |

**Stage 3 symbols absent from Stage 2.5 live data are still evaluable via `default_class` bands** — the bands config now covers the full 96-symbol Stage 3 universe (26 native + 70 HIP-3, sourced from `config/stage3_universe.json`).

**Replay-fallback was NOT exercised** because every traded symbol had at least one live trip, and `_baseline_per_symbol.json` / `replay_position_sessions.json` only carry aggregate-level data (no per-trip distributions for IQR computation). Untraded symbols use class-default bands directly; that's the gap replay-fallback would fill if per-trip replay data were available.

### Sample bands

```
xyz:SILVER  n=3  med=+$0.034   band=[-$0.099, +$0.201]   live_thin
xyz:XYZ100  n=2  med=+$0.057   band=[-$0.065, +$0.235]   live_thin
BTC         n=1  med=+$0.088   band=[-$0.168, +$0.432]   live_single
xyz:GOOGL   n=1  med=-$0.050   band=[-$0.575, +$0.425]   live_single
para:BTCD   n=1  med=-$0.061   band=[-$0.700, +$0.518]   live_single
ADA         n=1  med=-$0.166   band=[-$0.916, +$0.586]   live_single
```

The `live_single` bands are deliberately wide (single-point ± 100% padding × sizing_ratio). They will not produce false outliers but they also barely constrain anything. **Operator should treat them as placeholders** until Stage 3 generates more per-symbol round-trips.

---

## Assumptions that need operator sign-off

These defaults were chosen for the draft. Operator can override in `config/expectation_bands.json`:

1. **`sizing_ratio = 1.5`** (Stage 3 = $75, Stage 2.5 = $50). If Stage 3 is intended at $100, set ratio = 2.0 — bands widen accordingly.

2. **`min_round_trips_per_symbol = 3`** for evaluation (operator-approved 2026-04-28). Could be loosened to 2 for the first Stage 3 soak if you want broader evaluation earlier.

3. **`max_outlier_symbols = 2`**. With ~20 banded symbols, that's ~10% outlier tolerance. Tighten to 1 for a stricter Stage 3 acceptance.

4. **`min_aggregate_pnl_usd = -$37.50`** (= 50% of the $75 loss-guard at Stage 2.5). At Stage 3, this should be re-derived from the new loss-guard ($112.50 → -$56.25). The deriver uses the Stage 2.5 floor by default; operator should bump on Stage 3 launch.

5. **`min_evaluable_symbols_for_pass = 3`** — added by me as a guard against "1 symbol in band → PASS" false positives. Defensible at any value ≥ 1; 3 means "at least 3 symbols actually made it through a Gate D evaluation."

6. **Class-default bands** (HIP-3: [-$0.40, +$0.20], Native: [-$0.50, +$0.30] at Stage 2 sizing). These are guesses based on observed Stage 2.5 magnitudes. Could be tightened or replaced if you have stronger priors.

7. **Manual cloid prefix** (`0xdead0001`) is the Gate E exclusion mask. Hardcoded to match `scripts/lib/manual_order.py`. If we ever change the prefix, both files need updating.

---

## What this means for Stage 3 promotion

Stage 3 can now be **planned**, but **not promoted purely from Stage 2.5 evidence**:

- Stage 2.5 self-evaluation is PENDING (sample-thin), not FAIL. The strategy did not lose money or hit any guard, so no negative signal here.
- The bands are **declared** — that's the prerequisite for Stage 3.
- The Gate D evaluator is **wired** — that's the prerequisite for the Stage 3 acceptance check.
- The actual Gate D **PASS** verdict will come from running the Stage 3 soak itself against these bands.

This matches the design doc ordering: Steps 1 and 2 are deliverables tonight; Step 3 (the MAX_NET reduce-only patch) is required before the Stage 3 launch; Step 4 is the launch.

---

## Files for review

- `scripts/derive_expectation_bands.py` — pure analysis
- `scripts/gate_d_eval.py` — pure analysis
- `config/stage3_universe.json` — 96-symbol Stage 3 universe declaration
- `config/expectation_bands.json` — generated (96 symbols: 0 live_full, 2 live_thin, 18 live_single, 76 default_class)
- `docs/stage3_gate_d_review_note.md` (this file)

**Stop point:** I'm not committing these yet — review first, then I commit on your go-ahead. After that, request explicit authorization on Step 3 (MAX_NET reduce-only patch in `_risk_gate_ok`).
