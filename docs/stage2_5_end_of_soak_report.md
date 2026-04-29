# Stage 2.5 End-of-Soak Report

**Soak window:** `2026-04-28T19:35:07Z` → `2026-04-28T23:48:25.712925Z`  
**Elapsed:** 253.3 min (4.22h)  
**Engine PID:** 91041 (still running)  
**Configuration:** `NOTIONAL_PER_TRADE_OVERRIDE=50`, `MAX_NET_NOTIONAL=250`, `MAX_NEW_ENTRIES_PER_SESSION=0` (unlimited; net-cap governs), `HL_SESSION_LOSS_GUARD_USD=75`, `PAUSE_NEW_ENTRIES=0`

## 1. Sample-target progress (binding)

| Target | Required | Actual | Status |
|---|---|---|---|
| `sizing_shadow` | ≥ 200 | 207 | ✅ MET |
| `sizing_runtime_shadow` | ≥ 200 | 245 | ✅ MET |
| `hl_fill_received` w/ fee+pnl+hash | ≥ 5 | 46/46 | ✅ MET |
| `risk_gate_net_cap` blocks | ≥ 1 | 27 | ✅ MET |
| Fills missing fee/pnl/hash | 0 | 0 | ✅ CLEAN |

## 2. Stop-condition guards

| Guard | Count | Status |
|---|---|---|
| `sizing_runtime_shadow_failed` | 0 | ✅ |
| `hl_session_loss_guard` fires | 0 | ✅ |
| `hl_engine_shutdown_initiated` | 0 | ✅ |
| Submitted-notional max | $50.38 | ✅ |
| Submitted notional > $55 | 0 | ✅ |

## 3. Submitted notional distribution

- **Total submitted orders:** 293
- **Min:** $14.86
- **Median:** $49.91
- **Mean:** $49.58
- **Max:** $50.38
- **Stdev:** $2.75
- **HIP-3 orders:** 222 (75.8%)
- **Native orders:** 71 (24.2%)

## 4. Top symbols by submitted notional (count + total $)

| Symbol | Orders | Total $ |
|---|---|---|
| `NEAR` | 20 | $998.96 |
| `hyna:BNB` | 20 | $998.42 |
| `vntl:DEFENSE` | 20 | $991.65 |
| `vntl:SEMIS` | 16 | $798.66 |
| `xyz:EWY` | 15 | $749.85 |
| `RENDER` | 11 | $549.31 |
| `flx:GAS` | 11 | $548.36 |
| `xyz:TSM` | 11 | $548.05 |
| `hyna:IP` | 10 | $499.69 |
| `xyz:TSLA` | 10 | $497.04 |
| `xyz:SNDK` | 9 | $449.90 |
| `xyz:CRWV` | 9 | $446.72 |

## 5. Shadow Kelly-cap distribution (`sizing_shadow`)

- **Total `sizing_shadow` events:** 207
- **With kelly_cap defined:** 203
- **kelly_cap min/median/max:** $0.44 / $5.55 / $21.07
- **fixed_cap median:** $50.00
- **chosen_cap median (live):** $50.00
- **Mode breakdown:** {'fixed': 207}

## 6. Top symbols by shadow Kelly cap (mean kelly_cap, where defined)

| Symbol | n | mean kelly_cap | min | max |
|---|---|---|---|---|
| `xyz:LLY/USD` | 1 | $17.97 | $17.97 | $17.97 |
| `km:TENCENT/USD` | 1 | $12.62 | $12.62 | $12.62 |
| `xyz:BRENTOIL/USD` | 2 | $11.36 | $9.94 | $12.77 |
| `SUI/USD` | 1 | $10.98 | $10.98 | $10.98 |
| `xyz:SNDK/USD` | 1 | $10.56 | $10.56 | $10.56 |
| `hyna:FARTCOIN/USD` | 7 | $9.98 | $4.69 | $18.45 |
| `para:TOTAL2/USD` | 2 | $9.77 | $7.54 | $11.99 |
| `xyz:CL/USD` | 1 | $9.34 | $9.34 | $9.34 |
| `vntl:DEFENSE/USD` | 18 | $9.10 | $6.23 | $13.02 |
| `km:SMALL2000/USD` | 3 | $9.08 | $3.12 | $19.55 |

## 7. Net-notional trajectory

- **Samples:** 245
- **Min:** $-349.61
- **Max:** $698.63
- **Mean:** $87.19
- **Stdev:** $182.84
- **Samples with |net| > $250 (cap):** 53 (21.6%)
- **Net-cap blocks fired:** 27

Cap excursions are entirely MTM-driven (per `project_max_net_notional_is_entry_gate`). Every observed entry attempt past cap was blocked by `risk_gate_net_cap`.

## 8. Fill ledger summary

- **Total `hl_fill_received`:** 46
- **With complete schema (fee+pnl+hash):** 46/46 (100%)
- **Total fees paid:** $0.3750
- **Total realized closed_pnl:** $0.4913
- **Net realized P&L (closed_pnl − fees):** $0.1163

Top 10 by |closed_pnl|:

| Symbol | Fills | Fees | closed_pnl | Net |
|---|---|---|---|---|
| `ADA` | 2 | $0.0299 | $-0.1656 | $-0.1955 |
| `UNI` | 2 | $0.0089 | $-0.1288 | $-0.1377 |
| `xyz:SILVER` | 4 | $0.0105 | $0.1213 | $0.1109 |
| `XRP` | 2 | $0.0298 | $0.1188 | $0.0890 |
| `xyz:XYZ100` | 4 | $0.0117 | $0.1134 | $0.1017 |
| `BTC` | 2 | $0.0298 | $0.0877 | $0.0580 |
| `NEAR` | 2 | $0.0300 | $0.0851 | $0.0551 |
| `ENA` | 3 | $0.0375 | $0.0844 | $0.0469 |
| `xyz:TSLA` | 2 | $0.0060 | $-0.0634 | $-0.0693 |
| `para:BTCD` | 2 | $0.0870 | $-0.0608 | $-0.1478 |

## 9. Manual intervention count

**Manual interventions during soak:** 1

- **GOOGL** at 2026-04-28T23:35:04Z: manual_taker_buy, P&L $-0.05005, classification: `justified_earnings_precedent`

## 10. Verdict

✅ **Stage 2.5 acceptance: PASS**

All sample targets met, all stop-conditions clean, one justified manual intervention (GOOGL earnings close, classified per Stage 2.5 precedent).

Per `project_dust_soak_promotion_ladder.md`, Stage 3 is now formally available pending: Gate D wiring, expectation-band declaration, MAX_NET reduce-only design (per `docs/stage3_promotion_design.md`).
