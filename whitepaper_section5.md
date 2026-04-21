# Backtesting Methodology and Pitfalls

Robust backtests must avoid biases that inflate apparent performance. This section catalogs the specific pitfalls relevant to a multi-venue mean-reversion system operating across native cryptocurrency perpetuals and builder-deployed equity/commodity perpetuals (HIP-3), with dual maker/taker execution paths and dynamic volatility-calibrated thresholds. We ground each pitfall in the system's actual architecture and propose concrete mitigations.


## 5.1 Look-Ahead Bias

Look-ahead bias arises whenever information from time $t + k$ influences a trading decision at time $t$. Three vectors are specific to this system.

**Volatility calibration leak.** At engine startup, the RMSD-based z-tier calibration consumes 240 one-minute bars to compute per-coin volatility and assign entry/exit thresholds via the mapping:

$$\text{RMSD}_{\%} = \frac{\sigma(\{P_1, \dots, P_{240}\})}{\bar{P}} \times 100 \times \sqrt{240}$$

where the $\sqrt{240}$ factor normalizes one-minute variance to the four-hour timescale used by the screener's tier boundaries. In a backtest, the first four hours of z-score thresholds are calibrated on data that overlaps the trading window. A correct implementation must use an **expanding calibration window** anchored to data strictly before each decision point, or warm up on a non-overlapping prior session entirely.

**Universe selection leak.** The screener ranks candidate instruments by a composite score of RMSD, open interest, daily volume, and category diversification. Running the screener on today's data and backtesting that universe historically is in-sample selection masquerading as out-of-sample testing. At each backtest date $d$, the universe must be reconstructed using only data available as of $d - 1$:

```
For each backtest date d:
    Fetch OI, volume, RMSD from candles up to d-1
    Apply screener filter: OI >= $1M, volume >= $500K
    Compute composite scores, select top-N
    Universe_d = selected coins
    Run strategy on date d using Universe_d only
```

**Threshold override leak.** Per-symbol z-tier overrides (e.g., widening exit to $+5\sigma$ based on observed volatility characteristics) embed forward knowledge. In a backtest, any override must be derived mechanically from data available at each timestamp --- for instance, by conditioning the exit threshold on trailing realized volatility relative to the asset class median, rather than setting it from post-hoc observation.


## 5.2 Survivorship Bias

Survivorship bias inflates backtest returns by excluding instruments that were delisted, halted, or became illiquid during the test period. Standard mitigations include using survivorship-free datasets such as CRSP for equities [14]. For perpetual futures on decentralized venues, the problem takes a distinct form.

**Builder DEX instrument lifecycle.** HIP-3 equity perpetuals on TradeXYZ have existed only since early 2026. Instruments may be listed and delisted as the builder adjusts its offering. The engine's universe is static for each session (set via environment variables at boot) and does not track historical additions or removals. A backtest using today's 20-coin HIP-3 universe retroactively ignores coins that were available three months ago but have since been removed.

**Liquidity regime shifts.** The screener filters on OI $\geq$ \$1M and 24-hour volume $\geq$ \$500K. A coin that met these thresholds when listed may fail them months later, and vice versa. Backtesting with a fixed filter produces a portfolio of current survivors. The mitigation is to store **point-in-time liquidity snapshots** --- daily OI and volume for every instrument that has ever been listed --- and apply the filter independently at each backtest date.

**Short history constraint.** With only months of HIP-3 data available, any in-sample optimization is fragile. Walk-forward validation with short calibration windows (5--10 trading days) and single-day out-of-sample steps is the only viable approach. Large-window parameter optimization will overfit to the small sample.

**Practical recommendation:** Maintain a timestamped ledger of universe membership --- coins added, removed, and the date of each change. Without this ledger, survivorship-free backtesting on builder DEX instruments is impossible, and any reported backtest results on HIP-3 should carry an explicit survivorship caveat.


## 5.3 Transaction Cost Model

Transaction costs are the primary determinant of whether a mean-reversion strategy is viable at the target notional scale. Avellaneda \& Lee (2010) assume approximately 10 basis points round-trip for equities [3, 12]. For perpetual futures on Hyperliquid, the cost structure is more complex and must be modeled at the component level.

### 5.3.1 Direct Costs

| Component | Taker (IOC) | Maker (ALO) |
|-----------|-------------|-------------|
| Exchange fee | $-3.5$ bps per side | $+0.5$ bps per side (rebate) |
| Round-trip | $-7.0$ bps | $+1.0$ bps |

Live taker burn-in during Phase 4.2 revealed that exchange fees alone consumed approximately **210\% of gross P\&L** at the \$100-per-coin HIP-3 notional scale --- i.e., gross alpha was positive but net P\&L was negative after fees. This single empirical observation underscores that any backtest omitting transaction costs is meaningless for this strategy class.

### 5.3.2 Slippage and Market Impact

For taker (IOC) orders, slippage is the difference between the signal price and the realized fill price. The system logs both `sent_px` (the price at signal generation, after precision rounding) and `fill_px` (the exchange's reported average fill price), enabling empirical calibration:

$$\text{slip}_{\text{bps}} = \frac{|P_{\text{fill}} - P_{\text{sent}}|}{P_{\text{sent}}} \times 10{,}000$$

A backtest should sample slippage from the empirical distribution fitted to production fill logs rather than assuming a fixed constant. Slippage is not stationary: it varies with time of day, book depth, and volatility regime. For HIP-3 equity perps, books are thinner than native crypto markets, and slippage distributions should be estimated separately by asset class.

For larger order sizes, Almgren \& Chriss (2000) model total execution cost as $C(x) = \eta x + \gamma x^2$, where $\eta$ captures linear (spread) costs and $\gamma$ captures permanent price impact [15]. At the current notional scale (\$100--\$250 per coin), the quadratic term is negligible, but it becomes relevant if position sizing increases.

### 5.3.3 Funding Rates

Perpetual futures accrue funding payments every eight hours on Hyperliquid, transferring value between longs and shorts to anchor the perpetual price to the spot index. The funding rate $r_f$ is applied to position notional:

$$\text{Funding PnL} = -\text{sgn}(\text{position}) \times |Q| \times P_{\text{mark}} \times r_f$$

where $Q$ is the signed position size and $P_{\text{mark}}$ is the mark price at the funding epoch. A long position pays funding when $r_f > 0$ (contango) and receives it when $r_f < 0$ (backwardation).

**This cost is currently unmodeled in the live P\&L attribution pipeline.** Even at a modest 0.01\% per eight-hour epoch, the annualized drag is:

$$0.0001 \times 3 \times 365 = 10.95\%$$

For a mean-reversion strategy with multi-hour to multi-day holding periods on 24/7 perpetuals, cumulative funding is material. A backtest must deduct funding at each eight-hour epoch for every open position. Historical funding rates are available via the Hyperliquid `fundingHistory` API endpoint and should be fetched and stored alongside price data.

### 5.3.4 Full Cost Pseudocode

```
For each simulated trade (entry or exit):
    cost  = notional * fee_bps / 10_000          # 3.5 bps taker or -0.5 bps maker
    cost += notional * sample(slippage_dist)      # empirical per-class distribution
    pnl  -= cost

For each 8-hour funding epoch while position is open:
    funding_rate = historical_funding[coin][epoch]
    pnl -= sign(position) * abs(notional) * funding_rate
```


## 5.4 Fill Simulation

The most dangerous source of backtest inflation is unrealistic fill assumptions. The system operates two execution paths with fundamentally different fill semantics.

### 5.4.1 Taker Path (IOC)

Immediate-or-cancel orders either fill immediately against resting liquidity or are rejected. Simulation is straightforward: if the L2 book at time $t$ has sufficient depth at the signal price, the order fills. The fill price is the volume-weighted average across consumed book levels:

$$P_{\text{fill}} = \frac{\sum_{i=1}^{k} P_i \cdot Q_i}{\sum_{i=1}^{k} Q_i}$$

where $P_i, Q_i$ are the price and size at each consumed level. This requires historical L2 snapshots, not just OHLC bars. If only bar data is available, apply a fixed spread assumption calibrated from live L2 observations (e.g., median half-spread per coin).

### 5.4.2 Maker Path (ALO) --- The Adverse Selection Problem

Post-only (Add Liquidity Only) orders rest on the book until filled or cancelled. The naive backtest assumption --- "if price touched my limit, I filled" --- is severely optimistic for mean-reversion strategies.

**Queue priority.** A maker order posted at the best bid joins a queue. Fill priority is price-time: orders at the same price fill in submission order. The backtest has no information about queue depth or position. A conservative approximation is to assume the order fills only if the **entire level is consumed** (i.e., price trades *through* the limit, not merely *at* it).

**Adverse selection.** Maker fills on mean-reversion entries are negatively selected. Consider a long entry at $z < -s_{\text{entry}}$: the order rests at the best bid. If price continues falling, the order fills --- but the trade is immediately underwater. If price bounces before the order fills, the favorable entry is missed. The fills that execute are disproportionately the losers. This effect is well-documented in market microstructure literature and is amplified in thin books (e.g., HIP-3 equity perps with limited depth).

**Cancel-replace dynamics.** The engine reprices resting maker orders on a timed schedule (30-second intervals with 5 reprices for native crypto; 120-second intervals with 15 reprices for HIP-3). Each reprice resets queue priority to the back of the new level. Simulating this requires tracking the evolution of the order's price level across reprices and re-evaluating fill probability at each step.

**Recommendation:** Run backtest results under two fill regimes as a sensitivity bound:

1. **Conservative (taker-equivalent):** Assume all entries fill via IOC at the ask (buys) or bid (sells), paying full spread and taker fees. This establishes a performance floor.
2. **Optimistic (maker with adverse selection discount):** Assume maker fills occur when price trades through the limit level by at least one tick, apply maker rebate, but discount fill probability by an empirically calibrated factor $\phi \in [0.3, 0.7]$ representing the fraction of resting orders that actually execute before cancellation.

Report both regimes. If the strategy is profitable only under the optimistic maker assumption, it is not robust.


## 5.5 Synthetic Bar Construction

Native cryptocurrency perpetuals (BTC, ETH, SOL, etc.) have historical bar data available from multiple vendors at arbitrary resolutions. HIP-3 builder-deployed perpetuals do not. The engine synthesizes one-minute bars for HIP-3 instruments from Level-2 order book snapshots:

$$P_{\text{mid}} = \frac{P_{\text{best\_bid}} + P_{\text{best\_ask}}}{2}$$

These synthetic bars carry three artifacts that affect backtest fidelity.

**No volume field.** Synthesized bars record $\text{volume} = 0$ because L2 snapshots do not contain trade volume. Any volume-based feature (VWAP, volume-weighted z-scores, participation-rate sizing) is unavailable for HIP-3 instruments in backtest. The backtest must use price-only features for these coins.

**Midprice vs. trade price divergence.** During low-activity periods, the midprice can remain stable while actual trades execute at spread edges. Z-scores computed from midprice bars will understate true volatility and produce tighter entry thresholds than the market justifies. The RMSD floor (5\% for equities, 2.5\% for commodities, 3\% for ETFs, 1.5\% for indices) partially mitigates this but introduces a separate bias: it clips the lower tail of the volatility distribution.

**Session structure leakage.** HIP-3 equity perpetuals trade 24/7 but track US equity underlyings. Outside regular trading hours (RTH), quote updates slow or cease. Bars during these periods are flat or near-flat, depressing rolling standard deviation and artificially inflating z-scores at the RTH open. A backtest should either (a) restrict signals to RTH-adjacent windows, or (b) compute rolling statistics using only RTH-session bars to avoid contamination from overnight flats.


## 5.6 Validation Framework

### 5.6.1 Walk-Forward Protocol

Fixed-window in-sample optimization overfits on short histories. The engine's boot-time RMSD recalibration is itself a form of adaptive regime detection that must be replicated in backtest:

```
For each walk-forward step k:
    Calibration window:  [d_{k} - W, d_{k})     # W = 5-10 trading days
    Trading window:      [d_{k}, d_{k} + 1)     # single-day out-of-sample

    1. Fetch 240 x 1-min bars ending at d_k
    2. Compute RMSD, assign z-tiers per coin
    3. Apply category floors (EQUITY >= 5%, COMMODITY >= 2.5%, ...)
    4. Simulate strategy on trading window with these thresholds
    5. Record out-of-sample PnL after full cost model

Aggregate: concatenate all out-of-sample windows for performance metrics
```

### 5.6.2 Regime-Conditional Splits

Calendar-based train/test splits (e.g., first 80\% / last 20\%) conflate regime changes with time. Partition by market regime instead:

- **Volatility regime:** High ($\text{ATM IV} > 50$) vs. low ($\text{ATM IV} < 30$) for crypto; VIX-equivalent thresholds for equity perps.
- **Session regime:** US RTH (09:30--16:00 ET) vs. overnight, for HIP-3 equity perps that follow equity session dynamics despite 24/7 availability.
- **Trend regime:** Trending ($|\text{SMA}_{240} \text{ slope}| > \theta$) vs. mean-reverting. The engine's trend gate already blocks entries opposing the 240-bar SMA; the backtest should measure performance separately in trending vs. ranging regimes to validate this gate's value.
- **Macro regime:** Pre/post FOMC, CPI, NFP windows, given the engine's macro kill-switch halts trading $\pm15$ minutes around these events.

### 5.6.3 Metrics

Beyond Sharpe and Sortino ratios (Section 7), report:

- **Profit factor after costs:** $\text{PF} = \sum \text{winning trades (net)} \;/\; |\sum \text{losing trades (net)}|$, with all components from Section 5.3 applied. A PF $< 1.0$ after costs means the strategy destroys capital regardless of hit rate.
- **Hit rate by z-tier:** Stratify win/loss rates by the RMSD-assigned tier (tight vs. wide thresholds). If the tightest tier (RMSD $< 3\%$) underperforms, the tier assignment is not adding value.
- **Time-in-trade distribution:** Mean-reversion assumes convergence within a bounded horizon. A fat right tail (positions held $> 24$h) indicates regime breaks where the mean has shifted. Report median, 90th, and 99th percentile holding times.
- **Funding drag attribution:** Decompose net P\&L into $\text{PnL}_{\text{gross}} - \text{fees} - \text{slippage} - \text{funding}$ to isolate whether alpha survives each cost layer independently.
- **Maximum adverse excursion (MAE):** For each trade, record the worst unrealized drawdown before the trade closed. MAE distributions reveal whether stop-loss levels (or their absence) are appropriate [16].


## 5.7 Irreducible Limitations

Certain system behaviors are inherently discretionary, path-dependent, or non-reproducible in historical simulation. These should be disclosed as limitations of any backtest result.

**Builder DEX liquidity.** HIP-3 order books are thin relative to native crypto markets. Backtested position sizes that appear fillable at historical midprices may not have been executable at the time. Without historical L2 snapshots (which are not currently stored), fill feasibility for maker orders is unverifiable.

**Circuit breaker path dependency.** The watchdog terminates the engine when portfolio drawdown exceeds a fixed threshold (\$50 in production). This truncates the left tail of the P\&L distribution. A backtest that includes the circuit breaker produces artificially bounded losses; one that excludes it overstates tail risk. Report results both ways and note the discrepancy.

**Operator interventions.** Per-symbol z-tier overrides (e.g., widening the exit threshold for a specific instrument based on observed volatility characteristics) are discretionary decisions made during live operation. These cannot be systematically replicated in backtest. Any backtest that includes such overrides must flag them as non-mechanical alpha and report results with and without them.

**Regime breaks vs. mean reversion.** The z-score framework assumes prices oscillate around a stable mean. Structural events --- regulatory action, M\&A, technological disruption --- shift the mean permanently. The trend gate (240-bar SMA filter) partially screens for this, but historical backtests will include periods where the gate was not yet triggered while the regime had already changed. This is a fundamental limitation of any mean-reversion backtest, not a modeling error.


## 5.8 Comparative Summary

| Bias / Pitfall | Risk to This System | Mitigation |
|---|---|---|
| Look-ahead (RMSD calibration) | High --- calibration window overlaps trading window | Expanding window; non-overlapping warm-up |
| Look-ahead (universe selection) | High --- screener uses current liquidity data | Point-in-time universe reconstruction |
| Survivorship (HIP-3 delistings) | High --- no historical membership ledger exists | Maintain timestamped universe ledger |
| Transaction costs (taker fees) | Critical --- 210\% of gross P\&L in burn-in | Model at 3.5 bps/side minimum |
| Transaction costs (funding) | Material --- 10\%+ annualized unmodeled drag | Deduct at each 8h epoch from historical rates |
| Maker fill inflation | High --- adverse selection in thin books | Dual-regime reporting (taker floor / maker ceiling) |
| Synthetic bar artifacts | Moderate --- no volume, midprice divergence | RTH-only rolling stats; RMSD floors |
| Short sample (HIP-3) | High --- months, not years, of history | Walk-forward with small windows; survivorship caveat |
| Circuit breaker truncation | Moderate --- bounds left tail artificially | Report with and without breaker |
| Operator overrides | Low--Moderate --- discretionary, non-reproducible | Flag as non-mechanical; report both ways |
