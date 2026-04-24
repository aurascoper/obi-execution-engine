# Autoresearch — POW-dTS-Beta bandit discount tuning

## Goal

Maximize `SCORE` from `scripts/meta_discount_eval.py` by tuning the
`discount` default in `strategy/meta_controller.py`. The discount is the
time-decay applied to BOTH α and β across ALL arms on every update —
controlling how fast the posterior forgets.

## System context

`POWdTSBandit` (Polya-Urn-Weighted discounted Thompson Sampling, beta
posterior; arXiv 2507.18680) routes `select()` → arm tag for the live
engine's strategy router. Arms here: `hl_z` (mean-reversion) and
`momentum`. Reward is Bernoulli on `pnl_est > FEE_ROUND_TRIP_BPS / 100`.

Discount γ ∈ (0, 1]:
- γ = 1.0 → no forgetting; posteriors accumulate forever.
- γ = 0.995 (current default) → ~half-life around 138 updates.
- γ = 0.95 → aggressive forgetting; recent signal dominates.

## Constrained file

**`strategy/meta_controller.py`** — but you may ONLY change line 44:

```python
discount: float = 0.995,  # line 44 — this default value
```

Every other line in the file is frozen. Touching anything else is a
violation.

### Search bounds

`discount ∈ [0.90, 1.0]`.
- Below 0.90: the bandit barely accumulates any history; pathological.
- Above 1.0: math invariant of the class (`__init__` raises `ValueError`).

You may write the value as `0.995` literal or as a small expression
(e.g. `1.0 - 0.005`); keep it readable.

## Frozen files (do NOT touch)

- Anything else in `strategy/meta_controller.py` (any line ≠ 44)
- `config/risk_params.py` — risk-path config; `META_DISCOUNT` env override
  lives there but tuning the env var is out of scope (changes affect
  warmstart pipeline, not just the bandit constructor)
- `scripts/meta_warmstart.py` — builds priors; not the eval
- `scripts/meta_discount_eval.py` — the eval; reads from
  `strategy.meta_controller` so your edits propagate
- `hl_engine.py` and the live process
- `logs/*`, `data/*`, all other `autoresearch_*/` directories
- `.env`, `env.sh`

## Eval command

```bash
venv/bin/python3 scripts/meta_discount_eval.py
```

Runs in <1 s. Reads `logs/hl_engine.jsonl` over a 14-day lookback
(2,606 `exit_signal` events at baseline). Bandit uses `seed=0` →
deterministic.

## Metric

```
SCORE: <cumulative pnl_pct over matched events>
MATCH_RATE: <matched / total>
```

- **Primary (SCORE):** sum of `pnl_est` (in % units) on events where
  the bandit's `select()` matched the actual arm that fired. Higher
  is better. **Baseline at discount=0.995: +497.8990.**
- **Guard (MATCH_RATE):** must be ≥ **0.20**. Below this the eval prints
  `SCORE: -inf` automatically (treat as hard discard). A bandit that
  collapses to a single arm rapidly will fail this — collapse means it
  matches one arm well but ignores the other, biasing the score sample.

### Min-delta

Improvement only counts if SCORE increases by > **5.0** vs running best.
The eval is deterministic so this is just a sanity margin.

## Strategy guidance

Phase 1 — coarse landscape (iterations 1-8):
1. Probe widely first to map the curve. Try:
   - `0.999` (almost no decay)
   - `0.99`
   - `0.97`
   - `0.95`
   - `0.92`
2. The current default 0.995 is already well-located on the curve from
   our pre-launch probe (0.95 dropped SCORE by ~$580). Look for whether
   higher (slower decay) or lower (faster decay) helps.

Phase 2 — refinement (iterations 9-20):
3. Fine-step around whichever direction won phase 1, in 0.001 increments.
4. Try arithmetic combinations (e.g. 1 - 1/N for various N to express
   half-life intuition) if helpful.

Phase 3 — polish (iterations 21-30):
5. If a clear maximum emerged, sweep ±0.0005 around it.
6. If SCORE plateaued, document and stop early.

## Do NOT

- Edit any line of `strategy/meta_controller.py` other than line 44.
- Edit the eval (`scripts/meta_discount_eval.py`). It already imports
  `POWdTSBandit` from your constrained file; your edits propagate.
- Touch `config/risk_params.py` or its `META_DISCOUNT` env handling.
- Modify the random seed (`META_EVAL_SEED=0`) without explicit reason —
  changes the eval landscape entirely. If you do change it, treat that
  iteration as a separate experiment (note in description).
- Tune any other bandit parameter (priors, arms list, reward threshold).
  Those are separate experiments.
- Touch the live engine. Eval is replay-only.

## Loop mechanics

State files in this directory (same protocol as other autoresearch dirs):
- `autoresearch.jsonl` — config header (line 0) + iteration results.
- `autoresearch_dashboard.md` — regenerated each iteration.

After each iteration:
1. Run the eval. Parse `SCORE` and `MATCH_RATE`.
2. If `SCORE = -inf` → status=`crash` (or `guard_fail` if explicitly
   match-rate-driven), discard.
3. If `SCORE - best > 5.0` AND `MATCH_RATE ≥ 0.20` → keep.
4. Else → discard, `git checkout strategy/meta_controller.py`.
5. Append `{"type":"result", ...}` line to `autoresearch.jsonl` with
   iteration, values, score, match_rate, status, description, timestamp.
6. Regenerate `autoresearch_dashboard.md`.

## Stop conditions

- 30 iterations done.
- 5 consecutive non-improvements (parametric ceiling).
- Eval drift (different SCORE for the same discount across runs).
- A violation (edit outside line 44) — revert and stop, log to report.

## Recovery

If context is lost: read `autoresearch.jsonl` end-to-end. Config is line
0; last `result` line tells you where to resume. `git log -- strategy/meta_controller.py`
shows the kept history.

## Final report

When done: write `autoresearch_meta_discount/autoresearch_report.md` with:
- Best discount found, SCORE delta vs baseline.
- Top 5 iterations with descriptions.
- What worked / didn't (group iterations by direction).
- Recommended next round (e.g., "co-tune with prior_alpha/prior_beta",
  "test seed-robustness", "add hl_taker_z as a third arm").
