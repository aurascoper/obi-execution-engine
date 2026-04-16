# Live Trading Engine — Claude Code Context

## What this is
Dual-engine mean-reversion trading system (crypto + equities) via Alpaca API.
Currently in paper trading (Phase 2). Phase 3 roadmap: passive maker algorithm with cancel-replace loops.

## Change discipline

- **Never touch risk-path code without explicit instruction.** Risk-path = drawdown circuit breaker, per-symbol notional caps, sector exposure limits, macro kill-switch (±15 min around CPI/FOMC/NFP). State what you're about to change and why before editing these.
- **Surgical changes only.** Do not restructure the signal pipeline, rename strategy parameters, or refactor engine classes when asked to fix a specific bug.
- **State assumptions before editing position sizing or order logic.** Wrong assumptions here mean wrong trades. If anything is unclear, ask.
- **Paper trading ≠ safe to be sloppy.** The paper engine is the staging environment for live. Bugs introduced here go live.
- **Do not add logging, metrics, or instrumentation** unless explicitly asked — JSON log format is structured and downstream tooling depends on it.

## Sensitive paths
- `live_engine.py` / `equities_engine.py` — signal pipeline and order execution
- `risk/` — all drawdown, exposure, and kill-switch logic
- `config/` — strategy parameters; changes here affect live behavior on restart
- `maker_engine.py` — Phase 3 passive maker (in development); treat as unstable

## Developer Environment

- **Required Env Vars:** `HL_WALLET_ADDRESS`, `HL_PRIVATE_KEY`
- **Management:** These are stored in `.env` (gitignored). Before running execution tests, ensure these are loaded in the shell.
