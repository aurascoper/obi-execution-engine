<!-- generated_at: 2026-04-23T00:48:24Z -->
<!-- source: logs/monitor/latest.md (sanitized) -->

# Monitor Report — 2026-04-23 00:01 UTC

## 1. Universe Scan — HL native perps
**Longs (z_4h≥+2):** 0
**Shorts (z_4h≤-2):** 3
- ETH/USD    z=-1.33 z_4h=-2.72 ★
- ARB/USD    z=-2.47 z_4h=-2.67 ★
- SUI/USD    z=-1.37 z_4h=-2.45

## 2. Universe Scan — HIP-3 (xyz subaccount, funded)
**Longs (z_4h≥+2):** 1
- xyz:LLY/USD        z=+2.08 z_4h=+2.64 ★
**Shorts (z_4h≤-2):** 2
- xyz:CRCL/USD       z=-3.98 z_4h=-2.69 ★
- xyz:RIVN/USD       z=-1.31 z_4h=-2.25

_HIP-3 unfunded (blacklisted DEXs) w/ |z_4h|≥2.5:_ 4
  - vntl:ROBOT/USD         z=+7.03 z_4h=+9.45
  - vntl:BIOTECH/USD       z=-5.55 z_4h=-3.27
  - para:OTHERS/USD        z=-1.58 z_4h=-2.68
  - hyna:XMR/USD           z=-1.32 z_4h=-2.60

## 3. Screener — engine health
- **hl_engine**: PID 69837 up 04:09:32
- **hl_pairs**: PID 86061 up 03:22:44
- **feedback_loop**: NOT RUNNING
- flip_guard_blocked (1h): 102 ⚠
- pair lifecycle (1h): pair_open_complete=4, pair_close_complete=0, pair_stop_complete=0, pair_leg_no_fill=0, pair_beta_drift_rejected=8

## 4. Wallet NAV
- **main**: NAV $283.54 (Δ -152.81) margin $274.08 free $9.45 pos=14
- **xyz**: NAV $523.79 (Δ +155.98) margin $523.79 pos=23

## 5. Analyzer — patient-hold thesis distance
- ZEC            z_4h=-1.54 (long exit +5.0: Δ=+6.54; short exit -5.0: Δ=+3.46)
- xyz:MSTR       z_4h=-1.21 (long exit +5.0: Δ=+6.21; short exit -5.0: Δ=+3.79)
- AAVE           z_4h=-1.03 (long exit +5.0: Δ=+6.03; short exit -5.0: Δ=+3.97)
- xyz:RIVN       z_4h=-2.25 (long exit +5.0: Δ=+7.25; short exit -5.0: Δ=+2.75)
- LDO            z_4h=-1.86 (long exit +5.0: Δ=+6.86; short exit -5.0: Δ=+3.14)
- xyz:INTC       z_4h=+1.03 (long exit +5.0: Δ=+3.97; short exit -5.0: Δ=+6.03)
- xyz:NVDA       z_4h=-0.54 (long exit +5.0: Δ=+5.54; short exit -5.0: Δ=+4.46)
- xyz:AMZN       z_4h=-0.85 (long exit +5.0: Δ=+5.85; short exit -5.0: Δ=+4.15)

## 6. Open pair risk
- open pairs: 4
  - xyz:SKHX|ARB               pos=+1 z=-0.82 mtm=$+0.53
  - xyz:SKHX|xyz:TSM           pos=+1 z=-1.42 mtm=$+0.19
  - xyz:EWY|xyz:GOOGL          pos=+1 z=-2.05 mtm=$-0.06
  - LINK|xyz:MU                pos=+1 z=-2.64 mtm=$-0.17
