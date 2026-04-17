# OBI + Mean-Reversion Execution Engine
### Tri-Engine | Mean Reversion (Crypto Spot) + Statistical Arbitrage (Equities) + Bi-Directional Perps (Crypto + HIP-3 Equity/Commodity, Hyperliquid) | Async Python | Apple Silicon M4

A production-grade algorithmic trading system implementing three architecturally distinct
quantitative strategies across parallel execution engines, grounded in the survey of
[`whitepaper.pdf`](whitepaper.pdf).

| Engine | Strategy Paradigm | Session | Directionality |
|--------|-------------------|---------|----------------|
| `live_engine.py` (Alpaca Crypto Spot) | **High-Frequency Directional Mean Reversion** | 24/7 continuous tick loop | Long-only (spot market, broker-constrained) |
| `equities_engine.py` (Alpaca Equities) | **Statistical Arbitrage & Mean Reversion** | US RTH 09:30–16:00 ET strictly | Fully bidirectional (long + short, margin) |
| `hl_engine.py` (Hyperliquid Perps) | **Bi-Directional Z-Score Maker on Perp Futures** | 24/7 continuous tick loop | Fully bidirectional (long + short; native crypto 10× cross, HIP-3 equity perps 5× isolated) |

Both engines share the same mathematical primitive — an **Ornstein–Uhlenbeck z-score** gated
by **Order-Book Imbalance** — but differ fundamentally in their execution regime, hedging
structure, and alpha source.

---

## Whitepaper Alignment

The [`whitepaper.pdf`](whitepaper.pdf) surveys four quantitative strategy families. The table
below maps each section to the engines and flags where each engine diverges from or extends
the paper.

> The **"Crypto Engine"** column below spans two engines: `live_engine.py` (Alpaca spot,
> long-only) and `hl_engine.py` (Hyperliquid perps, bi-directional). Where the two
> diverge, the cell calls it out explicitly.

| Whitepaper Section | Equities Engine | Crypto Engine | Notes |
|--------------------|:-:|:-:|-------|
| **§1 Mean-Reversion** — OU process, 60-day z-score, entry at 1.25σ, exit at 0.50σ | ✓ Full | ✓ Partial | Crypto uses 60-**minute** window, not 60-day — intentional divergence for HF micro-structure |
| **§1 Short selling** — sell short when `s_t > +s_entry` | ✓ Implemented | ✓ HL only | `hl_engine.py` implements the short-spread exactly (`allow_short=True`); Alpaca spot crypto (`live_engine.py`) can't short — API limitation |
| **§2 Statistical Arbitrage** — factor models, PCA, cointegration, beta-neutral portfolios | ✓ Partial | ✗ Not implemented | Equities uses sector-level exposure caps as a proxy for factor neutrality; no explicit PCA |
| **§3 Order-Book Imbalance** — ρ_t formula, Cartea et al. (2018) | ✓ Full (NBBO) | ✓ Full (L2) | `live_engine.py` N=5, `hl_engine.py` N=20 (burn-in deepened to avoid front-row MM flicker); equities synthesizes NBBO quotes to single-level OBI |
| **§4 Data & Feature Engineering** — rolling z-scores, L2 features, normalization | ✓ Full | ✓ Full | 60-day window (equities) vs 60-minute window (crypto); both normalize per-bar |
| **§5 Backtesting** — look-ahead bias, survivorship bias, transaction costs | — | — | All engines are live/paper; backtesting is future work |
| **§6 Risk Management** — position sizing, drawdown, factor neutrality | ✓ Extended | ✓ Extended (HL) | Equities adds sector caps (Defense=1, Energy=1, Semis=2); HL adds pre-submit flip-guard + authoritative flat-sweep reconciler not covered in whitepaper |

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

### Engine-Specific OBI Data Sources & Execution Regimes

Three execution paths with unified signal logic but different OBI sources and market access:

| Engine | Asset Class | Data Source | OBI Levels | Bidirectional |
|--------|-------------|-------------|-----------|---|
| **live_engine.py** | Crypto Spot (Alpaca) | `CryptoDataStream` v1beta3 — L2 orderbook snapshots | N = 20 | ❌ Long-only |
| **hl_engine.py** | Crypto Perps (Hyperliquid) | `LiveFeed` (Alpaca bars) + `HyperliquidFeed` (HL L2) | N = 20 | ✅ Bidirectional |
| **equities_engine.py** | US Equities | `StockDataStream` — NBBO quotes synthesized to single-level OBI | N = 1 | ✅ Bidirectional |

**OBI Depth Rationale:**

Both crypto engines operate at **N = 20 levels** (deepened from N = 5 after live burn-in). Early testing revealed front-row market-maker flicker at levels 1–5 that contradicted the deeper committed book — specifically, a "bid-side façade at levels 1–5 over an ask-heavy 6–20" pattern on ETH. Deepening to 20 levels reduces OBI volatility by **2.6–5.8×** and eliminates spurious sign flips that blocked valid entries.

Equities use N = 1 (NBBO top-of-book only) because `StockDataStream` does not expose L2 orderbooks (crypto-only feature). The daily bar timeframe requires less granular microstructure, and the quote rate (~500/min) keeps OBI fresh between daily bar events.

### Dual-Gate Entry Logic (§1 + §3 Combined)

Both gates must hold simultaneously on the same bar:

$$
\text{LONG ENTRY}: \quad s_t < -s_{\text{entry}} \;\wedge\; \rho_t > \theta
$$

$$
\text{LONG EXIT}: \quad s_t > -s_{\text{exit}}
$$

$$
\text{SHORT ENTRY (bidirectional engines only)}: \quad s_t > +s_{\text{entry}} \;\wedge\; \rho_t < -\theta
$$

$$
\text{SHORT EXIT (bidirectional engines only)}: \quad s_t < +s_{\text{exit}}
$$

The z-score confirms statistical overextension; OBI confirms microstructure liquidity pressure
before capital is committed.

**Implementation per engine:**

- **Crypto spot (`live_engine.py`)** is long-only. Alpaca does not support crypto short selling; the short-entry gate is never evaluated (`allow_short=False`). The whitepaper's §1 short-spread logic does not apply to this engine.
- **Hyperliquid perps (`hl_engine.py`)** is fully bidirectional (`allow_short=True`). Short entries fire when $s_t > +1.25\sigma$ **AND** $\rho_t < 0$ (sell pressure detected in HL L2).
- **Equities (`equities_engine.py`)** is fully bidirectional via margin account. Short entries mirror long entries with opposite sign.

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
| Notional/trade | \$100 LIVE / \$1,500 PAPER |
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

### Engine 3 — Hyperliquid Perps (`hl_engine.py`) — Bi-Directional Z-Score Maker

Same mean-reversion primitive as Engine 1, but on a decentralized perp venue that permits
shorting. The universe spans two asset classes on the same chain: **native HyperCore crypto
perps** (BTC, ETH, SOL, …) and **HIP-3 builder-deployed equity/commodity perps** via
TradeXYZ (TSLA, MSFT, CL, BRENTOIL, …). Bar sources are hybrid: Alpaca spot bars for
coins Alpaca supports and HL-native synthesized bars for all others. Order book and
execution go direct to Hyperliquid (L2 WebSocket + exchange POST). The engine runs against
**real capital on HL mainnet** — Hyperliquid has no paper sandbox.

| Property | Value |
|----------|-------|
| Strategy | Bi-Directional OBI-Gated Z-Score — dual-mode execution (taker / maker) with dynamic urgency routing |
| Universe | 28 coins: 8 native crypto (`HL_UNIVERSE`) + 20 HIP-3 equity/commodity perps (`HIP3_UNIVERSE`); per-coin `szDecimals` + dust caps from `Info.meta()` + `Info.meta(dex=...)` at boot |
| Session | **24/7** continuous — no market-hours gate |
| Timeframe | 1-minute bars (Alpaca CryptoDataStream for supported coins; HL-native L2 midprice synthesis for others) |
| Z-score window | $W = 60$ bars = 60-minute rolling window |
| Z-score thresholds | **Per-coin tiers via RMSD calibration:** crypto majors ±1.25σ / ±0.50σ; crypto alts ±2.50σ / ±0.75σ; HIP-3 equities ±1.50σ / ±0.30σ; HIP-3 indices/ETFs ±1.75σ / ±0.40σ. Tiers auto-assigned by `screener_hip3.assign_z_tier()` based on 4h RMSD coefficient of variation, refined at boot by `_recalibrate_hip3_z()` over the preseed buffer. |
| OBI source | HL L2 WebSocket @ 20 levels (`l2Book` channel) |
| Leverage | Native crypto: 10× cross. HIP-3: 5× isolated (builder-deployed assets reject cross margin). Per-coin overrides via `leverage_map`. |
| Notional/trade | \$250 (crypto) / \$100 (HIP-3, per SYMBOL\_CAPS) |
| Execution style | `EXECUTION_STYLE=maker` — see dynamic urgency matrix below |
| Strategy tag | `hl_z` |
| Pre-seed | 240 × 1-min candles fetched from HL `candleSnapshot` API at boot for all coins — z-score buffer warm on bar 1 |
| Logs | `logs/hl_engine.jsonl` |

**Venue precision rules** (enforced by `hl_engine._submit`):

$$
\text{Tick}: \quad \text{price decimals} \leq \max(6 - \text{szDecimals}, 0) \;\wedge\; \text{sigfigs} \leq 5
$$

$$
\text{Lot}: \quad \text{qty} \equiv 0 \pmod{10^{-\text{szDecimals}}} \quad \text{(floor, never round up)}
$$

For BTC (`szDecimals=5`) at \$75k: integer-only prices, 5-decimal qty.

**HL response quirk — `status=ok` lies.** The exchange returns the envelope
`{"status":"ok","response":{...}}` even when the validator rejects the order. The true
per-order verdict is `response.data.statuses[i].error`. `_submit` parses this and
returns `None` on inner-error to trigger `rollback_entry`, preventing a phantom
in-memory position.

**Flip-guard with authoritative state wipe.** Before every submission, the engine
queries live HL positions and compares against in-memory intent. If live is flat but
memory holds a non-zero position, `reconcile_hl_positions` wipes the stale memory entry
(rather than just adding live non-zero positions). Without this sweep, any missed fill
or manual UI trade deadlocked every subsequent exit ("`exit_but_live_flat`" block loop).
Sub-lot residuals (`|szi| ≤ 1.5 × 10⁻szDecimals`) are treated as flat — eliminates a
deadlock observed on ETH where −0.0001 dust perpetually re-seeded the exit signal.

**Universe expansion workflow.** Two screeners select candidates for each asset class:

- `screener_hl.py` — ranks native HL crypto perps by volatility and liquidity.
- `screener_hip3.py` — scores all TradeXYZ (HIP-3) equity/commodity perps by **4h RMSD × liquidity × diversification** across sector categories (stock, ETF, index, commodity). Outputs a top-N portfolio with z-tier and leverage assignments.

```bash
python3 screener_hip3.py --top 20 --apply   # prints shell export block for engine launch
```

The engine reads `HL_UNIVERSE` (native crypto CSV) and `HIP3_UNIVERSE` (prefixed HIP-3 CSV)
from environment variables. Expansion is always an explicit, operator-reviewed change.

#### Phase 4.2 — Maker Execution (`EXECUTION_STYLE=maker`)

Live taker burn-in revealed transaction fees at **~210% of gross P&L** — the whitepaper
§3/§5 concern made concrete. The maker pivot posts `Alo` orders at the non-crossing
best (best_bid for buys, best_ask for sells) and rests until filled. HL fee flip:

$$
\text{Taker RT fee: } 2 \times (-3.5\ \text{bps}) = -7\ \text{bps}
\quad\longrightarrow\quad
\text{Maker RT fee: } 2 \times (+0.5\ \text{bps}) = +1\ \text{bps}
$$

The maker path is built as three composable spikes (all shipped):

| Spike | File | What it adds |
|---|---|---|
| **A** — userFills WS | `data/hl_feed.py` | Address-scoped `userFills` subscription; normalizes HL fill payloads into `{type: "hl_fill", cloid, side, ...}` on the engine queue. `isSnapshot` replay skipped to avoid double-firing `on_fill`. |
| **B** — Alo submit + cloid | `execution/hl_manager.py` | `Cloid` round-trip on `submit_order`; `cancel_by_cloid(coin, cloid)`; inner-rejection detection (HL's `status=ok` ⊕ `statuses[i].error` pattern). Supports `perp_dexs` passthrough and per-coin `leverage_map` for HIP-3 assets. |
| **C** — Reprice watchdog | `hl_engine.py::_maker_watchdog` | 1 s sweep over `_pending_resting`. Cancel+resubmit when market moves behind the queue; give up after lifetime/reprice limits. Partial-fill tolerant: cumulative fills track until remainder ≤ ½ lot. |

**Dynamic urgency routing.** Native crypto and HIP-3 equity perps have fundamentally
different order book microstructures. The `_submit()` router branches on asset class
and signal type:

| Scenario | Order Type | Watchdog Limits | Rationale |
|---|---|---|---|
| Native crypto entry/exit | Maker (Alo) | 30 s / 5 reprices | Deep books, fast taker flow — tight leash prevents stale quotes |
| HIP-3 entry, \|z\| < 3.0 | Maker (Alo) | 120 s / 15 reprices | Wider spreads, sparser takers — patience needed for organic fill |
| HIP-3 entry, \|z\| ≥ 3.0 | **IOC taker** | — | Extreme signal — adverse selection risk too high to rest a quote |
| HIP-3 exit (always) | **IOC taker** | — | Mean-reversion exits fire while price snaps to mean — Alo sits on wrong side of move |

The exit-always-taker rule reflects a key mean-reversion asymmetry: an unfilled entry
is a missed opportunity, but an unfilled exit is holding risk after the statistical edge
has evaporated. At $100 notional per HIP-3 coin, the 3–4 bps taker fee is negligible
vs. the slippage from a 120 s timeout while price reverts.

`cid` (client order id) persists across reprices so `SignalEngine.on_fill` routes
a final fill to the original intent regardless of how many times the `cloid` rolled.

#### Phase 4.3 — Partial-Fill Accumulation Fix + Trending-Regime Safety Net

Two corrections surfaced by the first day of live maker runs on the expanded universe.

**Partial-fill accumulation (`hl_engine.py::_handle_hl_fill`).** `SignalEngine.on_fill`
overwrites `positions[tag]` rather than accumulating, which is correct for a single-shot
taker cross but incorrect for a maker order that fills in multiple chunks. A SOL short
that filled in three chunks (0.38 + 0.20 + 5.27) left memory at the last chunk's size
(−5.27) instead of the true total (−5.85). The subsequent exit sized 5.27, leaving 0.58
on chain; a reconcile-driven exit then filled 0.57, leaving a 0.01 dust residual below
the sub-lot dust cap that never got swept. Fix: call `on_fill` exactly once per order —
on the terminal fill, with `cumulative` as the qty. Same fix also closes the inverse
bug on multi-chunk exits where partials 2+ would resurrect a phantom entry.

**Stop-loss + time-stop (`strategy/signals.py::evaluate`).** Mean-reversion has no
theoretical floor on adverse drawdown in a trending regime; a live session with sustained
upward drift produced a SOL short at −3.44% that the z-exit gate couldn't close because
the z-score kept climbing. Added two independent safety valves:

$$
\text{Hard stop:} \quad \frac{|P_t - P_{\text{entry}}|}{P_{\text{entry}}} \geq 1\% \text{ against direction} \quad\Longrightarrow\quad \text{force exit}
$$

$$
\text{Time stop:} \quad t - t_{\text{entry}} \geq \begin{cases} 30\ \text{min} & \text{US RTH (M-F 09:30–16:00 ET)} \\ 60\ \text{min} & \text{overnight / weekends} \end{cases} \quad\Longrightarrow\quad \text{force exit}
$$

Either condition triggers an `exit_signal` with `reason=stop_loss_<pnl>` or `reason=time_stop_<secs>`
regardless of z-score state. Entry timestamp (`entry_ts`) is a new per-tag field on
`_SymbolState`, populated at entry and at reconcile (adopt-now semantics → reconciled
positions get the full budget before a time-stop fires). Constants live in `signals.py`:
`STOP_LOSS_PCT=0.010`, `MAX_POSITION_SECS_RTH=1800`, `MAX_POSITION_SECS_OVN=3600`.
No changes to `risk/` or `config/risk_params.py`.

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
| Notional/trade | \$100 LIVE / \$1,500 PAPER |
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
├── hl_engine.py              Hyperliquid perps — TaskGroup(alpaca_bars, hl_orderbook, obi_pump, strategy)
├── fund_perp.py              One-shot HL spot→perp USDC transfer utility
├── screener.py               Equities universe scanner (MIN_PRICE=$20, MIN_ADV=1M)
├── screener_hip3.py          HIP-3 portfolio selector (RMSD × liquidity × diversification)
├── config/
│   ├── settings.py           Env-driven credentials (Alpaca + HL) via os.environ + .env
│   ├── risk_params.py        Circuit breaker constants, notional caps (LIVE/PAPER ternary)
│   └── universe.py           SECTOR_MAP (142 symbols), SECTOR_CAPS
├── data/
│   ├── feed.py               CryptoDataStream v1beta3 — bars + L2 orderbooks + quotes
│   ├── stock_feed.py         StockDataStream — bars + NBBO quotes (synthesized OBI)
│   └── hl_feed.py            Hyperliquid WebSocket — l2Book channel → unified messages
├── strategy/
│   └── signals.py            SignalEngine: _RollingBuffer, _SymbolState, reconcile_hl_positions
├── execution/
│   ├── order_manager.py      Alpaca submit_limit() taker | submit_maker() Phase 3
│   └── hl_manager.py         Hyperliquid exchange client, leverage pin, submit_order
├── risk/
│   ├── circuit_breaker.py    Drawdown watchdog — zero strategy imports by design
│   ├── sector_tracker.py     SectorExposureTracker — O(1) check/open/close
│   ├── macro_calendar.py     [Phase 3] MacroCalendar — FMP economic calendar
│   └── order_tracker.py      [Phase 3] OrderTracker — cancel/replace loop
└── logs/
    ├── engine.jsonl           Crypto engine structured JSON (gitignored)
    ├── equities_engine.jsonl  Equities engine structured JSON (gitignored)
    └── hl_engine.jsonl        Hyperliquid engine structured JSON (gitignored)
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
| Max order notional | \$100 LIVE / \$1,500 PAPER | Per-order cap (ternary on `EXECUTION_MODE`) |
| Max daily loss | \$50 LIVE / \$500 PAPER | Hard halt on cumulative intraday loss |
| Per-symbol cap | \$500–\$5,000 (crypto/equities), \$100 (HIP-3) | `SYMBOL_CAPS` dict |
| API rate limit | 30 orders/min | Token bucket (Alpaca allows 200/min) |
| Sector exposure | 1–3 positions | `SectorExposureTracker` O(1) check |
| Macro halt window | ±15 min | [Phase 3] FMP calendar, tier-1 events only |
| HL flip guard | pre-submit live check | Blocks if on-chain state disagrees with memory |
| HL reconcile sweep | on startup + on mismatch | Wipes mem-non-zero-but-live-flat entries |
| Rollback on block | on submit error or inner-reject | `signals.rollback_entry()` resets state |

---

## Quickstart

```bash
# 1. Credentials
source env.sh   # Alpaca keys (gitignored). Contains ALPACA_API_KEY_ID, ALPACA_API_SECRET_KEY
# For Hyperliquid, a .env file at repo root provides:
#   HL_WALLET_ADDRESS=0x...
#   HL_PRIVATE_KEY=0x...        (master wallet — agent keys can't do USD transfers)
# config/settings.py loads .env non-overriding, so env.sh values still win.

# 2. Install
pip install -r requirements.txt

# 3a. Run Alpaca engines (paper mode)
nohup /path/to/venv/bin/python3 live_engine.py     >> logs/engine.jsonl          2>&1 &
sleep 5
nohup /path/to/venv/bin/python3 equities_engine.py >> logs/equities_engine.jsonl 2>&1 &

# 3b. Run Hyperliquid engine (mainnet — real capital)
#     First time only: fund the perp clearinghouse
python3 fund_perp.py
#     28-coin hybrid launch: 8 native crypto + 20 HIP-3 equity/commodity perps.
#     HIP3_DEXS enables the TradeXYZ builder DEX alongside native HyperCore.
EXECUTION_MODE=LIVE ALPACA_TRADING_MODE=live EXECUTION_STYLE=maker \
  HL_UNIVERSE="BTC,ETH,SOL,DOGE,AVAX,LINK,kSHIB,HYPE" \
  HIP3_DEXS="xyz" \
  HIP3_UNIVERSE="xyz:HIMS,xyz:HOOD,xyz:CRCL,xyz:ORCL,xyz:EWY,xyz:XYZ100,xyz:COIN,xyz:CRWV,xyz:TSLA,xyz:CL,xyz:SNDK,xyz:MSTR,xyz:SKHX,xyz:MSFT,xyz:MU,xyz:SP500,xyz:AMD,xyz:PLTR,xyz:BRENTOIL,xyz:INTC" \
  HIP3_LEVERAGE=5 \
  nohup caffeinate -i venv/bin/python3 hl_engine.py &

# 4. Screen for new signals (Alpaca universe only)
python3 screener.py --new-only
python3 screener.py --sector Financials --min-z 1.5

# 5. Monitor live signals
tail -f logs/hl_engine.jsonl | python3 -c \
  "import sys,json; [print(json.dumps(json.loads(l),indent=2)) for l in sys.stdin
   if any(k in l for k in ['entry_signal','exit_signal','hl_order_intent','hl_order_inner_rejection'])]"
```

---

## Execution Modes

| `EXECUTION_MODE` | `ALPACA_TRADING_MODE` | Alpaca Orders | Alpaca Capital | Hyperliquid |
|---|---|---|---|---|
| `SHADOW` | `paper` | No | None | Subscribes to L2 + logs intent, does not submit. Per-coin shadow via `SHADOW_COINS` env var (HIP-3 burn-in). |
| `PAPER` | `paper` | Yes | Paper only | **N/A** — HL has no paper sandbox |
| `LIVE` | `live` | Yes | Real capital | Real capital on mainnet (native crypto + HIP-3 equity perps) |

`config/settings.py` enforces that `LIVE` mode requires `ALPACA_TRADING_MODE=live` —
mismatched flags raise `RuntimeError` before any connection is made. The Hyperliquid
path only exists in `SHADOW` (observe-only) and `LIVE` (real submission); there is no
mainnet/testnet switch in `hl_engine.py` — HL testnet is a separate URL the engine
does not target.

---

## Build Provenance

Built via a three-model pipeline:

1. **Architecture** — Gemini 3.1 Pro generated the system directive, execution phases,
   directory structure, and circuit breaker spec for an M4 async engine.

2. **Quantitative research** — ChatGPT Deep Research synthesized the strategy thesis into
   [`whitepaper.pdf`](whitepaper.pdf) drawing exact formulas and empirical thresholds from
   Avellaneda & Lee (2010), Cartea et al. (2018), and Cont & De Larrard (2013).

3. **Implementation** — Claude (Sonnet 4.6 → Opus 4.6, Claude Code) translated the whitepaper
   math into `SignalEngine`, `_RollingBuffer`, and the sector/macro risk layers; extended
   from a 7-symbol crypto prototype to a 28-coin hybrid HL engine + 138-symbol equities
   engine; designed the Phase 3 maker-pivot and Phase 4.2 dynamic urgency routing for
   HIP-3 equity perps.

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
