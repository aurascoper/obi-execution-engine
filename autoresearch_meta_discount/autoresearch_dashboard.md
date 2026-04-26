# Autoresearch Dashboard — Meta-controller discount

**Constrained file:** `strategy/meta_controller.py` (line 44 only)
**Tunable:** `discount` ∈ [0.90, 1.0]
**Eval:** `venv/bin/python3 scripts/meta_discount_eval.py` (<1 s, deterministic seed=0)

**Baseline:** SCORE = +497.8990 · MATCH_RATE = 0.581 · 3-run variance = 0.0
**Current best:** +532.9050 at discount=0.991 (Δ +35.01) · **Iterations:** 14/30
**Guard:** MATCH_RATE ≥ 0.20 · **Min-delta:** 5.0
**Lookback:** 14 days · 2,606 exit_signal events · arms = [hl_z, momentum]

| #  | discount | SCORE   | Δ        | MATCH  | Status   | Description |
|----|---------:|--------:|---------:|-------:|----------|-------------|
| 0  | 0.995    | +497.90 | +0.00    | 0.581  | baseline | Frozen 3-run stability |
| 1  | 0.999    | +457.27 | -40.63   | 0.773  | discard  | Near-no-decay; match rate high but SCORE drops |
| 2  | 0.99     | +517.26 | +19.36   | 0.632  | keep     | Faster forgetting lifts SCORE ~19 over baseline |
| 3  | 0.97     | +247.52 | -250.38  | 0.686  | discard  | Too-aggressive decay craters SCORE |
| 4  | 0.95     | -85.22  | -583.12  | 0.520  | discard  | Matches pre-launch probe result |
| 5  | 0.992    | +487.02 | -30.24   | 0.658  | discard  | Between baseline and best; loses |
| 6  | 0.985    | +394.41 | -122.85  | 0.602  | discard  | Curve drops sharply below 0.99 |
| 7  | 0.988    | +463.75 | -53.51   | 0.598  | discard  | In valley; best stays at 0.99 |
| 8  | 0.991    | +532.91 | +15.65   | 0.573  | keep     | New best; ~15 over 0.99 |
| 9  | 0.9915   | +398.69 | -134.22  | 0.669  | discard  | Jagged fine-scale landscape |
| 10 | 0.9905   | +520.22 | -12.68   | 0.652  | discard  | Above baseline but below best |
| 11 | 0.9912   | +532.32 | -0.58    | 0.672  | discard  | Essentially ties best (within min-delta) |
| 12 | 0.9908   | +430.79 | -102.12  | 0.612  | discard  | Jagged — no improvement |
| 13 | 0.993    | +425.14 | -107.77  | 0.513  | discard  | Drops below baseline |
| 14 | 0.9911   | +532.55 | -0.35    | 0.685  | discard  | Tight plateau at [0.991, 0.9912] |

**Kept:** 2 · **Discarded:** 12 · **Crashed:** 0 · **Guard failures:** 0

**Stop condition hit:** 6 consecutive non-improvements after iter 8 keep → early stop.
