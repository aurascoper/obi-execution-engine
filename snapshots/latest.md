<!-- generated_at: 2026-04-22T19:27:29Z -->
<!-- source: logs/monitor/latest.md (sanitized) -->

# Monitor Report — 2026-04-22 19:01 UTC

## 1. Universe Scan — HL native perps
**Longs (z_4h≥+2):** 0
**Shorts (z_4h≤-2):** 3
- FIL/USD    z=-4.31 z_4h=-3.22 ★
- RENDER/USD z=-4.45 z_4h=-3.00 ★
- ENA/USD    z=-3.91 z_4h=-2.59 ★

## 2. Universe Scan — HIP-3 (xyz subaccount, funded)
**Longs (z_4h≥+2):** 0
**Shorts (z_4h≤-2):** 3
- xyz:COST/USD       z=-1.70 z_4h=-2.89 ★
- xyz:TSLA/USD       z=-2.45 z_4h=-2.61 ★
- xyz:BABA/USD       z=-3.27 z_4h=-2.05

_HIP-3 unfunded (blacklisted DEXs) w/ |z_4h|≥2.5:_ 2
  - para:OTHERS/USD        z=-3.02 z_4h=-3.37
  - vntl:DEFENSE/USD       z=-1.23 z_4h=-2.65

## 3. Screener — engine health
- **hl_engine**: PID 45838 up 17:18
- **hl_pairs**: PID 29813 up 01:01:06
- **feedback_loop**: NOT RUNNING
- flip_guard_blocked (1h): 27 ⚠
- pair lifecycle (1h): pair_open_complete=2, pair_close_complete=0, pair_stop_complete=0, pair_leg_no_fill=0, pair_beta_drift_rejected=8

## 4. Wallet NAV
- **main**: NAV $518.28 (Δ -35.26) margin $458.41 free $59.87 pos=8
- **xyz**: NAV $287.45 (Δ +14.03) margin $287.45 pos=11

## 5. Analyzer — patient-hold thesis distance
- ZEC            z_4h=-0.88 (long exit +5.0: Δ=+5.88; short exit -5.0: Δ=+4.12)
- xyz:MSTR       z_4h=+0.12 (long exit +5.0: Δ=+4.88; short exit -5.0: Δ=+5.12)
- AAVE           z_4h=+0.17 (long exit +5.0: Δ=+4.83; short exit -5.0: Δ=+5.17)
- xyz:RIVN       z_4h=+0.21 (long exit +5.0: Δ=+4.79; short exit -5.0: Δ=+5.21)
- LDO            z_4h=+1.50 (long exit +5.0: Δ=+3.50; short exit -5.0: Δ=+6.50)
- xyz:INTC       z_4h=-1.79 (long exit +5.0: Δ=+6.79; short exit -5.0: Δ=+3.21)
- xyz:NVDA       z_4h=-0.66 (long exit +5.0: Δ=+5.66; short exit -5.0: Δ=+4.34)
- xyz:AMZN       z_4h=+1.11 (long exit +5.0: Δ=+3.89; short exit -5.0: Δ=+6.11)

## 6. Open pair risk
- open pairs: 3
  - xyz:SKHX|ARB               pos=+1 z=-0.90 mtm=$+0.06
  - ARB|xyz:EWY                pos=-1 z=+1.27 mtm=$+0.32
  - xyz:NFLX|BCH               pos=-1 z=+2.15 mtm=$-0.06
