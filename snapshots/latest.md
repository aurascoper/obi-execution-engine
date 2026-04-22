<!-- generated_at: 2026-04-22T17:49:55Z -->
<!-- source: logs/monitor/latest.md (sanitized) -->

# Monitor Report — 2026-04-22 17:01 UTC

## 1. Universe Scan — HL native perps
**Longs (z_4h≥+2):** 1
- ARB/USD    z=+2.12 z_4h=+3.57 ★
**Shorts (z_4h≤-2):** 3
- CRV/USD    z=-2.07 z_4h=-2.85 ★
- SUI/USD    z=-1.71 z_4h=-2.69 ★
- ADA/USD    z=-1.28 z_4h=-2.65 ★

## 2. Universe Scan — HIP-3 (xyz subaccount, funded)
**Longs (z_4h≥+2):** 0
**Shorts (z_4h≤-2):** 0

_HIP-3 unfunded (blacklisted DEXs) w/ |z_4h|≥2.5:_ 1
  - hyna:SUI/USD           z=-1.66 z_4h=-2.64

## 3. Screener — engine health
- **hl_engine**: PID 97069 up 05:23:10
- **hl_pairs**: PID 58264 up 02:25:49
- **feedback_loop**: NOT RUNNING
- flip_guard_blocked (1h): 3
- pair lifecycle (1h): pair_open_complete=1, pair_close_complete=1, pair_stop_complete=0, pair_leg_no_fill=0, pair_beta_drift_rejected=9

## 4. Wallet NAV
- **main**: NAV $534.57 (Δ +0.96) margin $224.72 free $309.85 pos=4
- **xyz**: NAV $303.94 (Δ +1.13) margin $303.94 pos=8

## 5. Analyzer — patient-hold thesis distance
- ZEC            z_4h=+0.81 (long exit +5.0: Δ=+4.19; short exit -5.0: Δ=+5.81)
- xyz:MSTR       z_4h=-0.55 (long exit +5.0: Δ=+5.55; short exit -5.0: Δ=+4.45)
- AAVE           z_4h=-0.65 (long exit +5.0: Δ=+5.65; short exit -5.0: Δ=+4.35)
- xyz:RIVN       z_4h=+0.56 (long exit +5.0: Δ=+4.44; short exit -5.0: Δ=+5.56)
- LDO            z_4h=-0.18 (long exit +5.0: Δ=+5.18; short exit -5.0: Δ=+4.82)
- xyz:INTC       z_4h=-1.19 (long exit +5.0: Δ=+6.19; short exit -5.0: Δ=+3.81)
- xyz:NVDA       z_4h=+0.81 (long exit +5.0: Δ=+4.19; short exit -5.0: Δ=+5.81)
- xyz:AMZN       z_4h=+0.65 (long exit +5.0: Δ=+4.35; short exit -5.0: Δ=+5.65)

## 6. Open pair risk
- open pairs: 5
  - xyz:MU|NEAR                pos=-1 z=+1.61 mtm=$-0.51
  - xyz:BRENTOIL|xyz:TSM       pos=-1 z=+1.61 mtm=$-0.02
  - xyz:BRENTOIL|xyz:META      pos=-1 z=+1.53 mtm=$-0.32
  - xyz:EWY|BTC                pos=-1 z=+1.97 mtm=$+0.01
  - SOL|LTC                    pos=+1 z=-0.76 mtm=$-0.15
