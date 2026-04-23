<!-- generated_at: 2026-04-23T03:48:54Z -->
<!-- source: logs/monitor/latest.md (sanitized) -->

# Monitor Report — 2026-04-23 03:01 UTC

## 1. Universe Scan — HL native perps
**Longs (z_4h≥+2):** 0
**Shorts (z_4h≤-2):** 3
- LTC/USD    z=-1.26 z_4h=-2.45
- BCH/USD    z=-1.51 z_4h=-2.35
- BNB/USD    z=-1.03 z_4h=-2.16

## 2. Universe Scan — HIP-3 (xyz subaccount, funded)
**Longs (z_4h≥+2):** 2
- xyz:BRENTOIL/USD   z=+2.81 z_4h=+2.36
- xyz:CL/USD         z=+2.65 z_4h=+2.17
**Shorts (z_4h≤-2):** 3
- xyz:ORCL/USD       z=-2.00 z_4h=-3.31 ★
- xyz:CRWV/USD       z=-1.66 z_4h=-3.20 ★
- xyz:EUR/USD        z=-3.39 z_4h=-3.11 ★

_HIP-3 unfunded (blacklisted DEXs) w/ |z_4h|≥2.5:_ 2
  - km:XIAOMI/USD          z=-3.72 z_4h=-6.98
  - vntl:MAG7/USD          z=-1.55 z_4h=-2.66

## 3. Screener — engine health
- **hl_engine**: PID 16544 up 06:39
- **hl_pairs**: PID 16321 up 07:14
- **feedback_loop**: NOT RUNNING
- flip_guard_blocked (1h): 97 ⚠
- pair lifecycle (1h): pair_open_complete=7, pair_close_complete=3, pair_stop_complete=0, pair_leg_no_fill=0, pair_beta_drift_rejected=2

## 4. Wallet NAV
- **main**: NAV $841.84 (Δ +237.22) margin $3.08 free $838.75 pos=2
- **xyz**: NAV $59.35 (Δ -234.96) margin $54.29 pos=8

## 5. Analyzer — patient-hold thesis distance
- ZEC            z_4h=-1.35 (long exit +5.0: Δ=+6.35; short exit -5.0: Δ=+3.65)
- xyz:MSTR       z_4h=-1.92 (long exit +5.0: Δ=+6.92; short exit -5.0: Δ=+3.08)
- AAVE           z_4h=-1.42 (long exit +5.0: Δ=+6.42; short exit -5.0: Δ=+3.58)
- LDO            z_4h=-0.83 (long exit +5.0: Δ=+5.83; short exit -5.0: Δ=+4.17)
- xyz:NVDA       z_4h=-2.12 (long exit +5.0: Δ=+7.12; short exit -5.0: Δ=+2.88)
- xyz:AMZN       z_4h=-1.77 (long exit +5.0: Δ=+6.77; short exit -5.0: Δ=+3.23)

## 6. Open pair risk
- open pairs: 4
  - xyz:BRENTOIL|xyz:META      pos=-1 z=+2.32 mtm=$-0.06
  - xyz:EWY|BTC                pos=+1 z=-2.75 mtm=$-0.11
  - xyz:EWY|xyz:GOOGL          pos=+1 z=-3.02 mtm=$-0.10
  - xyz:NFLX|BCH               pos=+1 z=-2.36 mtm=$-0.01
