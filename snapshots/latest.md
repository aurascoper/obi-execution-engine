<!-- generated_at: 2026-04-23T11:50:16Z -->
<!-- source: logs/monitor/latest.md (sanitized) -->

# Monitor Report — 2026-04-23 11:01 UTC

## 1. Universe Scan — HL native perps
**Longs (z_4h≥+2):** 0
**Shorts (z_4h≤-2):** 0

## 2. Universe Scan — HIP-3 (xyz subaccount, funded)
**Longs (z_4h≥+2):** 2
- xyz:AMZN/USD       z=+2.49 z_4h=+3.50 ★
- xyz:AAPL/USD       z=+1.89 z_4h=+2.86 ★
**Shorts (z_4h≤-2):** 0

_HIP-3 unfunded (blacklisted DEXs) w/ |z_4h|≥2.5:_ 2
  - km:RTX/USD             z=-1.55 z_4h=-3.54
  - hyna:XPL/USD           z=-1.78 z_4h=-3.09

## 3. Screener — engine health
- **hl_engine**: PID 16544 up 08:06:38
- **hl_pairs**: PID 16321 up 08:07:13
- **feedback_loop**: NOT RUNNING
- flip_guard_blocked (1h): 99 ⚠
- pair lifecycle (1h): pair_open_complete=6, pair_close_complete=3, pair_stop_complete=0, pair_leg_no_fill=0, pair_beta_drift_rejected=10

## 4. Wallet NAV
- **main**: NAV $479.38 (Δ -35.12) margin $217.61 free $261.77 pos=8
- **xyz**: NAV $315.07 (Δ +28.74) margin $315.07 pos=12

## 5. Analyzer — patient-hold thesis distance
- ZEC            z_4h=-1.12 (long exit +5.0: Δ=+6.12; short exit -5.0: Δ=+3.88)
- xyz:MSTR       z_4h=-0.48 (long exit +5.0: Δ=+5.48; short exit -5.0: Δ=+4.52)
- AAVE           z_4h=+0.39 (long exit +5.0: Δ=+4.61; short exit -5.0: Δ=+5.39)
- xyz:RIVN       z_4h=+0.96 (long exit +5.0: Δ=+4.04; short exit -5.0: Δ=+5.96)
- LDO            z_4h=-1.50 (long exit +5.0: Δ=+6.50; short exit -5.0: Δ=+3.50)
- xyz:INTC       z_4h=-0.05 (long exit +5.0: Δ=+5.05; short exit -5.0: Δ=+4.95)
- xyz:NVDA       z_4h=+0.47 (long exit +5.0: Δ=+4.53; short exit -5.0: Δ=+5.47)
- xyz:AMZN       z_4h=+3.50 (long exit +5.0: Δ=+1.50; short exit -5.0: Δ=+8.50)

## 6. Open pair risk
- open pairs: 5
  - xyz:NFLX|BCH               pos=-1 z=+1.66 mtm=$+0.05
  - xyz:SKHX|ARB               pos=-1 z=+1.32 mtm=$-0.11
  - xyz:SKHX|xyz:TSM           pos=+1 z=-1.83 mtm=$-0.06
  - xyz:MU|NEAR                pos=-1 z=+2.60 mtm=$-0.14
  - xyz:BRENTOIL|xyz:META      pos=+1 z=-1.85 mtm=$-0.40
