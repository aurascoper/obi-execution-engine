# GPT-5.5 Deep Research Prompt — Lift replay-vs-live ρ from 0.17 to >0.75

> Paste the entire content below into a fresh GPT-5.5 deep research session. The
> prompt is self-contained: it includes the domain, the harness, what's been
> tried, what failed, the data available, and the explicit research goal.

---

## Role and goal

You are a quantitative research engineer asked to design a measurement and
modeling plan that lifts the **replay-vs-live Pearson correlation (ρ)** of a
crypto perpetuals trading harness from its current value of **0.17** to
**at least 0.75**, ideally to the 0.80 target gate documented in the
production system.

The output we want is **a ranked, executable research plan** with:
1. Falsifiable hypotheses (what's broken, why) for each gap.
2. Concrete code or methodology changes (file-level granularity, no hand-waving).
3. **Predicted ρ-lift** per change AND a calibration step to verify each
   prediction before committing to >1 day of implementation.
4. An explicit acknowledgement of where prior model-based ρ-lift forecasts
   were wrong (we have a worked example below).

You have NO access to the codebase or data — work from the inlined
material. Where you propose a code change, write it as pseudocode keyed to
the file/section names below, and identify the specific data point that
would falsify your hypothesis.

---

## System summary

**What the production system is**: a 38-coin LIVE Hyperliquid trading
engine running mean-reversion z-score entries, with a momentum overlay,
a shock-ratchet exit trail, and various risk gates (OBI, trend SMA,
flip-guard, momentum-dedup, regime pause, net-cap). Native crypto coins
trade on the main HL clearinghouse; HIP-3 equity perps trade on a
separate clearinghouse (xyz, vntl, hyna, flx, km, cash, para subaccounts).

**What the replay harness does**: simulates per-symbol mean-reversion
trades against historical 1-min bar closes, applying a chain of entry
gates (G1..G5) and exit conditions (X1 z_revert, X2 ratchet, X3
time_stop, X4 stop_loss). Outputs `SCORE = sum of simulated PnL`
per symbol and aggregated. The harness is used as the eval target for
autoresearch loops that tune various parameters.

**Why ρ matters**: autoresearch can find replay-improving parameters,
but parameters that improve replay should also improve live PnL. If
replay isn't well-correlated with live, autoresearch winners may
*hurt* live performance. The current calibration project is blocking
several promotion decisions (e.g. `OBI_THETA=0.30` showed +$1227 in
replay but is blocked at "paper soak only" due to ρ=0.17).

---

## The replay harness — `scripts/z_entry_replay_gated.py`

**Methodology** (per-symbol, deterministic):

For each symbol with ≥50 historical 1-min ticks:
1. Walk the symbol's `signal_tick` stream from `logs/hl_engine.jsonl`.
   Each tick has `(ts_ms, z, obi, z_4h)`. Marks come from a separate
   1-min/15-min bar cache at `data/cache/bars.sqlite`.
2. At each tick, if no position is open, evaluate:
   - **G0 regime_pause** (optional): if BTC or xyz:SP500 1h |return| ≥
     `REGIME_1H_ABS_RETURN` (default 0.010) within last
     `REGIME_PAUSE_SECONDS` (default 10800), block all entries.
   - **G1 obi_gate**: `direction × obi > OBI_THETA` (default 0.0).
   - **G2 trend_regime**: long requires close ≥ 240-bar SMA; short
     requires close ≤ SMA.
   - **G3 flip_guard**: block immediate reopen of opposite-side trade
     after a close (per-symbol last_closed_side, cleared after one tick).
   - **G4 momentum_dedup**: block mean-reversion entry while `|z| ≥
     Z_MOMENTUM_ENTRY=1.25` (live engine routes those to momentum tag).
   - **G5 z_threshold**: long if z ≤ Z_ENTRY (default −1.25); short if
     z ≥ Z_SHORT_ENTRY (default +1.25). Per-symbol overrides exist.
3. If all gates pass, open at `mark_at(ts)` with fixed
   `NOTIONAL_PER_TRADE = $750`, `qty = $750 / mark`.
4. While in position, evaluate exits each tick:
   - **X1 z_revert**: z back through `Z_EXIT` (default −0.5) for longs,
     `Z_EXIT_SHORT` (+0.5) for shorts. Gated by:
     - `MIN_HOLD_FOR_REVERT_S=60` (deployed via prior autoresearch)
     - `MIN_REVERT_BPS=0.001` (0.1% favorable move)
     - Per-symbol `Z4H_EXIT` map for "patient hold" coins (block z_revert
       unless |z_4h| ≥ symbol-specific threshold, typically 5-10).
   - **X2 ratchet**: armed at `|z_4h| ≥ SHOCK_ARM=3.5`. Three tranches
     fire as z_4h retraces from peak by `RETRACE_STEP=0.005` increments.
     For replay simplicity, full position closes on first tranche fire
     (worst-case attribution).
   - **X3 time_stop**: age ≥ 3600s.
   - **X4 stop_loss**: adverse move ≤ −1%.
5. Sum signed PnL across all closes.

**Output**: `SCORE: $X` and a `GUARD_WORST_SYM_DELTA` per-symbol
regression check.

## The validation harness — `scripts/validate_replay_fit.py`

Runs the replay over the same window as production logs, then computes:

- **Per-symbol live PnL**: aggregated from `exit_signal` events in
  `logs/hl_engine.jsonl` (`pnl_est × qty × direction`).
- **Per-symbol replay PnL**: from the harness's per-symbol output.
- **Portfolio Pearson ρ**: across the 81 symbols in the live ∩ replay
  intersection.
- **Per-symbol ρ**: bucketed by day, requires ≥5 active days per symbol.

**Promotion gate**: portfolio ρ ≥ 0.80 AND every active symbol's ρ ≥
0.70. Below 0.50 per symbol indicates a "missing gate or stateful
effect."

---

## Current state (most recent run, 14d window)

```
window: [t-14d .. now]
live symbols: 83
replay symbols: 97
overlap: 81
portfolio rho: 0.1724
GATE: FAIL (< 0.80)
live total: $+481.39
replay total: $-1296.69
diff: $-1778.09
```

**Sample of per-symbol disagreement** (sorted by largest live $):

| symbol | live $ | replay $ | sign agree? | ratio (replay/live) | live active days |
|---|---:|---:|---|---:|---:|
| xyz:MSTR | +596.06 | +20.46 | ✓ | +0.03× | 4 |
| ETH | +222.73 | −41.47 | ✗ | −0.19× | 10 |
| xyz:INTC | −110.97 | −88.10 | ✓ | +0.79× | 4 |
| DOGE | −68.99 | −49.62 | ✓ | +0.72× | 4 |
| xyz:AMZN | +47.69 | −36.45 | ✗ | −0.76× | 4 |
| RENDER | −7.50 | +19.36 | ✗ | −2.58× | 2 |
| AAVE | −4.08 | −154.99 | ✓ | +37.94× | 2 |
| TAO | −3.30 | −41.82 | ✓ | +12.68× | 3 |
| hyna:IP | −1.01 | −59.78 | ✓ | +59.02× | 1 |

**Observations from this table**:
- Largest live winner (xyz:MSTR +$596) is barely captured in replay (+$20).
- ETH, xyz:AMZN, RENDER, ENA, CRV, SUI, xyz:CL all have **sign-flipped**
  replay vs live.
- Many sign-matched cases have wildly wrong magnitudes (AAVE 38×,
  hyna:IP 59×, xyz:SKHX 24×).

---

## What's already been tried — Phase A (negative result)

A previous validation report predicted that adding "cross-symbol
MAX_NET_NOTIONAL=$200 timeline modeling" (the live engine has a
portfolio-level signed-notional cap that the per-symbol replay can't
enforce) would lift ρ from 0.17 → 0.40.

**Implementation**: a `simulate_portfolio_gated()` function that did a
unified timeline walk across all symbols with shared portfolio state,
adding a G6 net_cap gate. Verified backward-compatible: at non-binding
cap, output matched legacy per-symbol output exactly.

**Sweep result** (NOTIONAL = $80 fixed, sweep MAX_NET_NOTIONAL):

| MAX_NET | ρ |
|---:|---:|
| $100 | −0.08 |
| $200 (live's actual setting) | 0.10 |
| $500 | 0.19 |
| **$1000** | **0.23** ← peak |
| $2000 | 0.18 |
| $5000 | 0.15 |
| ∞ (baseline, no G6) | 0.17 |

**Phase A made ρ WORSE at the live cap value, and the sweep peak was
+0.06 above baseline at a cap value that doesn't match live.** The
predicted +0.23 lift didn't materialize.

**Diagnosed root cause**: the replay's *candidate set* is ~11× larger
than live's *post-filter set*. Live runs net_cap **after** OBI / trend /
momentum / flip-guard gates have pre-filtered most candidates. By the
time entries reach net_cap in production, only ~2K make it over 14d.
The replay's `simulate_portfolio_gated()` ran net_cap on a much larger
candidate set (~23,500 blocks at $200 cap, 11× live's count), suppressing
entries that live wouldn't have evaluated for net_cap. The replay's
position-trajectory diverges from live's, the wrong entries get blocked,
sign-mismatches grow, ρ falls.

**Lesson**: closing one structural gap doesn't proportionally lift ρ;
sometimes it widens new mismatches.

---

## What's available as data

- **`logs/hl_engine.jsonl`** (~1.3M lines): structured log with events
  including `signal_tick`, `entry_signal`, `exit_signal`,
  `risk_gate_*`, `hl_fill_received`, `regime_pause_active`, `meta_pick`,
  etc. Per-event timestamps, symbols, z, z_4h, obi, pnl_est, etc.
- **`logs/shock_ratchet.log`**: per-symbol ratchet arming + tranche fires
  with peak z_4h, qty, fill price.
- **`logs/hl_pairs.jsonl`**: separate pairs strategy (out of scope for
  this question).
- **`data/cache/bars.sqlite`**: 15-minute and 1-hour OHLC bars for ~97
  symbols, going back ~90 days.
- **HL public API**: `info.user_fills_by_time`, `info.user_funding_history`,
  `info.user_state` — can be called for ground-truth fill/funding data.
- **`exit_signal`** events have `pnl_est, symbol, direction, qty,
  entry_px, exit_px, reason`. They're the live PnL ground truth.
- **`config/z_entry_params.json`**, `config/gates/*.json`,
  `env.sh` — strategy parameters.

---

## Hypotheses to consider (rank or extend)

These came up in the postmortem; you should evaluate them and propose
others:

1. **Live's candidate set is heavily filtered upstream by gates the
   replay reproduces poorly.** The OBI gate may be miscalibrated in
   replay (uses 20-level orderbook in live, replay uses logged scalar
   `obi`). If replay's OBI gate fires fewer / different events, the
   downstream gate ordering produces different blocked-vs-fired
   distributions.
2. **Per-symbol notional sizing is variable in live but fixed in
   replay.** Live trades range $20-$200 per fill (per `qty × mark`
   on observed `hl_fill_received` events); replay uses $750.
3. **Funding PnL is realized live but absent from replay.** HL native
   perps charge ~0.01% per 8h; HIP-3 perps charge separately.
   Symbols held for hours accumulate non-trivial funding.
4. **Maker-vs-taker fill alpha unmodeled.** Replay marks at mid; live
   captures spread on Alo-only orders (~20% of fills) and pays it on
   IOC fills (~80%). 10-20 bps of alpha unmodeled per fill.
5. **Position-trajectory dependence not captured.** Live engine has
   pre-window open positions inherited at the moment the replay starts;
   replay assumes flat at window start. Symbols with held-through
   positions (TAO, ZEC) systematically skew live > replay.
6. **Momentum-tag direction routing.** At `1.25 ≤ |z| < 3.0`, live
   routes momentum entries OPPOSITE to mean-rev direction (long when z
   is high). Replay enters mean-rev only. We modeled this with
   `Z_MOMENTUM_ENTRY=3.0` thresholding but it's incomplete.
7. **Regime pause gate just retuned (thr=0.010, pause=10800)** — the
   replay's G0 implementation may not match live's _RollingBuffer
   60-bar 1m semantics exactly (replay uses 4 × 15-min bars).
8. **Cross-symbol netting at the basket level.** When `SIGNAL_MODE=
   basket_residual`, live z is computed against a basket residual;
   replay uses raw close-vs-SMA z. Unclear whether this is currently
   enabled.
9. **Live's exit attribution differs from replay's.** Live's `exit_signal`
   reasons include `z_revert`, `stop_loss`, `time_stop`, `trend_break`,
   `z4h_exhaustion`, `z4h_patient_exit_*`. Replay's exits cover X1-X4
   but the relative shares may diverge — symbols where live exits
   "z4h_patient" but replay exits "stop_loss" produce different PnLs.

---

## Specific deliverable we want from you

Produce a research plan with the following structure:

### 1. Hypothesis ranking

Rank the hypotheses above (and any new ones you propose) by expected ρ
contribution. For each, state what data point would falsify it (e.g.
"if Hypothesis 5 is correct, symbols with `entry_ts < window_start_ts`
should account for >X% of live-replay magnitude diff").

### 2. Smallest measurement first

For each top-ranked hypothesis, design a **30-minute measurement** to
test it before building. Examples:

- "Compute the share of live PnL attributable to `entry_ts < window_start`
  positions. If <10%, deprioritize Hypothesis 5."
- "Recompute live's per-fill notional distribution and replace replay's
  $750 with the median. Re-run validate_replay_fit. ρ delta tells you
  Hypothesis 2's contribution."

The goal is to **measure each hypothesis cheaply before committing
multi-day work**, exactly the lesson from Phase A's failure.

### 3. Sequencing plan

After measurements rank the hypotheses by *measured* contribution,
propose an ordering to apply fixes. Consider:
- Diminishing returns / interactions (fix A may make B's contribution
  smaller).
- Validation runtime (each fix should be re-validated before adding the
  next).
- Risk of "Phase A repeat" — fixes that look directionally helpful but
  make ρ worse on certain axes.

### 4. Stop conditions

Specify when to stop:
- ρ ≥ 0.75 reached.
- Two consecutive fixes deliver < +0.02 ρ each (diminishing returns).
- Total invested time exceeds 5 working days without crossing 0.50.

### 5. What to avoid

Document anti-patterns we've already learned:
- Trusting validation_report.md ρ-lift forecasts as measured fact.
- Adding a downstream gate without first verifying the upstream
  candidate-set distribution matches live.
- Modeling the wrong granularity (per-tick vs per-event).

---

## Tone and format

- Skeptical and measurement-first. Quantify everything you can.
- File-level granularity. Every code change should reference
  `scripts/z_entry_replay_gated.py:<lineish>`,
  `scripts/validate_replay_fit.py:<lineish>`, or a new file at a
  named path.
- Prefer additive harness changes (new optional gate, new metric) over
  invasive refactors (timeline walk).
- Estimate effort in hours; calibrate vs the Phase A baseline (timeline
  walk + sweep took ~5 hours; produced negative result).
- Be explicit when a hypothesis cannot be tested with available data
  and what NEW data would be required.

---

## Background reading inlined (for context completeness)

### `autoresearch_gated/phase_a_postmortem.md`

(Excerpted above under "What's already been tried.")

### `autoresearch_gated/validation_report.md` — the structural gaps it lists

```
1. Cross-symbol MAX_NET_NOTIONAL=$200 gate (hl_engine.py:912-931).
   ~2K live risk_gate_net_cap rejections; replay cannot enforce
   because it processes symbols independently.

2. Momentum-tag direction routing. At 1.25 <= |z| < 3.0, live routes
   momentum entries opposite to mean-rev direction; replay enters
   mean-rev. Partially addressed by Z_MOMENTUM_ENTRY=3.0 gate but
   still a gap below.

3. Maker-vs-taker fill alpha. Live captures fee rebates + better
   fills via Alo orders; replay uses mid-price. Estimated ~10-20 bps
   of alpha unmodeled.
```

### Z entry replay's own honest assessment

From `autoresearch_z_entry/final_report.md`:

> "Under pure z-threshold gating with no OBI / trend / shock-ratchet /
> momentum gates, the current universe replay is net-negative at every
> participation level below z≈10. Only 1 of 97 symbols (xyz:COST,
> +$3.08) was net-positive at the live baseline threshold."

This means the strategy's edge **isn't in the z-thresholds**; it's in
the **gates around them**. Lifting ρ requires getting the gate
behavior right, not the entry-threshold logic.

---

## Final note

We are open to negative findings. If your analysis suggests ρ ≥ 0.75 is
infeasible without architectural changes (e.g. switching to event-driven
audit-replay rather than candidate-driven predictive replay), say so
explicitly. We'd rather know that and pivot than spend two weeks chasing
a target that the data structure forbids.

Begin the deep research with hypothesis ranking. Quantify wherever
possible.
