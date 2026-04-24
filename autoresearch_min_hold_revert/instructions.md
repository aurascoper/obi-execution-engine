# Autoresearch — z_revert exit-side damper tuning

## Goal

Maximize realized PnL of mean-reversion exits over the logged trading window
by tuning the two z_revert dampers in `strategy/signals.py`. Both are gates
that suppress z_revert exits unless conditions are met; tuning them changes
which exits fire and which are deferred to stop_loss / time_stop.

## System context

This is a 38-coin LIVE Hyperliquid + HIP-3 perps trading engine running
mean-reversion z-score entries with a 4-channel exit:
  X1 z_revert     — z back through Z_EXIT / Z_EXIT_SHORT (damper-gated)
  X2 ratchet      — shock-ratchet trail
  X3 time_stop    — age >= TIME_STOP_S
  X4 stop_loss    — adverse move >= STOP_LOSS_PCT
The constrained file's two dampers gate ONLY X1. Stops always fire.

## Constrained file

**`strategy/signals.py`** — but you may ONLY change the values on lines
112-113. Touching anything else in this file is a violation. The two
levers are:

```python
MIN_HOLD_FOR_REVERT_S = 15 * 60  # line 112 — current: 900 (15 min)
MIN_REVERT_BPS = 0.001           # line 113 — current: 0.001 (10 bps)
```

### Search bounds

- `MIN_HOLD_FOR_REVERT_S ∈ [60, 3600]` — 1 minute floor, 1 hour ceiling
- `MIN_REVERT_BPS ∈ [0.0, 0.01]` — "no gate" floor, 100 bps ceiling

You may write either bound as `int`/`float` literals or as expressions
(e.g. `15 * 60`, `0.0015`). Keep the comments next to the values
informative; a future reader should still be able to tell why a given
value was chosen.

## Frozen files (do NOT touch)

- Anything else in `strategy/signals.py` (any line not 112 or 113)
- `risk/*` and `config/risk_params.py` — risk path
- `scripts/z_entry_replay_gated.py` — the eval; it imports the two
  constants from `strategy.signals` so changes there propagate
- `config/z_entry_params.json`, `config/z4h_exit_params.json` — already
  tuned by separate autoresearch loops
- `hl_engine.py`, `execution/*` — engine + order paths
- `data/*`, `logs/*` — input data
- All other `autoresearch_*/` directories
- `.env`, `env.sh` — environment

## Eval command

```bash
venv/bin/python3 scripts/z_entry_replay_gated.py
```

Runs in ~8 s. Deterministic — same input data, same code → same SCORE.

## Metric

The eval prints two relevant lines:

```
SCORE: <signed dollar PnL across all symbols>
GUARD_WORST_SYM_DELTA: <worst per-symbol delta vs frozen baseline>  (<sym>)
```

- **Primary metric (SCORE):** total simulated PnL. Higher is better.
  Baseline at hold=900s, bps=0.001 is approximately **-1595.04**.
- **Guard metric (GUARD_WORST_SYM_DELTA):** per-symbol PnL change vs the
  frozen baseline file at `autoresearch_z_entry/_baseline_per_symbol.json`.
  **Threshold: must be ≥ -150.0.** If any single symbol regresses by more
  than $150 vs baseline, discard the change even if SCORE improves —
  this prevents portfolio gains driven by one or two coins blowing up.
- **Implicit guard (already enforced by the eval):** total trade count
  must stay above 30% of baseline (`MIN_TRADES_FRAC=0.30`). The eval
  already prints `SCORE: -inf` if you tighten dampers to the point that
  too few trades fire — treat any `-inf` result as a hard discard.

### Min-delta

Only count a change as an improvement if `SCORE` increases by more than
**$5.00** vs the running best. Smaller deltas may be noise (eval is
deterministic so this is mostly a safety margin against rounding).

## Strategy guidance

Phase 1 — coarse sweep (iterations 1–10):
1. Try the obvious extremes first to map the landscape:
   - hold=60, bps=0     → "no gate at all" — does removing dampers help?
   - hold=3600, bps=0.01 → "very strict" — does waiting harder help?
   - hold=900, bps=0    → just remove the move requirement
   - hold=60, bps=0.001 → just remove the hold requirement
2. From whichever extreme moves SCORE most, narrow toward it.

Phase 2 — refinement (iterations 11–20):
3. Sweep one knob with the other held at the best phase-1 value.
4. Test multiplicative-style relationships: hold × bps both up, both down.

Phase 3 — exploration (iterations 21–30):
5. If a clear winner emerged, fine-tune around it in 5%-step increments.
6. If SCORE plateaued, document the ceiling and stop early.

## Do NOT

- Edit any line of `strategy/signals.py` other than 112 and 113.
- Edit the eval script. The dampers ARE imported from signals.py — your
  edits propagate automatically.
- Change other tunables (Z_ENTRY, Z_EXIT, OBI_THETA, STOP_LOSS_PCT, etc.)
  even if the search seems blocked. Those have their own autoresearch
  loops and co-tuning is out of scope.
- Modify or delete `data/cache/bars.sqlite`, `logs/hl_engine.jsonl`, or
  any baseline file in `autoresearch_z_entry/`.
- Touch the live engine — `hl_engine.py` is running in production. The
  eval is replay-only; no live engine restart is required by this loop.
- Skip the guard check. `GUARD_WORST_SYM_DELTA < -150.0` → discard, full
  stop, even if SCORE went up.

## Loop mechanics

State files in this directory:
- `autoresearch.jsonl` — one JSON per line. Line 0 is config. Subsequent
  lines are iteration results.
- `autoresearch_dashboard.md` — regenerated after every iteration.

After each iteration:
1. Run the eval, parse `SCORE` and `GUARD_WORST_SYM_DELTA`.
2. If SCORE - best_score > 5.00 AND guard >= -150.0 → keep, update best.
3. Else → revert with `git checkout strategy/signals.py` and continue.
4. Append a `{"type":"result", ...}` line to `autoresearch.jsonl` with
   iteration #, current values, score, guard, status, and a one-sentence
   description of the change.
5. Regenerate `autoresearch_dashboard.md`.

## Recovery

If you lose context, read `autoresearch.jsonl` end-to-end. The config
header (line 0) has all experiment parameters. The last `result` line
has the most recent iteration. `git log -- strategy/signals.py` shows
the kept history. Continue from the next iteration number.

## Stop conditions

- 30 iterations done
- Or: 5 consecutive non-improvements (plateau — likely architectural
  ceiling, not parametric)

When stopping, write a final report to `autoresearch_report.md`
including: best (hold, bps) pair, SCORE delta vs baseline, what
worked, what didn't, and recommended next round (if any).
