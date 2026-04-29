# Execution-Calibration Sketch — `(β, σ, η)` from existing logs

**Status:** DESIGN ONLY. No code, no live integration. Read-only data sketch for future authorization.

**Goal.** Produce three per-symbol parameters that the Bechler-Ludkovski `α*(t,x,Y)` closed-form (1409.2618) needs:
- `β` — OFI mean-reversion rate (1/seconds)
- `σ` — OFI driving-noise vol
- `η` — temporary linear price impact ($ per unit traded notional)

These plug into the LQ Riccati ODE that replaces the current fixed `NOTIONAL_PER_TRADE / price` rate. **Tonight's deliverable is the design; the runner is a separate authorized PR.**

---

## Sources

| Parameter | Where the data lives | Event | Field |
|---|---|---|---|
| `β`, `σ` | `logs/hl_engine.jsonl` | `signal_tick` | `obi` (the ρ value our engine logs every L2 update) |
| `η` (slippage proxy) | `logs/hl_engine.jsonl` | `hl_fill_received` + matching `hl_order_submitted` | `px` (fill) − `limit_px` (order) per side |
| Side reference | `logs/hl_engine.jsonl` | both events | `side` |

Per-symbol partition: keys are the bare-form symbol (`BTC`, `xyz:GOOGL`), to match the band/universe convention.

---

## β, σ — OFI as scalar OU

**Model:** `dY = −β(Y − Ȳ) dt + σ dW` per symbol, with `Y = obi ∈ [-1, 1]`.

**Estimator** (per-symbol, on the soak window):
1. Build a uniform-time-grid resample of `obi` series at Δt = 1s using last-observation-carried-forward.
2. Compute the AR(1) regression: `Y_{t+Δt} = a + b · Yₜ + εₜ`.
3. `β̂ = -ln(b) / Δt`,  `σ̂ = std(εₜ) · √(2β̂ / (1 - exp(-2β̂Δt)))`.
4. Reject the fit if `|b| ≥ 1` (non-stationary) or `b ≤ 0` (over-damped). Fall back to class-default for those symbols.

**Class defaults** (when fit fails or sample-thin, < N_OBI_SAMPLES = 600):
- HIP-3 equity perps: `β = 0.05` (≈20s half-life), `σ = 0.20`
- Native crypto: `β = 0.10` (≈7s half-life), `σ = 0.30`

---

## η — temporary impact

**Model:** the `LIMIT_SLIPPAGE = 0.0010` constant in `config/risk_params.py` is an *upper bound* used to validate orders, not a calibrated impact. We can do better from realized fills.

**Estimator** (per-symbol):
1. For each `hl_fill_received` event, find the matching `hl_order_submitted` (by `cloid` if present, else nearest-prior on same symbol/side).
2. Compute signed slippage: `slip_bps = ±(fill_px - sent_px) / sent_px × 10000` (sign so positive = adverse).
3. Aggregate: `η̂ = median(slip_bps) / sent_qty_dollar_notional` per symbol.
4. Filter outliers > 5σ (book-walk events that aren't representative).

**Caveat:** crypto LOB temp impact is empirically square-root, not linear. The linear `η` is a first-order approximation acceptable for Bechler-Ludkovski. For Abi Jaber/Neuman/Tuschmann (2403.10273) or the nonlinear Fredholm (2503.04323), this needs replacing with a nonlinear `h(·)` fit — out of scope for the linear-LQ closed-form.

---

## Output artifact

`config/execution_params.json`:

```json
{
  "schema_version": 1,
  "fitted_at": "<UTC ISO>",
  "sample_window": {"start": "...", "end": "..."},
  "obi_resample_dt_s": 1.0,
  "min_obi_samples": 600,
  "min_fill_pairs": 5,
  "params": {
    "BTC":        {"beta": 0.118, "sigma": 0.41, "eta_bps_per_dollar": 0.00012, "n_obi": 14000, "n_fills": 9, "fit_status": "live_full"},
    "xyz:GOOGL":  {"beta": 0.085, "sigma": 0.32, "eta_bps_per_dollar": 0.00021, "n_obi": 8200,  "n_fills": 1, "fit_status": "live_thin_eta"},
    ...
    "xyz:HIMS":   {"beta": 0.05,  "sigma": 0.20, "eta_bps_per_dollar": null,    "n_obi": 0,     "n_fills": 0, "fit_status": "default_class"}
  },
  "_provenance": {
    "n_universe": 96,
    "n_with_full_obi_fit": 4,
    "n_with_default_obi": 92,
    "n_with_eta_fit": 8,
    "n_with_default_eta": 88
  }
}
```

`fit_status` values: `live_full` / `live_thin_obi` / `live_thin_eta` / `default_class`.

---

## Replay slippage penalty (granularity-gap mitigation)

**Problem.** Live engine signals fire on 1-minute bars (`NATIVE_BAR_INTERVAL_S = 60` at hl_engine.py:111). Replay infrastructure (`scripts/z_entry_replay.py`, `z4h_exit_replay.py`, `ratchet_replay.py`, `hmm_window_replay.py`, `train_*`) reads from `data/cache/bars.sqlite` at **15-minute and 1-hour intervals only** (line 110: `for iv in ("15m", "1h")`). Replay also reads the `c` (close) field only — no intra-bar realism.

**Consequence.** Any `(β, σ, η)` calibrated against replay-derived prices will **systematically underestimate execution friction**: a 1m signal that fires at $X may actually fill at the worst price within the corresponding 15m bar (often $X ± 0.3–0.8% on thin HIP-3 books). Replay reports flat fills at $X. The Riccati boundary conditions, especially terminal-penalty `α`, are calibrated against this optimistic mark and will produce trade rates more aggressive than the venue actually supports.

**Synthetic penalty (band-aid, not cure).**

For each replay fill, charge a microstructure-slippage penalty against the modeled fill price:

```
slip_penalty_bps = K_SLIP × (high - low) / close × 10000
slip_penalty_dollars = slip_penalty_bps × notional / 10000
```

Where `K_SLIP ∈ [0.25, 0.75]` represents what fraction of the bar range you charge against each fill. Suggested defaults:

- `K_SLIP_TAKER = 0.50` — a taker entry/exit at random within the bar pays half the bar range on average
- `K_SLIP_MAKER = 0.20` — a maker fill is closer to the touch but still subject to mid-walk during the bar
- `K_SLIP_ESCALATED = 0.75` — when `hip3_taker_escalation` fires, charge most of the bar range

**Application sites:**
- `scripts/z_entry_replay.py` line ~197 (computing `adverse`): subtract penalty before STOP_LOSS_PCT comparison
- `scripts/z4h_exit_replay.py`: same pattern at exit
- Any future runner that calibrates `η` from synthetic replay fills MUST include the penalty in the loss function

**This is a band-aid.** It approximates microstructure cost from a coarse mark; it does not fix the granularity gap, and it can only narrow the calibration error, not eliminate it.

---

## ⛔ HARD BLOCKER: 1-minute historical bar ingestion

Before any Riccati output is trusted for live capital allocation, `data/cache/bars.sqlite` MUST be populated with **1-minute** bars matching `NATIVE_BAR_INTERVAL_S = 60`. The slippage-penalty band-aid above does NOT substitute for this — it merely makes interim replay outputs less wrong.

**Acceptance criteria for the 1m data upgrade:**
1. `bars.sqlite` schema gains rows where `interval = "1m"` for every symbol in `config/stage3_universe.json`
2. Coverage: minimum 30 days back, ideally 90 days where the venue allows
3. Storage retention check: HL `candleSnapshot` retention at 1m is ~3.6d empirically (per `hl_pairs_discover.py` comment line 92), so the historical pull will need to be done in rolling chunks or via a different data source
4. The replay scripts that currently iterate `for iv in ("15m", "1h"):` get a `1m` arm added (or made the default for `(β, σ, η)` calibration specifically)
5. Re-run baseline z_entry replay against 1m data and verify the per-symbol PnL deltas are within tolerance of live observed; if they diverge, the divergence itself is a more honest measurement of execution friction than synthetic penalties can capture

**Status:** UNBLOCKED for the *exploratory* calibration runner with synthetic slippage. **BLOCKED** for any output that gets fed into a live `_size_order()` change.

The exploratory runner — read-only `(β, σ, η)` fit on Stage 2.5 + Stage 3 live data — does not depend on `bars.sqlite` at all (it reads `signal_tick.obi` and fill ledger from `hl_engine.jsonl`), so this blocker does not delay step 1 of the BL integration. It DOES delay any subsequent step that uses replay for empirical rescaling.

---

## Non-goals tonight

- **No** runner script. The above is the spec; the implementation needs explicit authorization since it's the precursor to an order-logic change.
- **No** Riccati ODE solver. That's a separate doc + script, gated by these calibration outputs.
- **No** swap of `_size_order()` in `strategy/signals.py`. CLAUDE.md change-discipline applies: "do not restructure the signal pipeline ... when asked to fix a specific bug." Bechler-Ludkovski is a strict generalization of fixed sizing; even framed as "additive," the integration touches both signal evaluation and execution paths.
- **No** 1-minute bars.sqlite hydration. Separate authorized scripted task.

## Open questions for the operator

1. Soak window for fitting: default to Stage 2.5 + Stage 3 union? Or Stage 3 only (cleaner but ~2h of data)?
2. `min_obi_samples` threshold: 600 (10 min at 1Hz) or 3600 (1h)?
3. `min_fill_pairs` for `η`: 5 (matches the design doc) or higher?
4. Should `η` be linear-bps or square-root-fit? Linear is enough for Bechler-Ludkovski; 2503.04323 needs concave `h`.
5. Per-symbol vs per-class outputs: per-symbol with class fallback (current sketch) is right for the closed-form; per-class only would simplify the table but lose the symbol-specific edge.
