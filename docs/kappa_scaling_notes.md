# κ Scaling Derivation — Notes for Next Session

**Status:** DRAFT for next-session work. Not implementation.
**Generated:** 2026-04-29 after the Riccati Convergence Duel (commit `809e2c5`).
**Trigger:** the duel showed `strategy/optimal_rate.py` diverging by 100x+ when κ > 0 and Y ≠ 0, even though the RK4 coefficient evolution itself is bounded.

---

## 1. The problem in one line

The BL optimal feedback is

```
α*(τ, x, y) = ( (2A + η_leak·C) · x  +  (C + 2 η_leak·B) · y ) / ( 2γ )
```

We need the **Y-coefficient** `K_Y(τ) := (C(τ) + 2 η_leak·B(τ)) / (2γ)` to remain **O(1/T)** or smaller, so that even with |y| ≈ 1 the rate `K_Y · y` is bounded by something on the order of "inventory ÷ horizon," not orders of magnitude larger.

In tonight's duel, with our calibrated **γ = η_frac ≈ 1.77×10⁻⁶**, picking κ = 0.5 gave `K_Y ≈ 0.7 × 10⁵` — i.e., a $1 of OFI signal demanded a $70,000/s trading rate. This blew up the inventory budget in the first 60 seconds.

The fix is to derive κ from γ, β, σ rather than choose it freely.

---

## 2. Steady-state analysis (long-horizon limit)

Set `dA/dτ = dB/dτ = dC/dτ = 0` in the Riccati system.

```
A_dot = λ   − (2A + η_leak·C)² / (4γ)         (= 0  ⇒  2A + η_leak·C = 2√(γλ))
B_dot = κ   − (C + 2 η_leak·B)² / (4γ) − 2β B  (= 0)
C_dot = − (2A + η_leak·C)(C + 2 η_leak·B) / (2γ) − β C   (= 0)
```

Let `P := 2A + η_leak·C` and `Q := C + 2 η_leak·B`. From `A_dot = 0`: P = 2√(γλ).

From `C_dot = 0`:
```
P · Q / (2γ) + β C = 0     ⇒    Q = − 2γβC / P = − γβC / √(γλ) = − C·√(γβ²/λ)
```

From `B_dot = 0`:
```
κ − Q²/(4γ) − 2β B = 0
2β B = κ − Q²/(4γ)
2 η_leak B = (η_leak/β) (κ − Q²/(4γ))
```

Substituting `Q = C + 2 η_leak B`:
```
Q − C = 2 η_leak B = (η_leak/β)(κ − Q²/(4γ))
```

Combined with the C_dot relation `Q = −C·√(γβ²/λ)`, we get a closed system.

For our regime where `λ` is small (operator-tunable, not extreme) and `η_leak` is small, the dominant balance is:
```
Q² ≈ 4γκ − 8γβB ≈ 4γκ      (when β B << κ)
|Q| ≈ 2√(γκ)
```

So the **steady-state Y-coefficient** is approximately
```
K_Y∞ = Q / (2γ) ≈ √(κ / γ)
```

For `K_Y∞` to be O(1/T), we need:
```
κ / γ  ≲  1 / T²
κ      ≲  γ / T²
```

For our values (γ = 1.77×10⁻⁶, T = 3600 s):
```
κ_max ≈ 1.77×10⁻⁶ / (3600)² ≈ 1.4×10⁻¹³
```

That's **13 orders of magnitude smaller** than the κ = 0.5 we tested with. The divergence ratio was about 10⁵ in α — consistent with √(0.5 / 1.77×10⁻⁶) ≈ 530, times some factor for the η_leak term and the transient C(τ) growth from the boundary at τ=0.

---

## 3. Cost-integral consistency check

The BL cost functional has three running terms:
```
∫₀ᵀ ( γ α² + κ Y² + λ x² ) dt
```

For the optimizer to *not* prefer trading mostly to manipulate Y, these terms must be of the same magnitude in the optimal regime:

```
γ α²  ~  κ Y²  ~  λ x²
```

With α ~ x/T (TWAP scale), Y ~ O(1), x ~ X₀:
```
γ · (X₀/T)²    ~  κ · 1   ~  λ · X₀²
```

From the first and second:  `κ ~ γ · X₀² / T²` — same order as the steady-state result with `X₀² ≈ 1`, i.e., `κ ~ γ / T²` matches.

From the first and third: `λ ~ γ / T²` — also useful as a cross-check on our λ derivation. With γT = 2 and our HIP-3 numbers, we set `λ = 5.46×10⁻¹³`. That's `(2/T)² × γ_frac = 4γ/T² = 4 × 1.4×10⁻¹³ ≈ 5.5×10⁻¹³`. ✅ Consistent with γ/T² scaling.

So the rule of thumb is:
```
κ ≲ γ / T²  ≈  λ
```

i.e., **κ should be on the same order as λ.**

---

## 4. Verification theorem condition (from BL paper)

The paper's Proposition 2 proves the Riccati system has a unique bounded solution under a structural condition. Roughly (informal restatement — verify against §3 of the paper):

> The cost-functional must be coercive: `α (terminal penalty) ≻ ½ X_b X_b^T` where X_b is some quantity built from the impact + inventory-risk + OFI-penalty terms.

For our scalar case, this typically reduces to a constraint of the form:
```
σ² · κ_max  <  4 β γ · (something)
```

i.e., **σ² · κ must not dominate γ β**. For our numbers (σ²=2.56e-4, β=0.0082, γ=1.77e-6):
```
σ² · κ_max  <  4 β γ
2.56×10⁻⁴ · κ_max  <  4 · 0.0082 · 1.77×10⁻⁶
κ_max  <  2.27×10⁻⁴
```

That's a *much looser* upper bound than the steady-state argument gave us. So the steady-state O(γ/T²) is the binding constraint, not the verification theorem.

**To-do for next session:** read the actual paper §3 to nail down the exact verification condition rather than my rough estimate above.

---

## 5. Proposed κ derivation rule

Synthesizing the above, for our HIP-3-calibrated regime:

```
κ_target = c · γ / T²
```

where `c` is an O(1) tuning constant (start at 1, sweep 0.1 → 10).

For the standard `--horizon-s 3600` test:
- γ = 1.77×10⁻⁶
- T = 3600
- T² = 1.296×10⁷
- κ_target = c × 1.37×10⁻¹³

If we want the OFI feedback to be *meaningful* (visible Y dependence) but *bounded* (no 100x overshoot), `c ∈ [0.1, 1]` is the right window. Larger c gives stronger Y feedback but risks edging toward the verification-theorem boundary.

---

## 6. Practical safety net (independent of derivation)

Even with the right κ, a parameter-misspecification accident could re-introduce the blowup. Add a hard rate cap at the integration layer:

```python
α_max_per_step = inventory / dt_remaining_min
α* = sign(α*_BL) · min(|α*_BL|, α_max_per_step)
```

This keeps the policy bounded by definition: it can't trade more than the residual inventory in the remaining time. The policy's *direction* and *shape* are still BL-derived; only the magnitude is clipped.

This belongs in `strategy/optimal_rate.py` (or a thin wrapper) before any wiring to `_size_order()`. Implementation budget: ~10 LoC + a unit test.

---

## 7. Concrete next-session plan

1. **Read BL §3 (Proposition 2 + verification theorem)** — confirm the σ²·κ structural constraint precisely; re-derive κ_max from there rather than my hand-waved estimate.
2. **Re-run `scripts/test_riccati_duel.py` with κ = c × γ/T² for c ∈ {0.01, 0.1, 1, 10}.** Pick the `c` that produces:
   - bounded α (max rate ≤ a few × `X₀/T`)
   - meaningful Y differentiation (Y=+0.8 trajectory measurably faster than Y=0)
   - terminal residual within ±0.01% of `X₀`
3. **Add the rate-cap safety net** to `strategy/optimal_rate.py` (separate authorized PR; minor code change).
4. **Re-run the duel one more time** with the safety net + correct κ. Both should pass cleanly.
5. **Only then** discuss `_size_order()` wiring.

Estimated next-session effort: 1–2 hours for the math + parameter sweep, 30 min for the safety net + tests.

---

## 8. What this session learned

- The BL Riccati is mathematically valid but **operationally fragile** when the parameter regime drifts far from the paper's assumed equity-market norms (γ ~ O(1)) into our maker-rebate-driven regime (γ ~ O(10⁻⁶)).
- The duel pattern — sandbox vs full-feature solver — is the right approach. We should keep `scripts/test_riccati_duel.py` as the regression test for any future change to either solver.
- We never wired `_size_order()`. CLAUDE.md change-discipline held throughout.

## 9. Files to revisit at start of next session

- `scripts/test_riccati_duel.py` — the regression harness
- `strategy/optimal_rate.py` — the unverified RK4 BL solver
- `math_core/riccati.py` — the trustworthy sandbox
- `scripts/calibrate_bl_params.py` — to re-pull β, σ, η if needed
- This file (`docs/kappa_scaling_notes.md`) — the queued derivation
