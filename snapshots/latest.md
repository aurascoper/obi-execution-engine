<!-- generated_at: 2026-04-26T06:41:43Z -->
<!-- source: logs/monitor/latest.md (sanitized) -->

# Monitor Report — 2026-04-23 12:01 UTC

## 1. Universe Scan — HL native perps
**Longs (z_4h≥+2):** 3
- ZEC/USD    z=+1.48 z_4h=+2.98 ★
- POL/USD    z=+1.84 z_4h=+2.95 ★
- LTC/USD    z=+1.90 z_4h=+2.81 ★
**Shorts (z_4h≤-2):** 0

## 2. Universe Scan — HIP-3 (xyz subaccount, funded)
**Longs (z_4h≥+2):** 3
- xyz:SP500/USD      z=+2.10 z_4h=+3.38 ★
- xyz:GOOGL/USD      z=+2.21 z_4h=+2.96 ★
- xyz:BABA/USD       z=+1.74 z_4h=+2.93 ★
**Shorts (z_4h≤-2):** 2
- xyz:CL/USD         z=-1.96 z_4h=-2.36
- xyz:JP225/USD      z=-2.24 z_4h=-2.10

_HIP-3 unfunded (blacklisted DEXs) w/ |z_4h|≥2.5:_ 4
  - km:SMALL2000/USD       z=+2.49 z_4h=+3.82
  - para:OTHERS/USD        z=-1.27 z_4h=-3.09
  - km:USTECH/USD          z=+1.83 z_4h=+2.76
  - km:XIAOMI/USD          z=+1.88 z_4h=+2.57

## 3. Screener — engine health
- **hl_engine**: PID 16544 up 09:06:39
- **hl_pairs**: PID 16321 up 09:07:14
- **feedback_loop**: NOT RUNNING
- flip_guard_blocked (1h): 120 ⚠
- pair lifecycle (1h): pair_open_complete=1, pair_close_complete=2, pair_stop_complete=0, pair_leg_no_fill=0, pair_beta_drift_rejected=10

## 4. Wallet NAV
- **main**: NAV $470.39 (Δ -8.99) margin $329.37 free $141.02 pos=14
- **xyz**: NAV $317.67 (Δ +2.60) margin $317.67 pos=14

## 5. Analyzer — patient-hold thesis distance
- ZEC            z_4h=+2.98 (long exit +5.0: Δ=+2.02; short exit -5.0: Δ=+7.98)
- xyz:MSTR       z_4h=+2.28 (long exit +5.0: Δ=+2.72; short exit -5.0: Δ=+7.28)
- AAVE           z_4h=+1.39 (long exit +5.0: Δ=+3.61; short exit -5.0: Δ=+6.39)
- xyz:RIVN       z_4h=+1.38 (long exit +5.0: Δ=+3.62; short exit -5.0: Δ=+6.38)
- LDO            z_4h=-0.01 (long exit +5.0: Δ=+5.01; short exit -5.0: Δ=+4.99)
- xyz:INTC       z_4h=+1.16 (long exit +5.0: Δ=+3.84; short exit -5.0: Δ=+6.16)
- xyz:NVDA       z_4h=+2.64 (long exit +5.0: Δ=+2.36; short exit -5.0: Δ=+7.64)
- xyz:AMZN       z_4h=+2.27 (long exit +5.0: Δ=+2.73; short exit -5.0: Δ=+7.27)

## 6. Open pair risk
- open pairs: 3
  - xyz:SKHX|xyz:TSM           pos=+1 z=-1.98 mtm=$-0.17
  - xyz:BRENTOIL|xyz:META      pos=+1 z=-2.23 mtm=$-0.48
  - xyz:NFLX|BCH               pos=-1 z=+1.32 mtm=$+0.17
