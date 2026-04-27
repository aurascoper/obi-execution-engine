# Calibration Baseline — HL closedPnl truth (PATCHED 2026-04-27)

> **2026-04-27: foundational correction.** The HL truth loader was
> 8×-double-counting and missing fills past the 2000-record cap. After
> patching `parse_hl_closed_pnl` (single-source, paginated, deduped),
> the canonical baselines below are SUPERSEDED. See
> `calibration_correction_note.md` for the full audit.

## Live PnL source (PATCHED)

```
live_pnl_source         : hl_closed_pnl_gross
api_call                : Info.user_fills_by_time(addr, from_ms, to_ms, ...)
                          single fetch (NOT iterated per-DEX), paginated by
                          cursor advancement, deduped on (hash, oid, time,
                          coin, side, px, sz)
loader                  : scripts/validate_replay_fit.py:fetch_user_fills_all
                          + parse_hl_closed_pnl
prior (broken) sources  :
  exit_signal pnl_est   replaced 2026-04-26 (ρ ≈ 0.6 with venue truth)
  iterated 8 DEX Info   replaced 2026-04-27 (8× double-count + truncation)
```

## Patched baselines (canonical anchor)

```
                          14d ρ         7d ρ
patched baseline         +0.2582       −0.0604
patched bucketed_3600    +0.2921       −0.1013
  Δρ                     +0.0339       −0.0410
patched Mode 1 audit
  (boundary-fixed)       +0.6788       −0.0554
  Δρ                     +0.4206       +0.0050

gate_target              0.80          0.80
remaining_gap (14d)      0.54          —
```

## Bugged baselines (SUPERSEDED — do not use for decisions)

```
SUPERSEDED 2026-04-27:
  baseline 14d ρ          : 0.4529   →  0.2582
  bucketed_3600 14d ρ     : 0.6125   →  0.2921
  bucketed_3600 14d Δρ    : +0.1596  →  +0.0339   (now BELOW +0.04 acceptance)
  Mode 1 audit 14d ρ      : 0.8358   →  0.6788   (still passes 0.65 threshold)
  baseline 7d ρ           : 0.0145   →  −0.0604
  bucketed_3600 7d ρ      : 0.0210   →  −0.1013

  net realized $-2,608 / 14d  →  −$402 / 14d   (6.5× overstated)
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

## Acceptance scorecard (14d) — PATCHED 2026-04-27

```
[FAIL]  14d Δρ ≥ +0.04                          +0.0339   (was +0.1596 with bug)
[FAIL]  7d ρ doesn't drop >0.02                 −0.0410   (was +0.0066 with bug)

bucketed_3600 NO LONGER passes acceptance under patched truth.
Demoted from "flag candidate" to "diagnostic only".
The flag remains in config/gates/ for reproducibility but is NOT
the active candidate baseline for further experiments.
```

## Promotion status — PATCHED

```
default_status          : OFF (unchanged)
promotion_status        : DEMOTED — fails acceptance under patched truth
diagnostic_use          : retained for cross-validation only
do_not_promote          : never, until a new candidate exceeds patched
                          baseline by ≥ +0.04 / 14d AND 7d ≥ −0.02
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
