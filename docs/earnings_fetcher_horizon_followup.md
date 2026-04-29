# Follow-up: earnings fetcher horizon design choice

**Status:** PROPOSAL — not yet authorized. Created 2026-04-29 after the SNDK gap was discovered.
**Tonight's stance (operator-set):** do not widen the default. Cover the gap operationally by running a separate routine for each upcoming day.

---

## What happened

The 2026-04-29 readiness routine + fetcher used a 36-hour window (`Apr 29 00:00 UTC` → `Apr 30 12:00 UTC`). SNDK's earnings print at `Apr 30 20:00 UTC` was 8 hours outside that window. We held a $74 long going into it without flagging it.

Discovered after-the-fact when the operator asked about the SNDK trade. Manually patched tonight by running the fetcher for `2026-04-30` and scheduling a parallel readiness routine.

## Two design options for permanent fix

### Option A — widen the default window to 48h

```python
DEFAULT_WINDOW_H = 48     # was 36
```

Pros:
- One routine per day, no operational overhead change.
- Catches all single-day-ahead and most two-day-ahead prints in one shot.

Cons:
- Larger window means more events in the readiness summary, including some that don't really concern today's positions. Potential noise.
- Still misses 3+ day prints if a position is opened with a long horizon.
- A fixed 48h window doesn't scale — we'd be revisiting the constant any time a longer-horizon edge case shows up.

### Option B — "next two trading days" routine pair

Run the fetcher daily for **both** day+1 and day+2:
- 6am Central routine fires `--date $(today+1)`
- Same routine also fires `--date $(today+2)` and writes a separate readiness report
- Operator gets two summaries, can act on either independently

Pros:
- Explicit two-day visibility.
- Each readiness summary is scoped to a single trading day — easier to reason about.
- The day+2 file gives 36+h of advance notice for after-close prints, matching the dust-soak intervention budget.

Cons:
- Operational overhead: two routines to maintain, two readiness files per day.
- Possible duplicate entries when day+2 (today's routine) overlaps day+1 (yesterday's routine — which already covered this date).

## Recommendation

**Option B**, with one tweak:
- The readiness summary should explicitly cross-reference any open position against both day+1 AND day+2 earnings names.
- Implement as a single routine that calls the fetcher twice (once per date) and emits a combined digest.

This keeps the "single 6am routine" cadence the operator already runs but extends visibility forward by a full day. Catches the SNDK case automatically going forward.

## When to revisit

- After SNDK is closed before the 2026-04-30 print
- Before the next dust-soak rung (Stage 4 or whatever comes after Stage 3 evaluable)
- Or whenever another miss surfaces (which would itself be a regression signal)

## Out of scope tonight

- No change to fetcher defaults
- No change to routine schedule
- No new automation
