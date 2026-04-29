# Stage 3 Promotion Design

**Status:** DRAFT — design only, no code changes.
**Date:** 2026-04-28
**Prereq for:** moving from Stage 2.5 ($50 dust + shadow Kelly) → Stage 3 ($75–$100 + Gate D)

Per `project_dust_soak_promotion_ladder.md`, Stage 3 requires:
1. Stage 2 acceptance (in flight — `sizing_shadow ≥ 200` trigger pending)
2. **Gate D forward-soak attribution active**
3. **Per-symbol expectation bands declared in `soak_state`**
4. (operator-added 2026-04-28) **MAX_NET semantic decision made**

This doc proposes one design for each open item. None of these are implemented tonight.

---

## 1. Gate D — forward-soak attribution

### Current state

`promotion_gate_redesign.md` defines Gate D as:
> Forward soak: per-symbol bands (not net P&L), attributed to live entries only, against pre-declared expectation bands.

The `validate_promotion_gate.py` runner has scaffolding but Gate D itself is currently a stub — it returns `mode: pending_forward_soak_data` and never actually evaluates.

### Proposed wiring

**Inputs:**
- `autoresearch_gated/soak_state_broad_micro.json` — soak window timestamps, status, intervention list
- `logs/hl_engine.jsonl` — fills with `tag` (so we exclude manual closes by `cloid` prefix `0xdead0001` and earnings-close interventions)
- `config/expectation_bands.json` (NEW) — per-symbol expected ρ-band

**Computation per symbol:**
```
1. Pull all hl_fill_received events in soak window with tag=hl_z, excluding
   manual-cloid-tagged fills (Gate E intervention mask).
2. Aggregate to closed round-trips: pair entry → exit per (symbol, side).
3. Compute realized PnL per symbol (gross of fees, then net of fees).
4. Compare per-symbol PnL distribution to declared expectation band.
5. PASS criteria: PnL median within band; outlier symbols flagged but not
   blocking unless concentrated.
```

**Status decision:**
- `pass` — all symbols with ≥ N round-trips inside their band
- `pending` — sample too thin overall, OR specific symbols under-sampled
- `fail` — more than K symbols **with sufficient sample** outside band, OR aggregate PnL below pre-declared minimum

**APPROVED rule (operator 2026-04-28):** under-sampled symbols (< N round-trips) **DO NOT count toward pass/fail outliers**. They are reported as `pending_thin_sample` and excluded from the K-outlier denominator. This keeps Gate D falsifiable without penalizing thinly-traded names.

`N` and `K` are set when bands are declared (see §2). **APPROVED N = 3** (operator 2026-04-28).

### What needs to be built

- `scripts/gate_d_eval.py` — computes the per-symbol attribution + decision
- Optional integration into `validate_promotion_gate.py` to roll Gate D into
  the overall `promotion_gate_status.json`

**Estimated effort:** 1–2 sessions, code-only, no risk-path touches.

---

## 2. Per-symbol expectation bands

### Why

Without pre-declared bands, "Stage 3 looks fine" is not falsifiable — anything can be rationalized post-hoc. Bands force a falsifiable claim before the soak runs.

### Format proposal

`config/expectation_bands.json`:

```json
{
  "schema_version": 1,
  "declared_at": "2026-04-29T...Z",
  "stage": 3,
  "min_round_trips_per_symbol": 3,
  "max_outlier_symbols": 2,
  "min_aggregate_pnl_usd": -25.0,
  "bands": {
    "BTC/USD":     {"pnl_per_trip_usd": [-0.50, 0.30], "rho_to_replay": [0.40, 0.85]},
    "ETH/USD":     {"pnl_per_trip_usd": [-0.40, 0.25], "rho_to_replay": [0.40, 0.80]},
    "xyz:AAPL/USD":{"pnl_per_trip_usd": [-0.30, 0.20], "rho_to_replay": [0.30, 0.70]},
    ...
  }
}
```

### How bands are derived

**APPROVED rule (operator 2026-04-28):** **Stage 2.5 live-soak data is the primary source; replay calibration is fallback only.** Replay is documented as ceiling-limited (`project_mode2_session_policy_ceiling.md`), so replay-only bands risk mis-anchoring Stage 3 expectations.

Process per symbol:

```
if symbol has ≥ N_BAND_SAMPLES Stage 2.5 live round-trips:
    band = live_median ± 1.5 × IQR × sizing_ratio
else:
    band = replay_median ± 1.5 × IQR × sizing_ratio   (fallback)
```

Where `sizing_ratio = stage_3_notional / stage_2_5_notional` (e.g., 75/50 = 1.5). `N_BAND_SAMPLES` proposed at 5 (slightly higher than the Gate-D `N=3` threshold so band derivation is well-anchored even when Gate-D evaluation is sample-thin).

Sources by precedence:
1. **Stage 2.5 live ledger** — `logs/hl_engine.jsonl` filtered to soak window, tagged hl_z, manual-cloid-excluded
2. **Replay calibration** — `autoresearch_gated/calibration_baseline_hl_truth_bucketed_3600.md` (bucketed-3600)

Sanity floor: `min_aggregate_pnl_usd` is ~50% of the loss-guard ($75 → -$37.50 absolute floor at Stage 2; $112.50 → -$56 at Stage 3).

### What needs to be built

- `scripts/derive_expectation_bands.py` — pulls replay calibration, writes
  `config/expectation_bands.json`
- One operator review pass before declaring Stage 3 active

**Estimated effort:** half a session, code-only.

---

## 3. MAX_NET semantic decision

### The observation (Stage 2.5)

`MAX_NET_NOTIONAL=$250` is currently an **entry-induced exposure gate**: it blocks new entries that would *grow* `|net|` past the cap, but does not force-reduce mark-to-market drift. In tonight's soak, `net_notional_before` drifted to $417 via MTM with the cap holding entries correctly.

At Stage 3 sizing ($75–$100), the same MTM drift produces ~50% larger excursions. The current semantic may not be acceptable at that scale.

### Three options

| Option | Behavior | Pro | Con |
|---|---|---|---|
| **(a) Hard exposure guard** | When `\|net\|` ≥ K × MAX_NET, auto-flatten the smallest positions until `\|net\|` ≤ MAX_NET | Tight upper bound on book exposure | Adds an order-path mutation; auto-flatten is itself a strategy decision; complex implementation |
| **(b) Adaptive throttling** | Scale down per-trade notional as `\|net\|` approaches cap (`size *= max(0, 1 - \|net\| / MAX_NET)`) | Smooth, doesn't introduce a flatten loop | Already in shadow via Kelly; but gating live size needs runtime-shadow path validation first |
| **(c) Reduce-only mode** | When `\|net\|` exceeds K × MAX_NET, all new entries are auto-converted to reduce-only until `\|net\|` is back under cap | Simplest semantic; never auto-flatten anything; mean-reversion strategy naturally drives |net| down | Slow recovery if signals are one-sided; can stay in reduce-only for hours |

### Recommendation: **(c) reduce-only mode**

Reasoning:
- Aligned with mean-reversion strategy nature: when book is one-sided + over-extended, taking only fading entries is the textbook mean-rev playbook.
- Lowest code complexity — single boolean flag in the entry path; no new flatten loop, no sizing math change.
- Fail-safe: if mean-reversion is not working in the regime, the book will stay over-cap rather than amplify by auto-flattening into the wrong side.
- Easy to reason about for ops: "we're at MAX_NET, so we're only taking fades right now."

**APPROVED K (operator 2026-04-28):** 1.5 — only kicks in if `|net|` > 1.5 × MAX_NET. Allows MTM breathing room without aggressive intervention.

**APPROVED hysteresis (operator 2026-04-28):**
- Activate reduce-only when `|net| > 1.5 × MAX_NET`
- Deactivate reduce-only when `|net| < 1.25 × MAX_NET`

The 0.25 × MAX_NET dead-band prevents flip-flopping at the boundary. Implementation note: track an explicit `_reduce_only_active: bool` flag on the engine; transitions log `risk_gate_reduce_only_activated` and `risk_gate_reduce_only_deactivated` events.

### What needs to be built

- One new gate in `_risk_gate_ok()` in `hl_engine.py` (~10 lines) — explicit code change required, in the risk path.
- New event: `risk_gate_reduce_only_active` (with `net_before`, `cap`, `k`).
- Update `MAX_NET_NOTIONAL` doc comment to clarify it's a soft cap, with reduce-only kicking in at K × cap.

**Estimated effort:** 1 session, **risk-path edit, requires explicit authorization.**

---

## Stage 3 acceptance checklist (proposed)

To promote Stage 2.5 → Stage 3 ($75–$100):

- [ ] Stage 2.5 end-of-soak report shows: 0 shadow failures, 0 loss-guard fires, 0 max-notional breaches, ≤ 1 manual intervention (the GOOGL earnings close, classified as justified)
- [ ] Gate D wired (`gate_d_eval.py` + integration)
- [ ] Per-symbol expectation bands declared (`config/expectation_bands.json` reviewed by operator)
- [ ] MAX_NET semantic decision implemented (recommend option **(c) reduce-only**)
- [ ] New launch script `scripts/relaunch_hl_engine_stage3_full_LIVE.sh` with $75 or $100 NOTIONAL_PER_TRADE_OVERRIDE, MAX_NET=$375, LOSS_GUARD=$112 (proportional)
- [ ] One re-soak at Stage 3 with the same 5-criterion Gate-D PASS

Anything in this checklist failing → stay at Stage 2.5 with the relevant fix, do not advance.

---

## Out-of-scope (Stage 4+)

Adaptive margin sizing remains Stage 4, gated separately by the instrumentation prerequisite in `project_dust_soak_promotion_ladder.md`. The shadow Kelly data being collected by Stage 2.5 is an **input** to Stage 4, not a Stage 3 dependency.

---

## Approved decisions (operator 2026-04-28)

1. ✅ **MAX_NET semantic:** option (c) reduce-only mode
2. ✅ **K = 1.5** activation, **1.25** deactivation (hysteresis dead-band)
3. ✅ **`min_round_trips_per_symbol = 3`** for Gate D, with **under-sampled = PENDING (not failing)**
4. ✅ **Band derivation:** Stage 2.5 live-first, replay-fallback (replay was already documented as ceiling-limited)

**Status:** ready for implementation planning **after Stage 2.5 end-of-soak report finalizes**, not before. No code changes tonight.
