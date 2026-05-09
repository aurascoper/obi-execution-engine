# Post-soak roadmap — Book A engine, Gate D and beyond

**Status:** drafted 2026-05-09 mid-soak (T0=2026-05-06T01:09Z, ~4 days remaining on a 7-day continuous Gate D).
**Scope:** sequential phases for what happens after the current expanded-envelope soak completes — not mid-soak guidance.
**Operating rule throughout:** no parameter changes mid-phase. Each phase produces a frozen artifact (run summary, calibration, code SHA) before the next phase begins.

---

## Phase 0 — Current run completes (target ~2026-05-13T01:09Z)

**Object under test:** the **expanded-envelope, no-entry-cap-brick** configuration.
- `MAX_NEW_ENTRIES_PER_SESSION=100000`
- `HL_SESSION_LOSS_GUARD_USD=$200`
- watchdog `MAX_DAILY_LOSS=$200`
- $50 cap / $12 floor / unified-account routing
- universe restricted to USDC lane (native HL + xyz HIP-3)

**This run answers:** does the engine stay continuously live without self-bricking on attempt count? **Not** whether it has edge under the original conservative envelope.

**No mid-soak changes.** Only safety-critical interventions allowed (e.g. watchdog SIGTERM is fine; manual parameter retuning is not).

**Frozen artifacts at completion:**
- run summary: T0, T_end, total uptime, fills, realized + unrealized PnL, drawdown, max-buffer-compression events
- code SHA at start + end
- config snapshot (env.sh, launcher, signal params)
- watchdog log
- engine jsonl (compressed)

---

## Phase 1 — Continuity verdict

**Question to answer:** did the engine stay live, did it generate normal trade flow, did watchdog/loss-guard never trip?

**Pass criteria (all must hold):**
- 7 continuous days uptime (no operator restart, no power-loss reset)
- ≥ N fills per day (N TBD from Phase 0 observed rate; sanity floor)
- watchdog never tripped
- `HL_SESSION_LOSS_GUARD_USD` never tripped
- no rejection-loop death (no instance of MAX_NEW_ENTRIES being silently pegged)

**If pass:** proceed to Phase 2. Continuity question answered.
**If fail:** debug specific failure; re-run continuity soak before proceeding.

**Explicit non-goal:** Phase 1 does NOT establish edge. A net-positive watchdog delta during Phase 0 is *suggestive*, not statistically meaningful at this sample size or under this loosened envelope.

---

## Phase 2 — Conservative rerun (Gate-D-comparable)

**Goal:** establish whether the engine has edge under the original conservative envelope — the *real* Gate D question.

**Configuration changes vs Phase 0** (revert envelope only):
- `MAX_NEW_ENTRIES_PER_SESSION=20`
- `HL_SESSION_LOSS_GUARD_USD=$50`
- watchdog `MAX_DAILY_LOSS=$50`
- everything else unchanged

**Do not roll back:**
- watchdog code-fix (`user_state(addr, dex=)` + spot USDC sum) — independent of envelope
- min-notional pre-submit gate in `strategy/signals.py` — prevents the 2026-05-05 JP225 reject loop
- any other code-level fixes accumulated during Phase 0

**This run answers:** does the engine have edge inside the conservative loss/attempt envelope?

**Same 7-day continuous standard.** Same frozen artifacts.

**Concatenation policy:** not by default. If used, must satisfy:
- same code SHA across halves
- same config hash
- no position mutation between halves
- interruption < 5 minutes
- documented reason
- explicit epoch ledger written to `docs/concatenation_ledger.md`

Without that ledger, treat any interruption as a reset.

---

## Phase 3 — Transient impact decay fit

**Prerequisite:** Phase 2 complete (need real fills under known envelope to fit decay parameters).

**Goal:** measure the transient market-impact decay kernel from the engine's own fills.

**Inputs:**
- per-fill records: cloid, submit_ts, fill_ts, sent_px, fill_px, qty, side, mid-at-submit, mid-at-fill, mid-at-+30s, mid-at-+5m
- existing `fill_observation` telemetry (shipped 2026-05-03 per `project_fill_observation_telemetry.md`) is the data source

**Deliverable:** `config/impact_kernel.json` — per-symbol or per-asset-class transient-impact decay parameters (η, β, half-life).

**Method:** linear regression of post-fill markout against fill size, with exponential decay term. See `scripts/calibrate_ofi_params.py` for the existing AR(1)/η pattern; extend rather than rewrite.

**Success criterion:** non-trivial fit on ≥ 2 symbols with R² ≥ 0.3. If kernel cannot be fit (too noisy, too few fills), block Phase 4.

---

## Phase 4 — Closed-form OU signal + transient impact sizing

**Prerequisite:** Phases 1–3 complete. Conservative envelope edge established. Impact kernel fit.

**Goal:** replace fixed sizing with closed-form optimal rate from the **Lehalle-Neuman 2017** setup.

**Anchor paper:** arxiv 1704.00847. Maps directly to our calibrated OBI AR(1). Supersedes BL 2014 as the operational anchor (per `reference_lehalle_neuman_signal_execution.md`).

**Components to wire:**
- OBI AR(1) calibration (already done, `scripts/calibrate_obi_ar1.py`)
- Transient impact kernel from Phase 3
- Closed-form rate solution combining the two

**Implementation target:** `strategy/optimal_rate.py` (already partially scaffolded per memory — extend, do not rewrite).

**Do not use** the synthesized form `v* = a⁻¹[C(t)Q + (ET−X)(S−θ) + D(t,μ)]` as if it were canonical literature. It's a teaching synthesis combining Lehalle-Neuman + Cartea-Jaimungal pieces; useful for explanation, not for citation. The actual closed form is in the LN paper.

**Validation:** shadow-only initially. Compare LN-suggested sizing against current fixed sizing on the same fills. Promote to live only after a separate continuity soak under the new sizing.

---

## Phase 5 — State-dependent α*(t, x, Y) via Fredholm propagator

**Prerequisite:** Phase 4 complete and live. Edge confirmed under closed-form sizing.

**Goal:** replace closed-form sizing with state-dependent rate function that uses inventory `x`, time-to-end-of-window `t`, and a richer signal state `Y`.

**Literature path:** Bechler-Ludkovski + Abi Jaber Fredholm propagator (per `project_execution_research_direction.md`).

**Why this is later:** the propagator approach is the right *target* for production-grade execution but has more moving pieces (kernel choice, state dimensionality, calibration of Y dynamics beyond AR(1)). Stand on Phase 4's shoulders rather than skipping ahead.

**Deliverable:** `α*(t, x, Y)` calibrated from our own data, replacing the closed-form rate with a time-and-state-dependent function. Lives alongside, not in place of, Phase 4 — Phase 4 stays as fallback.

---

## What this roadmap deliberately does NOT do

- ❌ tune maker offset/lifetime in the same breath as the soak verdict (parked separately in `project_maker_tuning_parked.md`; tighten-first / widen-second; revisit only after Phase 2 confirms edge under conservative envelope)
- ❌ skip the conservative rerun in favor of "the expanded soak was good enough" — it isn't, by design
- ❌ jump directly to Phase 5 without Phase 4 closed-form as the bridge
- ❌ change risk-path code (CLAUDE.md rule) outside of the explicit envelope reverts in Phase 2

---

## One-line summary

Run Phase 0 → 1 verdict → Phase 2 conservative rerun → impact kernel → closed-form sizing → propagator upgrade. Each phase frozen before the next. No parameter drift between phases.
