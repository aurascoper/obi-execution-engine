# Calibration Correction Note — 2026-04-27

**Severity: foundational.** The HL truth loader used throughout this
calibration project (`parse_hl_closed_pnl` in
`scripts/validate_replay_fit.py`) was double-counting fills and
truncating at the API's 2000-record cap. After patching, the canonical
baseline ρ shifted from 0.4529 → 0.2582 (14d) and 0.0145 → −0.0604
(7d). Every prior ρ measurement and every prior dollar residual must
be re-read under the patched truth.

## What was wrong

```
old loader:
    iterate sources = [native + 7 builder-DEX Info instances]
    each source.user_fills_by_time(addr, from, to)  →  same 2000 fills
    sum closedPnl per coin across all 8 sources    →  8× double-count
    no pagination                                   →  fills past 2000 cap missed

verified: native, xyz, vntl Info instances all return identical fill
sets (sigdiff = 0). The HL API's user_fills_by_time IGNORES the
perp_dexs parameter; the per-address fill stream encodes builder-DEX
fills via the `coin` field (e.g., "xyz:CL", "vntl:SPACEX").
```

## What's correct now

```
new loader (validate_replay_fit.py:fetch_user_fills_all + parse_hl_closed_pnl):
    single Info instance, single fetch loop
    paginated via cursor advancement past max_time + 1ms
    deduped on (hash, oid, time, coin, side, px, sz)
    capped iterations at 50 to prevent runaway

verified output:
    14d window  raw 2,633 fills  →  unique 2,208 fills
    sum closedPnl: −$250.85  (was −$1,677.73 in bugged version)
    sum fees:      +$150.82  (was +$930.39 in bugged version)
    ratio: 6.7×    (≈ 8× double-counting minus pagination's extra ~200 fills)
```

## Patched canonical baselines

```
                        14d ρ          7d ρ
patched baseline       +0.2582       −0.0604
patched bucketed_3600  +0.2921       −0.1013
  Δρ vs baseline       +0.0339       −0.0410

patched Mode 1 audit
  (boundary-fixed)     +0.6788       −0.0554
  Δρ vs baseline       +0.4206       +0.0050
```

## Prior values — superseded

| Anchor | Buggy value | Patched value | Status |
|---|---|---|---|
| 14d baseline ρ | 0.4529 | 0.2582 | superseded |
| 7d baseline ρ | 0.0145 | −0.0604 | superseded |
| 14d bucketed_3600 ρ | 0.6125 | 0.2921 | superseded |
| 14d bucketed Δρ | +0.16 | +0.034 | superseded — fails +0.04 acceptance |
| 14d Mode 1 audit ρ | 0.8358 | 0.6788 | superseded — still passes 0.65 threshold |
| 14d net realized $ | −$2,608 | −$402 | 6.5× overstated |
| AAVE residual (14d) | −$364 | ledger-derived TBD | bugged dollar |
| ZEC residual (14d) | −$591 | +$25 (live actually positive) | sign was right magnitude wildly wrong |
| ETH (14d) | −$251 | −$132 | bugged dollar |

## Decisions affected

- **Bucketed cooldown** is no longer an accepted candidate (Δρ +0.034 < +0.04 rule). Demote `config/gates/reentry_cooldown_by_symbol.json` from "flag candidate" to "diagnostic only — not promoted". The flag stays in the codebase but should not be used as the active candidate baseline for further experiments.
- **Mode 1 session abstraction** still has the strongest signal (Δρ +0.42 over patched baseline, ρ=0.68 clears the original 0.65 threshold). Architectural direction holds. Mode 1 *naive valuation* is still wrong on the focus symbols (ZEC sign-flipped: hl +$25 vs audit −$450; ETH 4× overshoot; xyz:MSTR 5× overshoot; AAVE 8× overshoot). The fill-ledger audit (Mode 2A) is the next required step before any Mode 2B build.
- **TIME_STOP_S sweep** rerun complete. Results below.
- **Window-fragility narrative** for bucketed_3600 was based on bugged 7d ρ shift. The fragility may have been bug noise. Re-evaluate.

## What dollar values are now reliable

After this patch, the following numbers come from the corrected loader
and can be cited:

```
14d realized:          −$250.85 closedPnl, +$150.82 fees,  net −$401.67
7d realized:           −$48.65 closedPnl, +$107.45 fees,   net −$156.10

top-5 14d closedPnl by symbol:
  ETH         −$132.07    largest contributor to losses
  xyz:MSTR    +$ 57.37
  ZEC         +$ 25.50    POSITIVE  (was reported −$591)
  AAVE        −$ 21.52
  SOL         −$ 20.61

unique coins hit:    71  (28 native + 43 hip3/builder)
fill count window:   2,208 unique
```

## Going forward

- All new `ρ vs HL truth` measurements use the patched `parse_hl_closed_pnl`.
- All new dollar residual claims must be sourced from the patched loader.
- The bucketed cooldown flag is preserved but de-prioritized.
- `calibration_baseline_hl_truth_bucketed_3600.md` is updated with the
  corrected anchor.
- Task #2 (24h calibration gate) interpretation also affected — the
  "logged fill closed_pnl vs HL API ρ ≥ 0.98" gate is fine in shape
  but the API source must be the patched loader.

## TIME_STOP_S sweep — patched truth

Sweep configs (no cooldown, varying X3 time-stop only):

```
config             win    ρ        Δρ vs base   trades   meanH    replay$
baseline_3600      14    +0.2582   ——           6619     1.15h    −$1,581
baseline_3600       7    −0.0604   ——           6042     0.97h    −$  767
ts_7200            14    +0.3535   +0.0953      4797     2.04h    −$1,266
ts_7200             7    −0.1940   −0.1336      4311     1.77h    −$  530
ts_14400           14    +0.2194   −0.0387      3444     3.33h    −$1,192
ts_14400            7    −0.3026   −0.2422      3023     3.03h    −$  527
ts_28800           14    +0.3100   +0.0519      2613     4.89h    −$1,198
ts_28800            7    −0.3373   −0.2768      2231     4.66h    −$  547
ts_86400           14    +0.3061   +0.0479      1908     6.85h    −$1,218
ts_86400            7    −0.3854   −0.3249      1544     6.85h    −$  530
disabled           14    +0.4271   +0.1689 ★    1683     7.22h    −$1,063
disabled            7    −0.3849   −0.3245      1335     7.23h    −$  523
```

Acceptance verdicts (per spec, patched-truth baseline):

```
ts_7200    14d Δρ +0.095 ✓  / 7d Δρ −0.134 ✗   → REJECT (window-fragile)
ts_14400   14d Δρ −0.039 ✗  / 7d Δρ −0.242 ✗   → REJECT (regresses 14d)
ts_28800   14d Δρ +0.052 ✓  / 7d Δρ −0.277 ✗   → REJECT (window-fragile)
ts_86400   14d Δρ +0.048 ✓  / 7d Δρ −0.325 ✗   → REJECT (also fails trade-floor)
disabled   14d Δρ +0.169 ✓  / 7d Δρ −0.325 ✗   → REJECT (window-fragile, fails trade-floor)
```

**Headline**: pattern is the same as before patch — every config window-fragile. **`disabled` (no time-stop) gives the largest 14d Δρ of +0.169** (vs bucketed_3600's +0.034), but catastrophically fails 7d (−0.325). No promotion candidate emerges.

Under patched truth the magnitudes shift but the architectural conclusion stands: single-gate sweeps can't satisfy both windows. The next architectural step remains **fill-ledger session replay (Mode 2A)** — see `mode2_session_replay_spec.md`.

## Engine-side issue (separate)

The local `hl_fill_received` stream is incomplete vs the HL API for
analysis purposes — earlier audit showed local sum closed_pnl ≈ $-10
vs API −$251 in the same 14d window. The local engine's userFills
subscription captures only what the engine itself trades on; cross-DEX
fills (e.g., manual close scripts, hl_pairs.py fills) may not all
reach `logs/hl_engine.jsonl`. For audit purposes, ALWAYS use the HL
API source, not the local log.
