# Roadmap Status After Empirical Passes — 2026-04-27

The non-risk falsification loop set up by the original consolidation
brief has completed. This document supersedes the action priorities in
that brief; the literature analysis itself is preserved unchanged for
reference.

## What landed

```
#1  maker-path data scaffold       LANDED   commit 1343931
#3  BOCPD run-length monitor       LANDED   commit 0e38dfa
#5a funding forecast + Gap A regr  LANDED   commit b499242
#5b signals/funding_basis.py       KILLED   (Gap A regression failed)
```

## Empirical verdicts

### Maker scaffold (#1)

PASSED. 5,748 lifecycle records aggregated from existing engine log:
4,927 maker / 1,318 taker, 17% filled, 36% canceled, median lifetime
3.55s, net realized $+6.52 ($2.65 of which was fees). The data
substrate is now in place; the queue/cancel score (risk-path #2 in
roadmap) has the inputs it needs but should wait for forward
accumulation under the new schema before any `maker_engine.py` change.

### Gap A — perp funding as directional drift (#5a, #5b)

FAILED. Pooled OLS over 90d × 5 symbols × hourly bars:

```
              n     beta      t_stat    R²
BTC          492   −110.4     −2.29     0.011
ETH          492   −34.8      −0.73     0.001
SOL          492   +1.7       +0.06     0.000
DOGE         492   −2.8       −0.04     0.000
AAVE         492   −14.2      −0.24     0.000
POOLED      2460   −16.2      −0.86     0.0003
```

Direction is mostly negative (high funding-residual → mildly negative
next-hour return) which is economically consistent with crowded-long
mean-reversion, but the magnitude is below the 0.02 R² threshold the
roadmap set as the build-justification bar.

**Implication:** `signals/funding_basis.py` is killed. The literature
support for funding-aware basis BOUNDS (SSRN 5036933, SSRN 5481866)
still stands for position-sizing and risk modeling, but not for
next-hour direction prediction. Funding remains an accounting/cost
feature only, not signal drift.

### Gap B — Alpaca↔HL hybrid routing

UNCHANGED. No empirical pass run; still frontier per original
roadmap. `risk/stale_ref_veto.py` stays in the deferred risk-path
queue. Build only after the current risk-path queue clears.

### BOCPD regime suspicion (#3)

MOSTLY FAILED — i.e., the suspicion that static z-thresholds are
unsafe was not supported. 14 of 15 symbol×interval combinations
returned `stable` label; 1 ambiguous (ETH at 1h/90d, just below the
120-bar threshold). cp_prob ≈ 0.004 (= 1/λ baseline) across the board.

**Implication:** `analysis/regime_threshold_backtest.py` is deferred.
Static z-thresholds are reasonable on the timescales tested. The
follow-up cited in the original roadmap ("If frequent breaks, build
threshold backtest") is not triggered.

## Updated cumulative roadmap

| # | Item | Status | Class | Notes |
|---|---|---|---|---|
| 1 | maker-path data scaffold | LANDED | non-risk | commit 1343931 |
| 1.5 | forward maker-lifecycle accumulation | RUNNING | non-risk | engine writes new schema; ~7-30d to mature |
| 2 | analysis/maker_lifecycle_summary.py | NEXT non-risk | non-risk | fill rate, cancel rate, fee-adjusted edge by symbol |
| 3 | BOCPD run-length monitor | LANDED | non-risk | commit 0e38dfa |
| 3.5 | analysis/maker_cancel_score_backtest.py | DEFERRED | non-risk | prerequisite for #5; needs ≥7d forward maker data |
| 4 | PCA-OFI N=10 dry-run report | NEXT non-risk after #2 | non-risk | analysis/pca_ofi_report.py before any hl_engine.py change |
| 5 | maker_engine.py queue/cancel score | DEFERRED | RISK-PATH | sign-off + ≥7-30d forward maker data + #3.5 |
| 6 | hl_engine.py PCA-OFI N=10 | DEFERRED | RISK-PATH | sign-off + #4 dry-run |
| 7 | risk/stale_ref_veto.py | DEFERRED | RISK-PATH | Gap B; lowest priority until #5 + #6 settle |
| 5a | funding_forecast.py | LANDED | non-risk | commit b499242 |
| 5b | signals/funding_basis.py | KILLED | — | Gap A regression failed |

## Decision rules now in force

1. **Default for "should we unpause?" is NO** unless the five-reason
   frame in `~/.claude/projects/.../memory/feedback_unpause_default_no.md`
   has every condition flipped. Full unpause (no caps) is OFF the
   table until forward HL-truth positive proof exists.

2. **Risk-path queue cap of 2.** #5 and #6 are the two queued slots;
   #7 stays deferred. No additional risk-path items added without
   one of these clearing first.

3. **Forward-data gating.** #5 requires ≥7d (ideally 30d) of
   `logs/maker_lifecycle.jsonl` accumulation under the post-Commit-1
   schema before any `maker_engine.py` patch is considered. Today's
   historical replay covers prior data but a model that changes
   cancel behavior should validate on a forward window.

## What this document is NOT

- Not a re-run of the consolidation brief. The literature analysis in
  `{{PASTE GPT-5.5 OUTPUT HERE}}` (per the brief's marker) and the
  per-paper triage tables remain valid as research-axis context.
- Not a deployment plan. Default operational stance remains
  `PAUSE_NEW_ENTRIES=1`, pairs halted, auto_topup stopped.
- Not an authorization to start any risk-path item. #5 and #6 still
  require explicit sign-off.

## What changed about the project's research posture

Two speculative paths got cheap empirical falsification before consuming
engine-modification dev-days. The roadmap's non-risk-first ordering
worked as designed. Net dev-days saved by the negative results: ≈
5 days for `signals/funding_basis.py` plus ≈ 4 days for
`analysis/regime_threshold_backtest.py` if BOCPD had shown frequent
breaks (not triggered).

The next concrete deliverable is forward-data-gated, not literature-
gated. Pause the literature roadmap; resume only after #2 and #3.5 are
either run or deferred for a documented reason.
