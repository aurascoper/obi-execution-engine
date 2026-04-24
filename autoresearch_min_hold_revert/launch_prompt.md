# Autoresearch launch prompt — paste into a fresh Claude Code session

You are running an autonomous autoresearch loop. Your goal: maximize the
SCORE printed by the eval below by tuning two parameters in
`strategy/signals.py`, with a guard against per-symbol regressions.

## Read first
1. `autoresearch_min_hold_revert/instructions.md` — full rules + scope.
2. `autoresearch_min_hold_revert/autoresearch.jsonl` — config header
   (line 0) and any prior iterations.
3. `autoresearch_min_hold_revert/autoresearch_dashboard.md` — human view.
4. `strategy/signals.py` lines 110-114 — the constrained region.

## Confirm baseline
Run the eval ONCE and confirm SCORE matches the baseline in the JSONL
config (-1595.0377, GUARD -67.7907). If it doesn't, STOP and ask the
user — something has changed in the input data and the experiment
needs re-baselining.

```bash
venv/bin/python3 scripts/z_entry_replay_gated.py 2>&1 | grep -E "^SCORE|^GUARD_WORST"
```

## Loop, up to 30 iterations

For each iteration:
1. Decide a candidate (hold, bps) within the bounds. Follow the strategy
   guidance in instructions.md (extremes first, then refine).
2. Edit ONLY lines 112 and 113 of `strategy/signals.py`.
3. Run the eval. Parse SCORE and GUARD_WORST_SYM_DELTA from stdout.
4. Decide:
   - SCORE = -inf → discard, status=`crash` (eval rejected the params,
     usually MIN_TRADES_FRAC).
   - guard < -150.0 → discard, status=`guard_fail`.
   - SCORE - best_score > 5.0 AND guard ≥ -150.0 → keep, update best,
     status=`keep`.
   - Else → discard, status=`discard`.
5. If discarded: `git checkout strategy/signals.py` to revert.
   If kept: leave the change in place; subsequent iterations branch off
   it. (You can still revert later if a stronger candidate appears by
   reverting + applying the new values directly.)
6. Append a `{"type":"result", ...}` line to `autoresearch.jsonl` with:
   `iteration`, `values`, `score`, `delta`, `guard_score`, `guard_pass`,
   `status`, `description`, `timestamp`.
7. Regenerate `autoresearch_dashboard.md` (full table from JSONL).

## Stop early if
- 5 consecutive non-improvements (parametric ceiling reached).
- Any iteration mutates `strategy/signals.py` outside lines 112-113
  (treat as a violation; revert and stop, ask for guidance).
- The eval starts emitting different SCOREs for unchanged params
  (eval drift; report and stop).

## When done (30 iters or early stop)

Write `autoresearch_min_hold_revert/autoresearch_report.md` containing:
- Best (hold, bps) found, SCORE delta vs baseline.
- Top 5 iterations with descriptions.
- What worked / what didn't (group iterations by direction explored).
- Recommended next round, if any (e.g., "co-tune with Z_EXIT — single-
  knob sweep plateaued at $X under baseline").

Do NOT change anything else, do NOT touch the live engine, do NOT push
or commit anything. The user will review the kept changes and decide
whether to commit the final values.

Begin.
