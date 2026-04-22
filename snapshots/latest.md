<!-- generated_at: 2026-04-22T20:47:43Z -->
<!-- source: logs/monitor/latest.md (sanitized) -->

# Monitor Report — 2026-04-22 20:01 UTC

## 1. Universe Scan — HL native perps
**Longs (z_4h≥+2):** 0
**Shorts (z_4h≤-2):** 3
- BNB/USD    z=-4.36 z_4h=-3.13 ★
- UNI/USD    z=-1.43 z_4h=-2.68 ★
- ENA/USD    z=-1.02 z_4h=-2.38

## 2. Universe Scan — HIP-3 (xyz subaccount, funded)
**Longs (z_4h≥+2):** 3
- xyz:HIMS/USD       z=+3.05 z_4h=+2.56 ★
- xyz:MSFT/USD       z=+1.40 z_4h=+2.43
- xyz:LLY/USD        z=+0.92 z_4h=+2.39
**Shorts (z_4h≤-2):** 2
- xyz:CRWV/USD       z=-1.94 z_4h=-3.22 ★
- xyz:EUR/USD        z=-2.00 z_4h=-2.19

_HIP-3 unfunded (blacklisted DEXs) w/ |z_4h|≥2.5:_ 2
  - vntl:ANTHROPIC/USD     z=-4.62 z_4h=-3.64
  - hyna:ENA/USD           z=-1.19 z_4h=-2.88

## 3. Screener — engine health
- **hl_engine**: PID 69837 up 09:32
- **hl_pairs**: PID 29813 up 02:01:06
- **feedback_loop**: NOT RUNNING
- flip_guard_blocked (1h): 60 ⚠
- pair lifecycle (1h): pair_open_complete=4, pair_close_complete=2, pair_stop_complete=0, pair_leg_no_fill=0, pair_beta_drift_rejected=2

## 4. Wallet NAV
- **main**: NAV $495.25 (Δ -23.03) margin $185.66 free $309.59 pos=9
- **xyz**: NAV $306.37 (Δ +18.92) margin $306.37 pos=14

## 5. Analyzer — patient-hold thesis distance
- ZEC            z_4h=-1.77 (long exit +5.0: Δ=+6.77; short exit -5.0: Δ=+3.23)
- xyz:MSTR       z_4h=+0.39 (long exit +5.0: Δ=+4.61; short exit -5.0: Δ=+5.39)
- AAVE           z_4h=-0.14 (long exit +5.0: Δ=+5.14; short exit -5.0: Δ=+4.86)
- xyz:RIVN       z_4h=-0.45 (long exit +5.0: Δ=+5.45; short exit -5.0: Δ=+4.55)
- LDO            z_4h=+0.49 (long exit +5.0: Δ=+4.51; short exit -5.0: Δ=+5.49)
- xyz:INTC       z_4h=-1.13 (long exit +5.0: Δ=+6.13; short exit -5.0: Δ=+3.87)
- xyz:NVDA       z_4h=+1.80 (long exit +5.0: Δ=+3.20; short exit -5.0: Δ=+6.80)
- xyz:AMZN       z_4h=+1.87 (long exit +5.0: Δ=+3.13; short exit -5.0: Δ=+6.87)

## 6. Open pair risk
- open pairs: 5
  - xyz:SKHX|BTC               pos=-1 z=+1.17 mtm=$+0.08
  - xyz:SKHX|ARB               pos=+1 z=+0.15 mtm=$+0.19
  - xyz:SKHX|xyz:TSM           pos=+1 z=-2.16 mtm=$-0.05
  - xyz:BRENTOIL|xyz:META      pos=+1 z=-2.68 mtm=$+0.00
  - LINK|xyz:MU                pos=+1 z=-1.45 mtm=$+0.01
