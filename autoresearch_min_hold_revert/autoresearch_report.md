# Autoresearch Report — z_revert dampers

## Best configuration

- **`MIN_HOLD_FOR_REVERT_S = 60`** (1 minute floor, was 900 = 15 min)
- **`MIN_REVERT_BPS = 0.001`** (unchanged — 10 bps favorable move)

## Outcome

- **Best SCORE:** -1581.0398 (iteration 4)
- **Baseline SCORE:** -1595.0377
- **Improvement:** +13.9979 USD across all symbols (about +0.88%)
- **Guard:** -67.79 (AAVE) — identical to baseline (PASS)
- **Stopping reason:** 5+ consecutive non-improvements (iters 5-10) after discovering best at iter 4; 10 total iterations run

## Top 5 iterations by SCORE

| Rank | Iter | hold (s) | bps | SCORE | Notes |
|-----:|-----:|---------:|-------:|---------:|-------|
| 1 | 4 | 60 | 0.0010 | **-1581.04** | KEPT best — hold gate was over-restrictive, bps gate alone is sufficient |
| 1t | 7 | 120 | 0.0010 | -1581.04 | Tie with iter 4 — no z_revert fires in the 60-120s window; hold is a plateau |
| 3 | 8 | 300 | 0.0010 | -1585.67 | Mild regression once hold >= 5 min — starts deferring profitable exits |
| 4 | 9 | 60 | 0.0015 | -1593.52 | Worse than peak bps — stricter bps defers too many exits |
| 5 | 6 | 60 | 0.0020 | -1594.70 | Marginally worse than baseline SCORE |

## What worked

- **Dropping the hold gate (Direction: loosen hold).** The original 15-minute floor was over-restrictive; positions recovering z within the first 15 min were being force-held until a stop fired, losing the profitable exit. Cutting the floor to 60s captures those early reverts.
- **Keeping bps=0.001 intact.** The 10 bps "proof-of-reversion" requirement is a tight peak — both looser (0.0005, 0.0008) and stricter (0.0015, 0.0020) variants regressed. It provides real discrimination between genuine z-reversions and mean-drift artifacts.

## What did not work

- **Removing both gates (iter 1: hold=60, bps=0).** Collapsed SCORE by -586, confirming at least one gate is load-bearing.
- **Pure tightening (iter 2: hold=3600, bps=0.01).** Worsened by -95, as predicted — too many exits deferred to stop_loss / time_stop.
- **Keeping hold=900 with bps=0 (iter 3).** -219 regression. The bps gate is the critical one; removing it swamps SCORE with drift-driven false exits.
- **Hold sweep (iters 7, 8).** Everything in [60, 120] is flat; by 300s, small regression begins. Above that (iters 2, baseline) it gets progressively worse.
- **Fine bps sweep (iters 5, 6, 9, 10).** bps=0.001 sits on a sharp, narrow peak — any deviation by +/-50% costs 10-50 USD.

## Landscape summary

```
bps \  hold    60        120       300       900        3600
0.0000       -2181     --         --        -1815      --
0.0005       -1629     --         --        --         --
0.0008       -1601     --         --        --         --
0.0010       -1581     -1581      -1586     -1595(BL)  --
0.0015       -1594     --         --        --         --
0.0020       -1595     --         --        --         --
0.0100       --        --         --        --         -1691
```

Two clear takeaways:

1. **Hold axis** is nearly flat between 60-120s, mildly degrading from 300s upward. The optimum is at or near the floor.
2. **Bps axis** is a sharp peak at 0.001. Moving in either direction costs.

## Recommended next round

- **Parametric ceiling likely reached on this pair.** The +14 USD gain is real but small; both axes were explored with both extreme and fine sweeps.
- **If further gains are wanted, co-tune out of scope params** (Z_EXIT, STOP_LOSS_PCT, TIME_STOP_S). The z_revert exit path is bounded by how many exits the other three channels (ratchet, time, stop) swallow; tuning dampers alone cannot fix exits the other channels already take.
- **AAVE is still the worst-delta symbol** (GUARD = -67.79, unchanged across all iterations). AAVE z_revert behavior is orthogonal to these dampers — any AAVE-specific mean-reversion pathology requires symbol-level investigation or exclusion, not a damper tweak.
- **Consider a mid-range hold sweep** (iters at 180, 240, 600) in a follow-up round if you want to rule out a very shallow optimum between 120 and 300s. Based on the flat 60->120 and mild regression 120->300, I estimate expected gain <5 USD.

## Final state of `strategy/signals.py` lines 112-113

```python
MIN_HOLD_FOR_REVERT_S = 60  # iter4: drop hold gate only
MIN_REVERT_BPS = 0.001  # iter4: keep 10 bps — isolate effect of removing hold only
```

(Comments reflect the decision lineage; a future reader can trace the hold-floor
drop to iteration 4 of this loop, where it was validated against baseline with
guard PASS.)
