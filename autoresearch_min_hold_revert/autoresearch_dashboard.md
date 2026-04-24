# Autoresearch Dashboard — z_revert dampers

**Constrained file:** `strategy/signals.py` (lines 112-113 only)
**Tunables:** `MIN_HOLD_FOR_REVERT_S` in [60, 3600] · `MIN_REVERT_BPS` in [0.0, 0.01]
**Eval:** `venv/bin/python3 scripts/z_entry_replay_gated.py` (~8 s, deterministic)

**Baseline:** SCORE = -1595.0377 · GUARD = -67.79 (AAVE) · 3-run variance = 0.0
**Current best:** -1581.0398 (iter 4) · **Iterations:** 10/30
**Guard:** GUARD_WORST_SYM_DELTA >= -150.0 · **Min-delta:** 5.0
**Stop:** 5 consecutive non-improvements reached (iters 5-10)

| # | hold (s) | bps | SCORE | Delta | Guard | Status | Description |
|---|---------:|-------:|---------:|---------:|-------:|--------|-------------|
| 0 | 900 | 0.0010 | -1595.04 | +0.00 | -67.79 PASS | baseline | Frozen 3-run stability check |
| 1 | 60 | 0.0000 | -2181.02 | -585.98 | -68.32 PASS | discard | Extreme no-gate — collapses SCORE |
| 2 | 3600 | 0.0100 | -1690.67 | -95.63 | -68.52 PASS | discard | Extreme very-strict — defers too much to stops |
| 3 | 900 | 0.0000 | -1814.70 | -219.67 | -68.32 PASS | discard | bps=0 alone (keep hold) — bps gate meaningful |
| 4 | 60 | 0.0010 | -1581.04 | +13.998 | -67.79 PASS | **keep** | Drop hold floor — bps gate alone is sufficient |
| 5 | 60 | 0.0005 | -1628.67 | -47.63 | -68.32 PASS | discard | bps=5 — too loose |
| 6 | 60 | 0.0020 | -1594.70 | -13.66 | -68.52 PASS | discard | bps=20 — slightly too strict |
| 7 | 120 | 0.0010 | -1581.04 | +0.00 | -67.79 PASS | discard | hold=120 same as 60 — flat plateau |
| 8 | 300 | 0.0010 | -1585.67 | -4.63 | -67.79 PASS | discard | hold=300 — slight regression |
| 9 | 60 | 0.0015 | -1593.52 | -12.48 | -68.52 PASS | discard | bps=15 — worse than 0.001 |
| 10 | 60 | 0.0008 | -1601.39 | -20.35 | -67.79 PASS | discard | bps=8 — narrow peak confirmed |

**Kept:** 1 · **Discarded:** 9 · **Crashed:** 0 · **Guard failures:** 0
