# Mode 2 Session-Replay Spec

Pre-implementation design note. Mode 2 (policy-mode session replay) is
**not** to be built until this spec is locked AND the fill-ledger audit
proves the accounting model can reproduce HL truth.

## Context — what got us here

The boundary-fixed Mode 1 audit (`replay_position_sessions.py`,
2026-04-27) produced:

```
14d ρ:  baseline 0.4529 → audit 0.8358   (+0.38)
7d  ρ:  baseline −0.0672 → audit −0.0824 (essentially flat)
```

14d clears every threshold. 7d is dominated by ZEC (live closedPnl
+$1006, audit −$373; $1379 mismatch) — symptomatic of a naive valuation
(`peak_qty × Δprice`) that breaks on:
  - VWAP shifts from auto_topup adds
  - Partial reductions priced at non-window prices
  - Direction flips during ratchet sells
  - Boundary-spanning sessions clipped without ledger context

So the next layer is **fill-ledger accounting**, not policy generation.

## Five required design decisions

### 1. Position quantity model — fill-ledger state machine

Replace `peak_qty` heuristic with a per-symbol signed-qty ledger that
walks `hl_fill_received` events chronologically.

State per symbol:
```python
position = {
    "symbol": sym,
    "side": "long" | "short" | None,    # None when flat
    "qty": float,                        # |qty|, always positive when open
    "entry_vwap": float,
    "realized_pnl": 0.0,
    "fees": 0.0,
    "reductions": [],                    # list of dict per reduction fill
    "adds": [],                          # list of dict per add fill
    "open_ts_ms": int,
}
```

On each fill:
```python
signed_d = +sz if side=="buy" else -sz
new_signed_qty = current_signed_qty + signed_d

# four mutually-exclusive transitions
if |current| == 0 and |new| > 0:           OPEN
elif sign(current)*sign(signed_d) > 0:     ADD             (same direction)
elif |new| > 0 and sign(new) == sign(current):
                                           REDUCE          (smaller, same sign)
elif current * new < 0:                    FLIP            (cross zero)
elif |new| == 0:                           CLOSE
```

Notes:
- FLIP is treated as REDUCE-to-zero + OPEN-with-residual.
- ADD always rolls into a single open session (no new session_id).
- REDUCE never opens a new session; closes the old when qty hits 0.

### 2. Cost-basis model — VWAP from exposure-increasing fills only

```python
# on ADD only:
new_vwap = (old_qty * old_vwap + add_qty * fill_px) / (old_qty + add_qty)

# on REDUCE: vwap unchanged (only qty drops)
# on FLIP/CLOSE: vwap → reset
# on OPEN: vwap = fill_px
```

**For pre-window sessions**, priority order:
1. **Historical fill reconstruction** — walk ALL hl_fill_received events
   from log start, build the ledger; the running state at window_start
   is correct by construction. (This is what the audit will do.)
2. `user_state` snapshot nearest window_start — not currently archived.
3. `mark_at(window_start)` fallback — flagged as APPROXIMATE; only used
   when ledger reconstruction is impossible (e.g., engine log doesn't
   cover the symbol's open).

**DO NOT** use peak quantity as exposure proxy. The Mode 1 audit's
−0.082 7d ρ is the cost of that shortcut.

### 3. Exit-price model — separate two modes explicitly

| | source of reductions | source of exit_px | use case |
|---|---|---|---|
| **Mode 2A — session-audit replay** | actual HL reduction fills | actual fill_px | prove accounting can match HL truth (ledger validation) |
| **Mode 2B — session-policy replay** | replay policy decisions | bar mark / mid at policy-decided ts | evaluate replay strategy quality |

**Do not mix.** Mode 2A is the ground-truth accounting check. Mode 2B
is the strategy candidate. The fill-ledger audit (next deliverable)
is Mode 2A.

### 4. Partial-reduction attribution

On every REDUCE fill:
```python
realized_pnl = side_sign(side) * reduce_qty * (fill_px - entry_vwap)
position.realized_pnl += realized_pnl
position.fees += fill_fee
position.qty -= reduce_qty
position.reductions.append({
    "ts_ms": fill.ts_ms,
    "qty": reduce_qty,
    "fill_px": fill_px,
    "entry_vwap_at_reduce": entry_vwap,
    "ledger_realized_pnl": realized_pnl,
    "ledger_fee": fill_fee,
    "hl_closed_pnl": fill.closed_pnl,    # venue truth at this fill
    "hl_fee": fill.fee,
})
```

**Acceptance for the accounting layer (ledger ≈ HL truth):**
- ρ(ledger_realized_pnl, hl_closed_pnl) ≥ 0.98 per symbol
- |Σ ledger_realized − Σ hl_closed_pnl| / |Σ hl_closed_pnl| ≤ 2%
- |Σ ledger_fee − Σ hl_fee| / |Σ hl_fee| ≤ 2%

If this fails, the audit found a discrepancy with HL's pricing
assumptions (most likely: HL uses different cost-basis treatment on
flips, or different fee timing). Investigate before building Mode 2B.

### 5. Boundary clipping for validation windows

**For PnL attribution to a window, count only events that occurred
in-window:**
```
in-window realized PnL =
    Σ ledger_realized_pnl(reduction)  WHERE reduction.ts_ms ∈ [from, to)
+ unrealized PnL change of any session OPEN at window boundaries
  (only if we explicitly include unrealized — by default, EXCLUDE)
```

**Default attribution rule for Mode 2A:**
- only realized fills inside the window count
- pre-window opens contribute realized PnL only via reductions inside window
- post-window opens (still open at window_end) contribute $0 realized in window

This is a clean apples-to-apples comparison with HL's
`user_fills_by_time(from, to)` filtering, which is what
`parse_hl_closed_pnl` does. The two should match per-fill modulo
HL-side cost-basis quirks.

**Naive shortcut to avoid:**
```
session.audit_pnl = peak_qty × (mark_at_window_end − mark_at_window_start)
```
This overstates exposure on auto_topup symbols (peak ≠ representative
qty), misattributes direction on ratchet exits, and ignores VWAP shifts.
That's what blew up ZEC in the 7d Mode 1 audit.

## Acceptance gate — fill-ledger audit (Mode 2A)

Before any Mode 2B (policy) work:
- 14d ledger-vs-HL ρ ≥ 0.95 across symbols
- 7d ledger-vs-HL ρ ≥ 0.95
- ZEC residual reduced by ≥ 80% vs the Mode 1 naive audit
- ETH sign matches HL truth
- boundary-spanning sessions explicitly counted via realized fills only

If the audit passes:
- Mode 2A is established as ground-truth accounting
- Mode 2B (policy replay) build is justified
- Promotion gate for Mode 2B: 14d ρ ≥ 0.75 AND 7d improves materially

If the audit fails:
- Mode 2B is premature
- Investigate where ledger diverges from HL (cost basis on flips, fee
  timing, FX/funding inclusion, etc.)

## Implementation files

### Now (Mode 2A audit)
```
scripts/audit_fill_ledger_sessions.py
autoresearch_gated/audit_fill_ledger_sessions.json
```

### Later (Mode 2B policy)
```
scripts/session_policy_replay.py    (new file, NOT inside z_entry_replay_gated.py)
autoresearch_gated/session_policy_replay.json
```

Mode 2B is an **architectural fork**, not a flag inside the existing
candidate-trade harness. Keep `z_entry_replay_gated.py` intact.

## What this spec is NOT

- Not a green light for Mode 2B implementation
- Not a promotion plan
- Not authorization for any engine-side change
- Not a replacement for the calibration_baseline_hl_truth artifact

## Decision rules summary

```
ledger audit ρ ≥ 0.95   → Mode 2B build justified
ledger audit ρ < 0.95   → fix accounting first
Mode 2B 14d ρ ≥ 0.75    → promotion-candidate
Mode 2B 14d ≥ 0.75 only,
        7d worse        → diagnostic only, NOT promotion
```
