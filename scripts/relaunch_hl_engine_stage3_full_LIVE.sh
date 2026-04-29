#!/bin/bash
# Stage 3 LIVE — full universe (natives + HIP-3) at $75 per trade.
#
# Promotion ladder rung after Stage 2.5 PASSED 2026-04-28
# (docs/stage2_5_end_of_soak_report.md). Sizing ratio 1.5 vs Stage 2.5
# matches the bands in config/expectation_bands.json.
#
# Stage 3 caps:
#   NOTIONAL_PER_TRADE_OVERRIDE = 75
#   MAX_NET_NOTIONAL            = 375
#   HL_SESSION_LOSS_GUARD_USD   = 112.5
#   MAX_NEW_ENTRIES_PER_SESSION = 0   (unlimited; MAX_NET + reduce-only govern)
#   PAUSE_NEW_ENTRIES           = 0
#
# Reduce-only hysteresis gate (commit 6f34339) becomes operationally
# meaningful here: |net| crossing 1.5 × MAX_NET = $562.50 activates;
# crossing 1.25 × MAX_NET = $468.75 deactivates. Defaults are inherited
# from hl_engine.py module constants (REDUCE_ONLY_K_ACTIVATE=1.5,
# REDUCE_ONLY_K_DEACTIVATE=1.25); not env-overridden so the default
# operator-approved thresholds remain authoritative for the soak.
#
# Universe + wallet/key/Z-override env are unchanged from Stage 2.5.

set -e
cd "$(dirname "$0")/.."

mkdir -p logs

env \
  ALPACA_API_KEY_ID=PKDITHFX2TLSPWAJABZ4QLSGMT \
  ALPACA_API_KEY_LIVE=AKMHY6GFQNIDH2ZZSE5VOU67PA \
  ALPACA_API_SECRET_KEY=GoRKCvGki3fQgyCGLaD4xFY2bAFzompuYueqGNeQHfdS \
  ALPACA_API_SECRET_LIVE=BrBkCwDMrzfWL2KjZHjkbTSvMRzpKVrei9PureePy37y \
  ALPACA_TRADING_MODE=live \
  EXECUTION_MODE=LIVE \
  EXECUTION_STYLE=maker \
  HIP3_DEXS="xyz,flx,vntl,hyna,km,cash,para" \
  HIP3_LEVERAGE=40 \
  HIP3_UNIVERSE="xyz:HIMS,xyz:HOOD,xyz:ORCL,xyz:EWY,xyz:XYZ100,xyz:CRWV,xyz:TSLA,xyz:CL,xyz:SNDK,xyz:SKHX,xyz:MSFT,xyz:MU,xyz:SP500,xyz:AMD,xyz:PLTR,xyz:BRENTOIL,xyz:GOLD,xyz:SILVER,xyz:NATGAS,xyz:COPPER,xyz:PLATINUM,xyz:TSM,xyz:GOOGL,xyz:META,vntl:ANTHROPIC,vntl:OPENAI,vntl:SPACEX,vntl:MAG7,vntl:SEMIS,vntl:NUCLEAR,vntl:BIOTECH,vntl:DEFENSE,vntl:ROBOT,hyna:HYPE,hyna:FARTCOIN,hyna:XMR,hyna:XPL,hyna:PUMP,hyna:ENA,hyna:IP,hyna:BNB,hyna:SUI,flx:USDE,flx:GAS,km:BMNR,km:USTECH,km:USOIL,km:SMALL2000,km:TENCENT,km:XIAOMI,km:RTX,cash:KWEB,cash:WTI,para:BTCD,para:OTHERS,para:TOTAL2,xyz:AAPL,xyz:LLY,xyz:NFLX,xyz:COST,xyz:BABA,xyz:RKLB,xyz:MRVL,xyz:EUR,xyz:JP225,xyz:XLE,xyz:PALLADIUM,xyz:URANIUM,xyz:RIVN" \
  HL_API_WALLET_ADDRESS=0xF42A54c658774566f62c3C405b210A93119A6E6a \
  HL_PAIRS_MAX_OPEN=5 \
  HL_PRIVATE_KEY=0x44571934d047012bb2b801166f81083c7fe76da246cb19afd180b7cf8602c665 \
  HL_UNIVERSE="BTC,ETH,SOL,AAVE,XRP,DOGE,PAXG,ARB,CRV,LINK,ADA,AVAX,LTC,BCH,DOT,UNI,LDO,POL,RENDER,FIL,HYPE,BNB,SUI,TAO,NEAR,ENA" \
  HL_WALLET_ADDRESS=0x32D178fc6BC4CCC7AFBDB7Db78317cF2Bbd6C048 \
  PAIR_NOTIONAL=25 \
  PER_PAIR_NOTIONAL=750 \
  Z_OVERRIDE_AAVE="-2.0,5.0,2.0,0.5" \
  Z_OVERRIDE_ETH="-1.25,-0.5,1.25,0.5" \
  Z_OVERRIDE_LDO="-2.0,5.0,2.0,0.5" \
  Z_OVERRIDE_SUI="-2.0,5.0,2.0,0.5" \
  Z_OVERRIDE_TAO="-2.0,5.0,2.0,0.5" \
  Z_OVERRIDE_ZEC="-2.0,5.0,99.0,99.0" \
  PAUSE_NEW_ENTRIES=0 \
  NOTIONAL_PER_TRADE_OVERRIDE=75 \
  MAX_NET_NOTIONAL=375 \
  MAX_NEW_ENTRIES_PER_SESSION=0 \
  HL_SESSION_LOSS_GUARD_USD=112.5 \
  nohup caffeinate -i venv/bin/python3 hl_engine.py \
    >> logs/hl_engine.stdout \
    2>> logs/hl_engine.stderr &

NEW_PID=$!
echo "$NEW_PID" > logs/hl_engine.pid
echo "Stage 3 LIVE PID=$NEW_PID  NOTIONAL=75  MAX_NET=375  LOSS_GUARD=112.5  MAX_NEW=0 (unlimited)  reduce-only at 1.5×/1.25× cap"
