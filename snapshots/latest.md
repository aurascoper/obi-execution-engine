<!-- generated_at: 2026-04-23T09:29:51Z -->
<!-- source: logs/monitor/latest.md (sanitized) -->

# Monitor Report — 2026-04-23 09:01 UTC

## 1. Universe Scan — HL native perps
**Longs (z_4h≥+2):** 0
**Shorts (z_4h≤-2):** 0

## 2. Universe Scan — HIP-3 (xyz subaccount, funded)
**Longs (z_4h≥+2):** 2
- xyz:BRENTOIL/USD   z=+1.57 z_4h=+2.75 ★
- xyz:CL/USD         z=+1.49 z_4h=+2.41
**Shorts (z_4h≤-2):** 3
- xyz:TSLA/USD       z=-1.79 z_4h=-3.13 ★
- xyz:PLATINUM/USD   z=-1.96 z_4h=-3.06 ★
- xyz:SILVER/USD     z=-1.61 z_4h=-2.69 ★

_HIP-3 unfunded (blacklisted DEXs) w/ |z_4h|≥2.5:_ 1
  - vntl:NUCLEAR/USD       z=-2.79 z_4h=-3.02

## 3. Screener — engine health
- **hl_engine**: PID 16544 up 06:06:38
- **hl_pairs**: PID 16321 up 06:07:13
- **feedback_loop**: NOT RUNNING
- flip_guard_blocked (1h): 91 ⚠
- pair lifecycle (1h): pair_open_complete=3, pair_close_complete=3, pair_stop_complete=0, pair_leg_no_fill=0, pair_beta_drift_rejected=6

## 4. Wallet NAV
- **main**: NAV $556.12 (Δ -11.12) margin $556.14 free $0.00 pos=7
- **xyz**: NAV $285.33 (Δ +3.31) margin $285.33 pos=9

## 5. Analyzer — patient-hold thesis distance
- ZEC            z_4h=+0.27 (long exit +5.0: Δ=+4.73; short exit -5.0: Δ=+5.27)
- xyz:MSTR       z_4h=-1.76 (long exit +5.0: Δ=+6.76; short exit -5.0: Δ=+3.24)
- AAVE           z_4h=-1.32 (long exit +5.0: Δ=+6.32; short exit -5.0: Δ=+3.68)
- xyz:RIVN       z_4h=-1.72 (long exit +5.0: Δ=+6.72; short exit -5.0: Δ=+3.28)
- LDO            z_4h=+0.44 (long exit +5.0: Δ=+4.56; short exit -5.0: Δ=+5.44)
- xyz:INTC       z_4h=-2.02 (long exit +5.0: Δ=+7.02; short exit -5.0: Δ=+2.98)
- xyz:NVDA       z_4h=-2.47 (long exit +5.0: Δ=+7.47; short exit -5.0: Δ=+2.53)
- xyz:AMZN       z_4h=-0.28 (long exit +5.0: Δ=+5.28; short exit -5.0: Δ=+4.72)

## 6. Open pair risk
- open pairs: 1
  - xyz:BRENTOIL|xyz:TSM       pos=-1 z=+2.21 mtm=$-0.14
