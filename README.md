# Alpaca L2 Execution Engine
### Hybrid OBI + Mean-Reversion | Long-Only Crypto | Async Python | Apple Silicon M4

A production-grade, hardened live trading engine that synthesizes two quantitative strategies
from recent academic literature into a single dual-gate signal: **Order-Book Imbalance** as a
microstructure precision trigger gating a **Mean-Reversion z-score** entry. Runs natively on
Apple Silicon via an asyncio event loop with WebSocket L2 data, fractional crypto order
execution through Alpaca, and independent circuit breakers that operate with no dependency on
strategy logic.

---

## Mathematical Basis

### 1. Mean Reversion — Ornstein–Uhlenbeck z-score

Prices of individual assets are modeled as Ornstein–Uhlenbeck (OU) processes
(Avellaneda & Lee, 2010):

```
dX_t = κ(μ − X_t) dt + σ dW_t
```

where `X_t` reverts to mean `μ` at speed `κ`. The normalized deviation (z-score) over a
rolling window `W` is:

```
z_t = (X_t − μ_W) / σ_W
```

where `μ_W` and `σ_W` are the rolling mean and sample standard deviation (ddof=1) over `W`
bars. Per Avellaneda & Lee's empirical calibration across equity residuals, the thresholds are:

| Parameter    | Value  | Interpretation                          |
|--------------|--------|-----------------------------------------|
| `W` (window) | 60 bars | Rolling lookback (60-min bars ≈ 1 day) |
| `Z_ENTRY`    | −1.25σ | Enter long when price is oversold       |
| `Z_EXIT`     | −0.50σ | Exit when mean-reversion is sufficient  |

The buffer is implemented as an O(1) circular array (`_RollingBuffer`) backed by a contiguous
`float64` numpy array — 60 × 8 bytes = 480 bytes per symbol, entirely in L1 cache on M4.

### 2. Order-Book Imbalance — Cartea et al.

The static volume imbalance at depth N is (Cartea et al., 2018):

```
ρ_t = (V^b_t − V^a_t) / (V^b_t + V^a_t + ε)
```

where `V^b` and `V^a` are the aggregated bid and ask volume across the top `N = 5` order-book
levels. `ε = 1e-8` guards against division by zero on empty books. `ρ_t ∈ (−1, 1)`:
positive values indicate net buy-side pressure.

Empirically, a positive imbalance predicts a higher probability of the next market order
being a buy and a short-term price uptick (Cartea et al., 2018). The threshold `OBI_THETA = 0`
(any net buy pressure) is used as the gate condition.

### 3. Dual-Gate Entry Logic

Both conditions must hold simultaneously on the same bar:

```
ENTRY:  z_t < Z_ENTRY  AND  ρ_t > OBI_THETA
EXIT:   z_t > Z_EXIT
```

The z-score confirms the asset is statistically oversold; OBI confirms buy-side liquidity
pressure is present at the microstructure level before capital is committed. This is a
long-only strategy — no short positions, consistent with the ~$150 paper-trading account
constraint and Pattern Day Trader avoidance.

---

## Architecture

```
live_trading/
├── live_engine.py          # Async entry point — TaskGroup(feed, strategy, drawdown)
├── config/
│   ├── settings.py         # Env-driven credentials (os.environ only — zero hardcoding)
│   └── risk_params.py      # Hardcoded circuit breaker constants
├── data/
│   └── feed.py             # Alpaca CryptoDataStream v1beta3 WebSocket (bars + L2 orderbooks)
├── strategy/
│   └── signals.py          # SignalEngine: _RollingBuffer, OBI, dual-gate logic
├── execution/
│   └── order_manager.py    # LimitOrderRequest + asyncio.to_thread + slippage logging
├── risk/
│   └── circuit_breaker.py  # Independent watchdog — no strategy imports
├── sandbox.py              # Streamlit shadow-mode signal explorer (no orders)
├── logs/
│   └── .gitkeep            # engine.jsonl written here at runtime (gitignored)
└── requirements.txt
```

### Signal Pipeline

```
Market Prices (1-min bars)
        │
        ▼
  _RollingBuffer.push(close)
        │
        ▼
  zscore = (close − μ_W) / σ_W    ◄── None if window < 60 bars (warmup)
        │
        ├── in_position = True?
        │       └── z > Z_EXIT → log exit_signal, set in_position = False
        │
        └── in_position = False?
                ├── z < Z_ENTRY?  (oversold gate)
                └── ρ_t > θ?      (OBI buy-pressure gate)
                        │
                        ▼
                    size_order()  → floor(notional / price, decimals)
                        │
                        ▼
                  LimitOrderRequest  → Alpaca paper/live endpoint
```

```
L2 Orderbook (per snapshot, ~1000/min)
        │
        ▼
  bid_sizes = top-5 bid depths
  ask_sizes = top-5 ask depths
  ρ_t = (Σbid − Σask) / (Σbid + Σask + ε)
        │
        └── cached in _SymbolState.obi — gating condition on next bar
```

---

## Risk Controls

All thresholds are hardcoded constants in `config/risk_params.py` — not runtime-configurable.
`CircuitBreaker` in `risk/circuit_breaker.py` has **zero imports from `strategy/`** by design.

| Control                  | Value           | Trigger                                   |
|--------------------------|-----------------|-------------------------------------------|
| Daily drawdown halt      | 2% equity       | Hard stop — engine exits, feed closes     |
| Absolute daily loss      | $500            | Hard stop (whichever hits first)          |
| Max order notional       | $15             | Per-order cap (Alpaca minimum is $10)     |
| Per-symbol caps          | $500–$5,000     | Per `SYMBOL_CAPS` dict in `risk_params`   |
| Max open positions       | 10              | Portfolio-level                           |
| Single position size     | ≤ 10% equity    | Portfolio-level                           |
| API rate limit           | 30 orders/min   | Token bucket (Alpaca allows 200/min)      |
| Slippage alert           | > 0.5% from ref | Logged as `warning` in `engine.jsonl`     |
| Rollback on failed order | on submit error | `signals.rollback_entry()` resets state   |

Drawdown is checked every 60 seconds by an independent `_drawdown_watch` coroutine running
concurrently with the strategy loop via `asyncio.TaskGroup`.

---

## Universe

7-symbol long-only crypto universe (24/7 Alpaca paper trading):

| Symbol     | Qty Decimals | Notional Cap |
|------------|-------------|--------------|
| `ETH/USD`  | 6           | $3,000       |
| `BTC/USD`  | 6           | $5,000       |
| `SOL/USD`  | 4           | $3,000       |
| `DOGE/USD` | 2           | $1,000       |
| `AVAX/USD` | 4           | $1,000       |
| `LINK/USD` | 4           | $1,000       |
| `SHIB/USD` | 0 (whole)   | $500         |

Qty is floored (never rounded) so actual notional never exceeds the cap at submission.

---

## Quickstart

### 1. Credentials

```bash
cp env.sh.example env.sh   # create from template — never commit env.sh
# Edit env.sh with your Alpaca paper keys
source env.sh
```

`env.sh` is in `.gitignore`. All keys are read via `os.environ` in `config/settings.py`.

### 2. Install

```bash
pip install -r requirements.txt
```

### 3. Run (paper mode — default)

```bash
# Paper trading (safe — fake money, real fills)
export EXECUTION_MODE=PAPER
export ALPACA_TRADING_MODE=paper
source env.sh && python live_engine.py
```

Logs stream to `logs/engine.jsonl` (newline-delimited JSON). Monitor signals:

```bash
tail -f logs/engine.jsonl | python3 -c \
  "import sys, json; [print(json.dumps(json.loads(l), indent=2))
   for l in sys.stdin if any(k in l for k in
   ['entry_signal','order_submitted','order_blocked','exit_signal'])]"
```

### 4. Shadow mode (zero orders, strategy visible)

```bash
export EXECUTION_MODE=SHADOW
python live_engine.py
```

### 5. Signal sandbox (Streamlit UI)

```bash
streamlit run sandbox.py
```

Interactive sliders for Z_ENTRY, Z_EXIT, OBI_THETA, crash depth. Drives the live
`SignalEngine` class with synthetic price data — no API keys needed.

### 6. Live trading

```bash
export EXECUTION_MODE=LIVE
export ALPACA_TRADING_MODE=live
# Requires ALPACA_API_KEY_LIVE and ALPACA_API_SECRET_LIVE in env.sh
source env.sh && python live_engine.py
```

The `config/settings.py` `load()` function enforces that `EXECUTION_MODE=LIVE` requires
`ALPACA_TRADING_MODE=live` — mismatched flags raise `RuntimeError` before any connection is
made.

---

## Execution Modes

| `EXECUTION_MODE` | `ALPACA_TRADING_MODE` | Orders submitted | Capital at risk |
|------------------|-----------------------|-----------------|-----------------|
| `SHADOW`         | `paper`               | No              | None            |
| `PAPER`          | `paper`               | Yes             | None (paper)    |
| `LIVE`           | `live`                | Yes             | Real capital    |

---

## Logging

All events are written as newline-delimited JSON to `logs/engine.jsonl`. Key event types:

| Event               | Level    | Meaning                                      |
|---------------------|----------|----------------------------------------------|
| `engine_start`      | info     | Mode, universe, baseline equity              |
| `baseline_equity`   | info     | Opening equity snapshot for drawdown calc    |
| `feed_subscribed`   | info     | WebSocket subscriptions confirmed            |
| `feed_msg_rate`     | info     | bars/orderbooks/quotes per minute (1-min)    |
| `signal_tick`       | debug    | Per-bar z-score and OBI values               |
| `entry_signal`      | info     | Both gates open — order being submitted      |
| `exit_signal`       | info     | Z reverted above exit threshold              |
| `order_submitted`   | info     | Alpaca order ID, latency_ms, qty, limit_px   |
| `order_blocked_*`   | warning  | Circuit breaker blocked the order            |
| `slippage`          | warning  | Fill > 0.5% from expected price              |
| `CIRCUIT_BREAKER_TRIPPED` | critical | Hard halt — reason logged          |

---

## Build Provenance

This engine was built via a three-model pipeline:

1. **Architecture blueprint** — Gemini 3.1 Pro generated the initial system directive
   (execution phases, directory structure, circuit breaker spec, M4 async requirements).

2. **Quantitative strategy research** — ChatGPT Deep Research synthesized the strategy thesis
   into a whitepaper pulling exact formulas and empirical thresholds from:
   - Avellaneda & Lee (2010), *Statistical Arbitrage in the U.S. Equities Market*
   - Cartea, Jaimungal & Penalva (2018), *Algorithmic and High-Frequency Trading*
   - Cont & De Larrard (2013) and hftbacktest OBI tutorials

3. **Implementation** — Claude Sonnet 4.6 (Claude Code) translated the whitepaper math into
   the `SignalEngine` class with numpy-vectorized operations, wired the Alpaca v1beta3 crypto
   WebSocket, fixed the `asyncio.to_thread` blocking-SDK issue, and hardened all circuit
   breakers for the $150 long-only crypto account constraint.

---

## References

- Avellaneda, M. & Lee, J.H. (2010). Statistical arbitrage in the U.S. equities market.
  *Quantitative Finance*, 10(7), 761–782.
- Cartea, Á., Jaimungal, S. & Penalva, J. (2015). *Algorithmic and High-Frequency Trading*.
  Cambridge University Press.
- QuestDB. Ornstein-Uhlenbeck Process for Mean Reversion.
- hftbacktest. Market Making with Alpha — Order Book Imbalance.
- Portfolio Optimization Book. The Seven Sins of Quantitative Investing.

---

## License

MIT
