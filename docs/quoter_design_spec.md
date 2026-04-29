# Quoter Design Spec — Task 18

**Status:** SPEC, not implementation. Locked in for the next session's build.
**Date:** 2026-04-29
**Predecessors:**
- `math_core/schedulers.py` (Task 17, committed `faf3df8`)
- `docs/regularized_riccati_validation_findings.md` (BL falsification, committed `1f0f3f3`)

## Purpose

The quoter does NOT invent a trajectory. Its only job is to **track the
scheduler's target inventory curve** `q*(t)` while maximizing maker-rebate
capture and keeping non-execution / adverse-selection risk bounded.

This is the second half of the Scheduler/Quoter split adopted after the
γ-regularized BL scheme failed the six-criterion validation matrix
(0/50 cells passing). The structural fix: move all γ-dependence (spread,
posting, fill-intensity) into the placement layer. There is no `1/γ`
anywhere in this design.

## Contract with the scheduler

The quoter consumes only:

| Symbol | Meaning |
|---|---|
| `t` | current time |
| `T` | horizon |
| `q_t` | current inventory (USD, signed) |
| `q*(t)` | target from `math_core.schedulers.get_target_inventory(...)` |
| `m_t` | current mid |
| `spread`, `touch` | book state |
| `Y_t` | OFI / imbalance |
| `λ(δ)` | fill-intensity estimate / queue state |
| `risk_limits` | net cap, per-symbol cap, kill-switch state |

**Tracking error (liquidation convention):**

```
e_t = q_t − q*(t)
```

- `e_t > 0`: behind schedule, must sell faster
- `e_t < 0`: ahead of schedule, can back off

## Quoter output

Per decision step, output an **execution intent** — not a theoretical rate:

- `side`: post-sell | post-buy | IOC-sell | IOC-buy | hold
- `price_offset(s)`: `δ_b, δ_a` from mid (or from touch)
- `clip_size`: USD per child order
- `order_type`: post-only | IOC
- `ttl`: cancel-replace cadence (s)
- `regime`: PASSIVE | TOUCH | CATCHUP (label, for telemetry)

## Core state variables

### 1. Time pressure
```
τ_t = T − t
```

### 2. Tracking urgency (catch-up pace)
```
u_t = max(0, e_t) / max(τ_t, τ_min)
```
Minimum average sell pace required from now to deadline. `τ_min` prevents
divide-by-zero at terminal.

### 3. Imbalance / adverse-selection state
Use `Y_t` **only in the placement layer**:
- favorable to selling → can be more passive
- toxic to selling → tighten / reduce waiting time / escalate sooner

This is exactly where OFI belongs after the BL falsification.

## Three execution regimes

### Regime 1 — PASSIVE (rebate capture)
**Use when:** `e_t` small AND plenty of time left AND imbalance not toxic.
**Action:**
- post-only at best ask or one tick inside passive envelope
- small-to-medium clips
- normal TTL

**Objective:** earn rebate; let fills do the work; avoid unnecessary crossing.

### Regime 2 — TOUCH (join the touch)
**Use when:** behind schedule but not critical, OR time pressure rising,
OR imbalance mildly toxic.
**Action:**
- quote at the touch
- shorten TTL
- increase clip size modestly
- cancel-replace more aggressively

**Objective:** raise fill probability without fully paying taker costs.

### Regime 3 — CATCHUP (forced completion)
**Use when:** `e_t` exceeds threshold, OR terminal slack low, OR end-of-horizon
completion risk high.
**Action:**
- IOC slice OR hybrid post-then-cross fallback
- size capped by participation / safety rules
- deadline-focused execution

**Objective:** protect terminal completion, not rebate capture.

## Spread model

AS-style reservation/offset logic, conditioned on scheduler miss.

### Reservation price (liquidation)
```
r_t = m_t − θ_q · q_t − θ_e · e_t − θ_Y · Y_t
```
- `θ_q`: inventory risk skew (large positive q pushes r down → encourage sell)
- `θ_e`: schedule-tracking skew (behind schedule pushes r down)
- `θ_Y`: imbalance toxicity skew (toxic sell-side OFI pushes r down)

### Quote offsets
With AS fill-intensity model `λ(δ) = A · e^(−k·δ)`:

| Regime | Offset rule |
|---|---|
| PASSIVE | larger `δ_a` (deeper, higher rebate, lower fill prob) |
| TOUCH | `δ_a → 0` (at touch) |
| CATCHUP | abandon quote optimization; use IOC |

**Critical:** no `1/γ` anywhere in the placement rule. This is the
structural property that makes the design well-posed at γ ≈ 10⁻⁶.

## Size model

The scheduler does NOT directly dictate a marketable rate. Map urgency `u_t`
into clip sizing:

```
s_t = min( s_max, s_base + η_u · u_t + η_e · e_t⁺ )
```

with additional reductions if:
- queue depth is thin
- imbalance is toxic
- recent adverse selection is high

> The scheduler says "sell faster."
> The quoter translates that into "bigger clip / shorter TTL / more aggressive venue behavior."

## Escalation logic

Per decision cycle:
1. compute `e_t`
2. compute `τ_t`
3. compute `u_t`
4. choose regime: PASSIVE / TOUCH / CATCHUP
5. compute quote offsets and clip
6. send intent
7. update on fill / timeout / price move

If after TTL expiry the fill deficit remains:
- re-evaluate `e_t`
- do NOT blindly repost the same order
- escalate regime if completion risk increased

## Acceptance criteria

Same institutional bar as the scheduler falsification. For each scheduler
family, the quoter must satisfy:

1. target tracking error stays bounded
2. no sign crossing
3. terminal completion within tolerance
4. maker share maximized subject to completion
5. toxic OFI → faster completion than favorable OFI
6. no reliance on hard emergency caps for normal operation

Plus two quoter-specific metrics:

7. realized fill mix: maker vs taker
8. adverse-selection cost after fill

## Files for the next session

| Path | Purpose |
|---|---|
| `math_core/quoter_policy.py` | pure decision logic, no live imports |
| `scripts/test_quoter_tracking.py` | drives scheduler curve + synthetic fills / OFI |
| `autoresearch_gated/quoter_tracking_matrix.json` | reproducible artifact |
| `docs/quoter_design_spec.md` | this file |

## Implementation order (matches scheduler sequencing)

1. TWAP + quoter
2. Exponential + quoter
3. Sinh-ratio + quoter
4. unified scheduler menu through one quoter interface (already prepared
   via `math_core.schedulers.get_target_inventory(...)`)

## Next-session decision

> Start with: **TWAP scheduler + three-regime quoter + synthetic fill-intensity model.**

Once that tracks cleanly, swapping the scheduler family is a one-line
change at the dispatcher. The quoter stays agnostic.

## Invariants (non-negotiable)

- No `1/γ` term anywhere in the quoter.
- Scheduler `q*(t)` is the only input that defines "where should I be."
- All OFI / queue / fill-intensity logic lives in the quoter, never in
  the scheduler.
- `_size_order()` and the live fixed-size maker path stay frozen until
  the combined policy clears criteria 1-8 above on the same matrix
  bar that falsified the regularized BL scheme.
