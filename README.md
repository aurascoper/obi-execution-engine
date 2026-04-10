# OBI + Mean-Reversion Execution Engine
### Dual-Engine | HF Directional Mean Reversion (Crypto) + Statistical Arbitrage (Equities) | Async Python | Apple Silicon M4

A production-grade algorithmic trading system implementing two architecturally distinct
quantitative strategies across parallel paper-trading engines, grounded in the survey of
[`whitepaper.pdf`](whitepaper.pdf).

| Engine | Strategy Paradigm | Session | Directionality |
|--------|-------------------|---------|----------------|
| `live_engine.py` (Crypto) | **High-Frequency Directional Mean Reversion** | 24/7 continuous tick loop | Long-only (spot market, broker-constrained) |
| `equities_engine.py` (Equities) | **Statistical Arbitrage & Mean Reversion** | US RTH 09:30–16:00 ET strictly | Fully bidirectional (long + short, margin) |

Both engines share the same mathematical primitive — an **Ornstein–Uhlenbeck z-score** gated
by **Order-Book Imbalance** — but differ fundamentally in their execution regime, hedging
structure, and alpha source.

---

## Whitepaper Alignment

The [`whitepaper.pdf`](whitepaper.pdf) surveys four quantitative strategy families. The table
below maps each section to the engines and flags where each engine diverges from or extends
the paper.

| Whitepaper Section | Equities Engine | Crypto Engine | Notes |
|--------------------|:-:|:-:|-------|
| **§1 Mean-Reversion** — OU process, 60-day z-score, entry at 1.25σ, exit at 0.50σ | ✓ Full | ✓ Partial | Crypto uses 60-**minute** window, not 60-day — intentional divergence for HF micro-structure |
| **§1 Short selling** — sell short when `s_t > +s_entry` | ✓ Implemented | ✗ Not applicable | Alpaca does not support crypto short selling; equities engine is bidirectional |
| **§2 Statistical Arbitrage** — factor models, PCA, cointegration, beta-neutral portfolios | ✓ Partial | ✗ Not implemented | Equities uses sector-level exposure caps as a proxy for factor neutrality; no explicit PCA |
| **§3 Order-Book Imbalance** — ρ_t formula, Cartea et al. (2018) | ✓ Full (NBBO) | ✓ Full (L2) | Crypto uses true L2 depth at 5 levels; equities synthesizes NBBO quotes to a single-level OBI |
| **§4 Data & Feature Engineering** — rolling z-scores, L2 features, normalization | ✓ Full | ✓ Full | 60-day window (equities) vs 60-minute window (crypto); both normalize per-bar |
| **§5 Backtesting** — look-ahead bias, survivorship bias, transaction costs | — | — | Both engines are live/paper; backtesting is future work |
| **§6 Risk Management** — position sizing, drawdown, factor neutrality | ✓ Extended | ✓ Partial | Equities adds sector caps (Defense=1, Energy=1, Semis=2) not covered in whitepaper |

---

## Mathematical Basis

### §1 — Mean Reversion: Ornstein–Uhlenbeck Process

Prices are modeled as OU processes (Avellaneda & Lee, 2010):

$$
dX_t = \kappa(\mu - X_t)\,dt + \sigma\,dW_t
$$

where $X_t$ reverts to mean $\mu$ at speed $\kappa$. The normalized deviation (z-score) over
rolling window $W$ is:

$$
s_t = \frac{X_t - \hat{\mu}_W}{\hat{\sigma}_W}
$$

where $\hat{\mu}_W$ and $\hat{\sigma}_W$ are the sample mean and standard deviation (ddof=1)
over the most recent $W$ bars.

**Empirical thresholds** (Avellaneda & Lee, 2010 — §1 of whitepaper):

| Parameter | Value | Interpretation |
|---|---|---|
| $W$ | 60 bars | Rolling lookback |
| $s_{\text{entry}}$ | $1.25\sigma$ | Enter when oversold/overbought |
| $s_{\text{exit}}$ | $0.50\sigma$ | Exit when mean-reversion sufficient |

> **Timeframe divergence (§4 of whitepaper):** The whitepaper references Avellaneda & Lee's
> 60-**day** window for equity residuals. The **Equities engine** follows this exactly.
> The **Crypto engine** uses a 60-**minute** window by design — crypto trades 24/7 and the
> micro-structure mean-reversion is faster. The formula is identical; only $W$'s clock differs.

### §3 — Order-Book Imbalance (Cartea et al., 2018)

Static volume imbalance at depth $N$ (whitepaper §3):

$$
\rho_t = \frac{V^b_t - V^a_t}{V^b_t + V^a_t + \varepsilon}
$$

where $V^b_t$, $V^a_t$ are total bid/ask volume across the top $N=5$ levels.
$\varepsilon = 10^{-8}$ guards against empty-book division. $\rho_t \in (-1, 1)$;
positive values indicate net buy-side pressure.

Per Cartea et al. (2018): a buy-heavy imbalance predicts a higher probability of the next
market order being a buy and a short-term price uptick. The engine uses $\theta = 0$ (any net
buy pressure) as the gate condition.

**Engine-specific OBI data sources:**

| Engine | Data source | Levels |
|--------|-------------|--------|
| Crypto | Alpaca `CryptoDataStream` — true L2 orderbook snapshots | $N = 5$ |
| Equities | Alpaca `StockDataStream` — NBBO quotes synthesized to single-level OBI | $N = 1$ |

### Dual-Gate Entry Logic (§1 + §3 Combined)

Both gates must hold simultaneously on the same bar:

$$
\text{LONG ENTRY}: \quad s_t < -s_{\text{entry}} \;\wedge\; \rho_t > \theta
$$

$$
\text{LONG EXIT}: \quad s_t > -s_{\text{exit}}
$$

$$
\text{SHORT ENTRY (equities only)}: \quad s_t > +s_{\text{entry}} \;\wedge\; \rho_t < -\theta
$$

$$
\text{SHORT EXIT (equities only)}: \quad s_t < +s_{\text{exit}}
$$

The z-score confirms statistical overextension; OBI confirms microstructure liquidity pressure
before capital is committed.

> **Crypto engine is long-only.** The short-entry condition is not implemented in
> `live_engine.py`. Alpaca does not support crypto short selling. The whitepaper's §1
> short-spread logic ("sell short when $s_t > +s_{\text{entry}}$") applies only to the
> equities engine.

### §2 — Statistical Arbitrage: Sector Factor Proxy

The whitepaper's §2 discusses multi-factor models with beta-neutral portfolios:

$$
R_{it} = \alpha_i + \sum_{k=1}^{K} \beta_{ik} F_{kt} + \varepsilon_{it}
$$

The equities engine does not implement PCA or explicit factor regressions. Instead, it uses
**sector exposure caps** as a first-order approximation of factor neutrality: no more than
$c_s$ simultaneous open positions per GICS sector $s$:

$$
\text{cap}(s) = \begin{cases}
  1 & s \in \{\text{Defense},\, \text{Energy},\, \text{Energy ETF},\, \text{Nuclear Energy}\} \\
  2 & s = \text{Semiconductors} \\
  3 & \text{otherwise}
\end{cases}
$$

The macro overrides (Defense, Energy, Nuclear) are not from the whitepaper — they reflect
live geopolitical risk (Iran war, 2026-04) that destroys stationarity assumptions in those
sectors (see §1 stationarity discussion and Phase 3 §§ below).

---

## Engine Architectures

### Engine 1 — Crypto (`live_engine.py`) — High-Frequency Directional Mean Reversion

Unhedged directional entries on extreme negative Z-score deviations, gated by real-time L2
Order-Book Imbalance. Operates on a 24/7 continuous tick loop with no session boundaries.
Long-only — Alpaca's spot crypto API does not support short selling.

| Property | Value |
|----------|-------|
| Strategy | High-Frequency Directional Mean Reversion (unhedged, spot-only) |
| Universe | 28 crypto/USD pairs (see table below) |
| Session | **24/7** continuous — no market-hours gate |
| Timeframe | 1-minute bars (24/7 WebSocket stream) |
| Z-score window | $W = 60$ bars = **60-minute** rolling window |
| Directionality | **Long-only** — broker API limitation (spot market, no crypto shorts) |
| Entry signal | $s_t < -1.25\sigma$ (extreme negative deviation) **AND** $\rho_t > 0$ (OBI buy pressure) |
| Pre-seed | None — live warmup, ~60 min to first signal |
| OBI source | L2 orderbook via `CryptoDataStream` v1beta3 |
| Notional/trade | \$15 |
| Logs | `logs/engine.jsonl` |

**Universe (28 pairs):**

| Category | Symbols |
|----------|---------|
| L1 / Infrastructure | BTC, ETH, SOL, AVAX, ADA, DOT, LTC, BCH, XRP, XTZ |
| DeFi blue chips | LINK, AAVE, UNI, CRV, SUSHI, LDO, GRT, YFI |
| L2 / Utility | ARB, POL, FIL, RENDER |
| Liquid alts | DOGE, SHIB, BONK, PEPE, BAT |
| Precious metals (crypto) | PAXG (gold-backed ERC-20, tracks XAU/USD) |

*Excluded: TRUMP, WIF, HYPE, SKY, ONDO (illiquid/political meme); USDC, USDT, USDG (stablecoins).*

### Engine 2 — Equities (`equities_engine.py`) — Statistical Arbitrage & Mean Reversion

Hedged, sector-capped statistical arbitrage exploiting ±1.25σ deviations across a 138-symbol
curated universe. Operates strictly within US Regular Trading Hours; no positions are opened
or held outside 09:30–16:00 ET. Fully bidirectional — broker margin enables both long and
short entries, creating a naturally hedged book.

| Property | Value |
|----------|-------|
| Strategy | Statistical Arbitrage & Mean Reversion (hedged, sector-capped) |
| Universe | 138 equity symbols, curated (see below) |
| Session | **US RTH only** — 09:30–16:00 ET, strict boundary enforcement |
| Timeframe | Daily bars |
| Z-score window | $W = 60$ bars = **60 trading days** (~3 months) |
| Directionality | **Fully bidirectional** — long ($s_t < -1.25\sigma$) + short ($s_t > +1.25\sigma$), margin-funded |
| Entry signal | Z-score gate ±1.25σ **AND** OBI confirmation; sector cap must not be breached |
| Pre-seed | 60 daily IEX closes fetched at startup — warm on bar 1 |
| OBI source | NBBO quotes synthesized to single-level OBI via `StockDataStream` |
| Notional/trade | \$15 |
| Sector caps | Defense=1, Energy=1, Energy ETF=1, Nuclear=1, Semis=2, others=3 |
| Logs | `logs/equities_engine.jsonl` |

**Universe (138 symbols, curated) — screened 2026-04-09:**

*Quality filters: price > \$20, 30-day ADV > 1,000,000 shares.*

| Zone | Symbols |
|------|---------|
| Long zone screened (z < −1.25σ) | HRL, NKE, TSLA, NOW, SJM, EXE, PTC, PODD, VRSK, ZS, GEN, NTAP, CRM, DLTR, DG, DDOG, WDAY, LDOS, CTAS, INTU, CPB, SMCI, ISRG, MKC, GIS, PLTR, EL, COR, GPN, PAYX, PM, CSGP, GD, TTD, LEN, MOS, SYY, JKHY, ULTA, FICO, TSCO, ORCL, TEAM, CPRT, J, SNOW, CRWD |
| Short zone screened (z > +1.25σ) | INTC, MRVL, KLAC, MPWR, JBL, LRCX, STT, STX, FAST, SNDK, SBAC, ETR, WDC, TJX, ETN, COST, PPL, HUBB, RL, GLW, TER, Q, HLT, WAB, ROST, FIX, GEV, VRSN, NI, LITE, HPE, DELL, SRE, DLR, TGT, GL, KEYS, CMI, CMS, COHR, NFLX, FTV, PNW, ODFL, WEC, MAR, LNT, NTRS, GRMN, EME, VRT, EQIX, CTVA, GWW, FE, EVRG, LYV, SLB, CSCO, DTE, STZ, FCX, EIX, ED, TSN, CNP, CSX, DUK |
| Russell 3000 longs | BKNG, AXON, VEEV, ADBE, HUBS, ADSK, BSX, MDB, ABT, ADP, NTNX, GWRE, MANH |
| Russell 3000 shorts | CAR, PVH, FLEX, SNX, BK, C, BURL, CROX, HOG |
| Precious metals ETF | GLD (\$438, ADV 7M), SLV (\$68, ADV 16M) |
| Energy ETF ⚠️ cap=1 | USO (\$127, ADV 5M) — Iran war crude spike risk |
| Nuclear Energy ⚠️ cap=1 | URA (\$51, ADV 1.5M) — Iran nuclear program sensitivity |

*Screener: `python3 screener.py --new-only`. Excluded: PPLT, PALL, CPER, URNM (fail ADV), UNG (price < \$20).*

---

## Phase 3 Roadmap — From Taker Baseline to Maker Algorithm

Phase 3 is grounded in two problems identified in the whitepaper and confirmed by live taker
execution data:

### Problem 1 — Stationarity Death at Macro Events (Whitepaper §1, §6)

The whitepaper (§1) requires stationarity for the OU model to hold. At tier-1 macro prints
(CPI 08:30 ET, FOMC 14:00 ET, NFP first Friday 08:30 ET), the market instantly reprices
assets — the $-2.5\sigma$ dip **is** the new mean, not a rubber-band deviation. Simultaneously,
market makers pull L2 liquidity (whitepaper §3, §5: "model execution latency and order-book
dynamics"), widening spreads before the print.

**Solution: Macro Kill Switch** (extends whitepaper §6 risk management)

$$
\text{HALT if } \exists\, e \in \mathcal{E}_{\text{tier-1}} : |t - t_e| \leq 15\,\text{min}
$$

where $\mathcal{E}_{\text{tier-1}} = \{\text{CPI, NFP, FOMC, PCE, PPI, GDP, Retail Sales, Jobless Claims, JOLTS}\}$.

On halt entry: cancel all pending orders, freeze signal evaluation. On halt exit: resume.
Calendar sourced from Financial Modeling Prep API (free tier, 250 req/day).

New file: `risk/macro_calendar.py` — `MacroCalendar` class with 6-hour cache TTL.

### Problem 2 — Taker Fees and Adverse Spread (Whitepaper §3, §5)

Current execution crosses the spread (taker):

$$
P_{\mathrm{lim}} = P_{\text{ref}} \times (1 + \delta_{\text{slip}}), \quad \delta_{\text{slip}} = 0.10\%
$$

The whitepaper (§3) cites Cartea et al.'s finding that OBI-aware execution — posting
**passive** limit orders at the best bid/ask rather than crossing — outperforms naive
order splitting. The whitepaper (§5) notes: *"simulate order-book dynamics: only mark trade as
filled if opposite liquidity exists."*

Phase 3 synthesizes the OBI signal (already live) into the execution layer:

**Maker pivot:**

$$
\text{LONG entry}: \quad P_{\mathrm{lim}} = P^b_t \quad \text{(join bid queue)}
$$

$$
\text{SHORT entry}: \quad P_{\mathrm{lim}} = P^a_t \quad \text{(join ask queue)}
$$

$$
\text{All exits}: \quad P_{\mathrm{lim}} = P_{\text{ref}} \times (1 \pm \delta_{\text{slip}}) \quad \text{(taker, urgency)}
$$

**Adverse selection guard** (new `risk/order_tracker.py`):

Because passive orders can be stranded when price moves adversely, an async cancel/replace
loop walks the order with the market:

$$
\text{replace if } \frac{|P^{b/a}_t - P_{\mathrm{lim}}|}{P_{\mathrm{lim}}} > 0.1\%, \quad \Delta t_{\text{replace}} \geq 2\,\text{s}
$$

Replacement uses Alpaca's atomic `replace_order_by_id()` — no cancel-race window.

**Build sequence for Phase 3:**

```
1. config/risk_params.py    — MACRO_HALT_WINDOW_MINUTES, ORDER_TRACKER constants
2. config/settings.py       — fmp_api_key field
3. risk/circuit_breaker.py  — _macro_halted flag (separate from drawdown _halted)
4. strategy/signals.py      — best_bid slot + best_prices() accessor
5. risk/macro_calendar.py   — new: MacroCalendar (FMP + 6h cache)
6. risk/order_tracker.py    — new: OrderTracker (cancel/replace loop)
7. execution/order_manager.py — submit_maker() passive method
8. live_engine.py + equities_engine.py — wire _macro_watch() + OrderTracker to TaskGroup
```

---

## Architecture

```
live_trading/
├── live_engine.py            Crypto engine — TaskGroup(feed, strategy, drawdown)
├── equities_engine.py        Equities engine — TaskGroup(feed, strategy, drawdown, sector_guard)
├── screener.py               Universe scanner (MIN_PRICE=$20, MIN_ADV=1M, CLI filters)
├── config/
│   ├── settings.py           Env-driven credentials — os.environ only, zero hardcoding
│   ├── risk_params.py        Circuit breaker constants, notional caps
│   └── universe.py           SECTOR_MAP (142 symbols), SECTOR_CAPS
├── data/
│   ├── feed.py               CryptoDataStream v1beta3 — bars + L2 orderbooks + quotes
│   └── stock_feed.py         StockDataStream — bars + NBBO quotes (synthesized OBI)
├── strategy/
│   └── signals.py            SignalEngine: _RollingBuffer (O(1)), _SymbolState, dual-gate
├── execution/
│   └── order_manager.py      submit_limit() taker (current) | submit_maker() Phase 3
├── risk/
│   ├── circuit_breaker.py    Drawdown watchdog — zero strategy imports by design
│   ├── sector_tracker.py     SectorExposureTracker — O(1) check/open/close
│   ├── macro_calendar.py     [Phase 3] MacroCalendar — FMP economic calendar
│   └── order_tracker.py      [Phase 3] OrderTracker — cancel/replace loop
└── logs/
    ├── engine.jsonl           Crypto engine structured JSON (gitignored)
    └── equities_engine.jsonl  Equities engine structured JSON (gitignored)
```

### Signal Pipeline

```
1-min bars (crypto) / Daily bars (equities)
         │
         ▼
   _RollingBuffer.push(close)         ← O(1) circular float64 array, 480 bytes/symbol
         │
         ▼
   s_t = (close − μ_W) / σ_W          ← None if count < 60 (warmup)
         │
         ├─ in_position=True?
         │      └─ s_t > s_exit → exit_signal, reset state
         │
         └─ in_position=False?
                ├─ s_t < −s_entry?    (oversold gate)
                └─ ρ_t > θ?           (OBI buy-pressure gate)
                         │
                         ▼
                   size_order()        → floor(notional / price, decimals)
                         │
                         ▼
                 [Phase 3] submit_maker()  →  limit at best_bid / best_ask
                 [Current] submit_limit()  →  limit at ref_px × (1 + δ)

L2 snapshots (~1000/min crypto) / NBBO quotes (~500/min equities)
         │
         ▼
   ρ_t = (ΣV^b − ΣV^a) / (ΣV^b + ΣV^a + ε)
         │
         └─ cached in _SymbolState.obi — gates next bar evaluation
```

---

## Risk Controls

All thresholds are hardcoded constants in `config/risk_params.py`. `CircuitBreaker` has
**zero imports from `strategy/`** by design.

| Control | Value | Trigger |
|---------|-------|---------|
| Daily drawdown halt | 2% equity | Hard stop — engine exits, feed closes |
| Max order notional | \$15 | Per-order cap (Alpaca minimum \$10) |
| Per-symbol cap | \$500–\$5,000 | `SYMBOL_CAPS` dict |
| API rate limit | 30 orders/min | Token bucket (Alpaca allows 200/min) |
| Sector exposure | 1–3 positions | `SectorExposureTracker` O(1) check |
| Macro halt window | ±15 min | [Phase 3] FMP calendar, tier-1 events only |
| Rollback on block | on submit error | `signals.rollback_entry()` resets state |

---

## Quickstart

```bash
# 1. Credentials
source env.sh   # env.sh is gitignored; contains ALPACA_API_KEY_ID, ALPACA_API_SECRET_KEY

# 2. Install
pip install -r requirements.txt

# 3. Run both engines (paper mode)
nohup /path/to/venv/bin/python3 live_engine.py     >> logs/engine.jsonl          2>&1 &
sleep 5
nohup /path/to/venv/bin/python3 equities_engine.py >> logs/equities_engine.jsonl 2>&1 &

# 4. Screen for new signals
python3 screener.py --new-only
python3 screener.py --sector Financials --min-z 1.5

# 5. Monitor live signals
tail -f logs/engine.jsonl | python3 -c \
  "import sys,json; [print(json.dumps(json.loads(l),indent=2)) for l in sys.stdin
   if any(k in l for k in ['entry_signal','exit_signal','order_submitted','macro_halt'])]"
```

---

## Execution Modes

| `EXECUTION_MODE` | `ALPACA_TRADING_MODE` | Orders | Capital |
|---|---|---|---|
| `SHADOW` | `paper` | No | None |
| `PAPER` | `paper` | Yes | Paper only |
| `LIVE` | `live` | Yes | Real capital |

`config/settings.py` enforces that `LIVE` mode requires `ALPACA_TRADING_MODE=live` —
mismatched flags raise `RuntimeError` before any connection is made.

---

## Build Provenance

Built via a three-model pipeline:

1. **Architecture** — Gemini 3.1 Pro generated the system directive, execution phases,
   directory structure, and circuit breaker spec for an M4 async engine.

2. **Quantitative research** — ChatGPT Deep Research synthesized the strategy thesis into
   [`whitepaper.pdf`](whitepaper.pdf) drawing exact formulas and empirical thresholds from
   Avellaneda & Lee (2010), Cartea et al. (2018), and Cont & De Larrard (2013).

3. **Implementation** — Claude Sonnet 4.6 (Claude Code) translated the whitepaper math into
   `SignalEngine`, `_RollingBuffer`, and the sector/macro risk layers; extended the engine
   from a 7-symbol crypto prototype to a 170-symbol dual-engine system; and designed the
   Phase 3 maker-pivot architecture grounded in live taker execution data.

---

## References

- Avellaneda, M. & Lee, J.H. (2010). Statistical arbitrage in the U.S. equities market.
  *Quantitative Finance*, 10(7), 761–782.
- Cartea, Á., Jaimungal, S. & Penalva, J. (2015). *Algorithmic and High-Frequency Trading*.
  Cambridge University Press.
- Cont, R. & De Larrard, A. (2013). Price dynamics in a Markovian limit order market.
  *SIAM Journal on Financial Mathematics*, 4(1), 1–25.
- Gatev, E., Goetzmann, W.N. & Rouwenhorst, K.G. (2006). Pairs trading: Performance of a
  relative-value arbitrage rule. *Review of Financial Studies*, 19(3), 797–827.
- hftbacktest. Market Making with Alpha — Order Book Imbalance.

---

## License

MIT
