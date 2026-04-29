# Quoter Shadow Telemetry — Task 25 Design (Gate 3)

**Status:** Wiring complete; awaiting forward soak data.
**Predecessor:** `docs/quoter_family_replay_markout_findings.md` (Gate 2D).
**Date:** 2026-04-29

This is the **design and runbook** for Gate 3, not a verdict. The verdict
will follow once the engine has run for a forward window long enough to
build a representative `quoter_shadow` event distribution. Run
`scripts/analyze_quoter_shadow.py` against `logs/hl_engine.jsonl` once
that data exists; the script's `acceptance` block applies the
four-criterion bar below.

## Why Gate 3 next (not Gate 2E)

The committed Task 24 doc named both Gate 2E (joint OBI+mid replay) and
Gate 3 (shadow telemetry) as defensible next moves. Operator chose
Gate 3 with the rationale: 2E is "tighten confidence" — Gate 3 advances
the actual readiness path toward a feature-flagged `_size_order()`. If
shadow distributions line up reasonably with the simulator, 2E becomes
optional. If they don't, 2E becomes the right intermediate debugging
bridge.

## What was wired

Three surgical additions; live engine behavior unchanged.

### 1. `strategy/quoter_shadow.py` (new file)
Pure observer. One function:
```python
shadow_quoter_payload(*, obi, intended_side_sign, notional_about_to_trade,
                     notional_held_signed, mid, ...) -> dict
```
Returns a dict suitable for log emission. Returns `{"shadow_status": "skipped_<reason>"}`
on invalid input. Never raises.

Synthetic framing inside the helper: at the engine's order-decision
boundary, treat the post-trade inventory as the principal of a fresh
600-second liquidation horizon, with `t=0` so `e_t = 0`. The quoter's
regime is then driven by the OBI signal alone — exactly the "what
would the quoter say right now given OBI?" telemetry Gate 3 needs.

### 2. `strategy/signals.py` (4 additive edits)
Added `"obi": round(st.obi, 4)` to each of the four return dicts:
- mean-reversion exit (line 573)
- mean-reversion entry (line 659)
- momentum exit (line 1329)
- momentum entry (line 1395)

Purely additive. No existing key removed or modified. Downstream
consumers ignore unknown keys.

### 3. `hl_engine.py` (one new try/except block)
At the existing `sizing_runtime_shadow` hook (line 2148+), after the
existing `log.info("sizing_runtime_shadow", ...)` call, a new isolated
try/except block computes `shadow_quoter_payload(...)` and emits a
**separate** `quoter_shadow` event. The existing event format is
unchanged — downstream tooling that reads `sizing_runtime_shadow` is
unaffected.

The shadow path:
- reads `sig.get("obi")` (now surfaced by the signals.py edit)
- reads the engine's signed inventory via `self._signals._state[sym].positions`
- calls the helper
- logs `quoter_shadow` with the result

Wrapped in its own try/except. Any failure in this block emits
`quoter_shadow_failed` and falls through to the order path; orders are
never blocked by telemetry failure.

## Event shape

```json
{
  "event": "quoter_shadow",
  "symbol": "BTC",
  "coin": "BTC",
  "tag": "hl_taker_z",
  "is_momentum": false,
  "side": "sell",
  "side_sign": -1,
  "intended_notional": 250.0,
  "held_notional_signed": 1500.0,
  "mid": 76420.5,
  "shadow_status": "ok",
  "shadow_regime": "touch",
  "shadow_order_type": "post_only",
  "shadow_side": "sell",
  "shadow_delta_a_bps": 0.0,
  "shadow_delta_b_bps": null,
  "shadow_clip_usd": 100.0,
  "shadow_ttl_s": 15.0,
  "shadow_e_t": -0.0,
  "shadow_u_t": 0.0,
  "shadow_reservation_price": 76414.4,
  "shadow_y_toxicity": 0.55,
  "shadow_post_trade_inventory_usd": 1250.0,
  "shadow_horizon_s": 600.0,
  "level": "info",
  "timestamp": "2026-04-29T..."
}
```

## Decision rule (operator-supplied)

Gate 3 clears if all four are true:

| # | Criterion | How the analyzer measures it |
|---|---|---|
| 1 | toxic windows show more TOUCH/CATCHUP than favorable | per-OBI-bucket regime distribution: `(touch + catchup) / n_total` for toxic > same ratio for favorable |
| 2 | shadow markout preserves the same broad ordering seen in replay/sim | `markout_by_scenario`: favorable >= neutral, toxic <= favorable (allowing 1 bps slack on neutral≥favorable) |
| 3 | scheduler miss / actual action distributions are sane | shadow event skip-rate < 20% of total |
| 4 | no pathological churn / dead zones / contradiction between quoter intent and actual engine behavior | contradiction count (shadow says non-PASSIVE but engine submits zero) < 5% of total |

The analyzer (`scripts/analyze_quoter_shadow.py`) emits PASS / FAIL /
INCONCLUSIVE per criterion plus an `all_pass` rollup. INCONCLUSIVE
means insufficient data (e.g., zero markout-resolvable fills) — keep
the soak running.

## How to read the output

After the engine has run for a forward window:

```bash
venv/bin/python3 scripts/analyze_quoter_shadow.py \
    --log logs/hl_engine.jsonl \
    --markout-horizon-s 60
```

The artifact `autoresearch_gated/quoter_shadow_distributions.json`
will contain:

- `regime_by_scenario`: regime mix per OBI-bucket-mapped-to-scenario,
  comparable to the simulator's `touch_step_fraction_mean` etc.
- `regime_by_symbol_top`: top 20 most-active symbols, regime mix per
  symbol — flags symbol-level pathology that would be hidden by
  pooled stats.
- `markout_by_scenario`: realized maker markout in bps over a 60s
  forward horizon, bucketed by (OBI, side)→scenario.
- `intended_clip_stats` / `shadow_clip_stats`: the engine's actual
  intended notional vs the quoter's recommendation. Big systematic
  divergence → flag.
- `obi_seen_stats`: distribution of OBI at decision points. If most
  decisions sit near 0, toxic-bucket sample size will be small and
  Gate 3 acceptance criterion 1 may register INCONCLUSIVE.
- `contradiction_counts`: shadow says non-PASSIVE but engine doesn't
  trade. Should be near zero in normal operation.
- `acceptance`: the four-criterion verdict.

## What this telemetry does NOT yet support

- **Joint OBI+mid replay.** The shadow uses the engine's own OBI feed
  paired with the engine's own mid. This is more truthful than Gate 2D's
  replay window, but the simulator-vs-shadow comparison still has one
  remaining gap: simulator OBI/mid evolution is independent (Gate 2D),
  while shadow OBI and shadow mid are naturally correlated (both come
  from the same live book). If shadow markout systematically deviates
  from replay markout in direction or magnitude, Gate 2E becomes the
  right next debugging step.

- **Continuous regime trace.** The shadow fires at order-decision
  moments only — not at every `signal_tick`. A continuous trace would
  reveal regime *transitions* (e.g., how often does the quoter would-have
  flipped PASSIVE↔TOUCH between two consecutive decisions). Out of scope
  for Gate 3; possible Gate 3.1 extension if needed.

- **Quoter override hooks.** Gate 3 is observation-only. Gate 4 will
  feature-flag a path where the quoter's recommendation can actually
  influence `_size_order()`. Until Gate 3 clears, that wiring stays
  unbuilt.

## Forward-soak operating notes

- The `quoter_shadow` event volume scales with the engine's entry
  signal rate. Roughly 1 shadow event per signal-driven order decision.
  At ~100 signals/hour across the 38-coin universe, expect ~2400
  events/day.
- A 24-48h soak is the minimum useful sample. A full week gives more
  robust regime-mix statistics, especially in the toxic bucket where
  OBI > +0.30 doesn't fire often.
- The analyzer is idempotent and read-only against the log. Run it as
  often as desired during the soak.

## Failure modes the analyzer should flag

- **Skip-rate > 20%**: most likely cause is that `obi` is missing from
  the sig dict. Check the four signals.py return-dict edits.
- **Toxic regime distribution looks like favorable**: either OBI sign
  convention drifted, or toxicity_threshold (0.30) is too high relative
  to the live OBI distribution. Compare against `obi_seen_stats`.
- **Markout INCONCLUSIVE for all scenarios**: not enough fills with
  resolvable future-mid. Either soak is too short, or the analyzer's
  mid-source fallback (entry_signal.limit_px / exit_signal.limit_px)
  is too sparse. Considering augmenting with `signal_tick` mid (would
  require adding mid to signal_tick, which is a separate ask).
- **Contradiction count > 5%**: shadow says quoter would act, engine
  submits zero. Check that `intended_notional` is set on the sig dict
  for the cases where shadow regime != HOLD.

## Gate status

| Gate | Status |
|---|---|
| Gate 1: family sweep, simple model | CLEARED |
| Gate 2A: TWAP × microstructure_v1 | CLEARED |
| Gate 2B: family sweep × microstructure_v1 | CLEARED |
| Gate 2C: AR(1) OBI calibrated to live data | CLEARED |
| Gate 2D: replay-driven realized markout | CLEARED |
| **Gate 3: shadow telemetry** | **WIRED, awaiting soak** |
| Gate 2E (optional): joint OBI+mid replay | not started; do only if shadow exposes mismatch |
| Gate 4: feature-flagged `_size_order()` | not started |

## Files committed

- `strategy/quoter_shadow.py` (new, pure observer)
- `strategy/signals.py` (additive: `obi` key on 4 return dicts)
- `hl_engine.py` (additive: 1 new try/except block at sizing_runtime_shadow hook)
- `scripts/analyze_quoter_shadow.py` (new, analyzer)
- `docs/quoter_shadow_design.md` (this file)

`_size_order()` and the live maker path remain frozen. Stashes parked.
