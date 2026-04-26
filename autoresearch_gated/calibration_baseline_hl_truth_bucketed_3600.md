# Calibration Baseline — HL closedPnl truth + bucketed cooldown (flagged)

## Live PnL source

```
live_pnl_source         : hl_closed_pnl_gross
api_call                : Info.user_fills_by_time(addr, from_ms, to_ms, aggregate_by_time=False)
                          summed `closedPnl` per coin across native + 7 builder-DEX
                          clearinghouses (xyz, vntl, hyna, flx, km, cash, para)
loader                  : scripts/validate_replay_fit.py:parse_hl_closed_pnl
prior (broken) source   : exit_signal pnl_est event (correlation ρ ≈ 0.6 with HL truth)
                          replaced 2026-04-26 — see feedback_replay_pnl_metric_overfits_truth.md
```

## Baselines

```
window                  : 14d primary, 7d holdout
baseline_hl_truth_rho   : 0.4529   (no replay-side flags)
candidate_bucketed_3600 : 0.6125   (REENTRY_COOLDOWN_BY_SYMBOL=...)
delta                   : +0.1596
gate_target             : 0.80
remaining_gap           : 0.19
7d_baseline_rho         : 0.0145
7d_candidate_rho        : 0.0210   (Δ +0.0066 — passes "≥-0.02 worsening" rule)
```

## Candidate config

```
flag                    : --reentry-cooldown-by-symbol  (env: REENTRY_COOLDOWN_BY_SYMBOL)
config_file             : config/gates/reentry_cooldown_by_symbol.json
default_status          : OFF — environment variable must be set explicitly
sensitivities_tested    : 1800s, 3600s, 7200s — only 3600s passed all rules

bucket assignment (structural — not metric-chasing):
  HIP-3 / xyz: equity perps   → 3600s cooldown
  ZEC (auto_topup watcher)    → 3600s cooldown
  long-hold natives           → 0s (off)
                                 AAVE, ETH, BTC, SOL, LDO, CRV, BNB,
                                 SUI, TAO, DOGE, LINK, ADA, AVAX, LTC,
                                 BCH, DOT, UNI, POL, RENDER, FIL, HYPE,
                                 NEAR, ENA, PAXG, ARB, XRP
```

## Acceptance scorecard (14d)

```
[PASS]  14d Δρ ≥ +0.04                          +0.1596
[PASS]  7d ρ doesn't drop >0.02                 +0.0066
[PASS]  AAVE Δ|residual| < $25                  +0.00
[PASS]  top-10 abs residual improves ≥ $50      -$108.29
[PASS]  xyz equity bucket residual improves     -$167.07
[PASS]  longhold deleted counterfactual ≤ +$10  +$0.00
```

## Promotion status

```
default_status          : OFF
promotion_status        : paper-soak / forward validation only
do_not_default_until    :
  - 24h calibration gate passes (corr(logged closed_pnl, HL API closedPnl) ≥ 0.98)
  - bucketed_3600 result reproduced on a window not used in tuning
  - top-10 residual improvement holds on the new window
```

## Reproduction command

```
REENTRY_COOLDOWN_BY_SYMBOL=config/gates/reentry_cooldown_by_symbol.json \
  venv/bin/python3 scripts/validate_replay_fit.py --window 14d
```

## What this baseline is NOT

- Not the original 2026-04-26 baseline of ρ=0.17 — that was against the
  broken `exit_signal pnl_est` source which under-counted live PnL.
- Not a deployable production change — it's a flag, off by default.
- Not a strategy change — pure replay/validation harness modification.

## Related artifacts

- scripts/z_entry_replay_gated.py — position-state refactor + cooldown gate
- scripts/validate_replay_fit.py:parse_hl_closed_pnl — HL-truth source
- scripts/bucketed_cooldown_matrix.py — the validation matrix run
- autoresearch_gated/bucketed_cooldown_matrix.json — full per-symbol data
- ~/.claude/projects/.../memory/feedback_bucketed_cooldown_accepted.md
