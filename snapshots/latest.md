<!-- generated_at: 2026-04-23T02:28:41Z -->
<!-- source: logs/monitor/latest.md (sanitized) -->

# Monitor Report — 2026-04-23 02:01 UTC

## 1. Universe Scan — HL native perps
**Longs (z_4h≥+2):** 0
**Shorts (z_4h≤-2):** 0

## 2. Universe Scan — HIP-3 (xyz subaccount, funded)
**Longs (z_4h≥+2):** 0
**Shorts (z_4h≤-2):** 3
- xyz:PLATINUM/USD   z=-1.99 z_4h=-3.12 ★
- xyz:COPPER/USD     z=-1.56 z_4h=-3.11 ★
- xyz:JP225/USD      z=-1.91 z_4h=-3.10 ★

_HIP-3 unfunded (blacklisted DEXs) w/ |z_4h|≥2.5:_ 1
  - km:XIAOMI/USD          z=-1.78 z_4h=-3.94

## 3. Screener — engine health
- **hl_engine**: PID 69837 up 06:09:32
- **hl_pairs**: PID 86061 up 05:22:44
- **feedback_loop**: NOT RUNNING
- flip_guard_blocked (1h): 62 ⚠
- pair lifecycle (1h): pair_open_complete=3, pair_close_complete=2, pair_stop_complete=0, pair_leg_no_fill=0, pair_beta_drift_rejected=5

## 4. Wallet NAV
- **main**: NAV $604.62 (Δ +90.95) margin $112.80 free $491.81 pos=5
- **xyz**: NAV $294.31 (Δ -97.28) margin $294.31 pos=10

## 5. Analyzer — patient-hold thesis distance
- ZEC            z_4h=-0.20 (long exit +5.0: Δ=+5.20; short exit -5.0: Δ=+4.80)
- xyz:MSTR       z_4h=-0.84 (long exit +5.0: Δ=+5.84; short exit -5.0: Δ=+4.16)
- AAVE           z_4h=-1.48 (long exit +5.0: Δ=+6.48; short exit -5.0: Δ=+3.52)
- xyz:RIVN       z_4h=-0.63 (long exit +5.0: Δ=+5.63; short exit -5.0: Δ=+4.37)
- LDO            z_4h=-0.02 (long exit +5.0: Δ=+5.02; short exit -5.0: Δ=+4.98)
- xyz:INTC       z_4h=-0.71 (long exit +5.0: Δ=+5.71; short exit -5.0: Δ=+4.29)
- xyz:NVDA       z_4h=-1.70 (long exit +5.0: Δ=+6.70; short exit -5.0: Δ=+3.30)
- xyz:AMZN       z_4h=-1.99 (long exit +5.0: Δ=+6.99; short exit -5.0: Δ=+3.01)

## 6. Open pair risk
- open pairs: 4
  - xyz:BRENTOIL|xyz:TSM       pos=+1 z=-2.45 mtm=$+0.15
  - ARB|xyz:EWY                pos=+1 z=-0.91 mtm=$-0.17
  - xyz:EWY|BTC                pos=+1 z=-1.62 mtm=$+0.04
  - LINK|xyz:MU                pos=+1 z=-0.97 mtm=$-0.15
