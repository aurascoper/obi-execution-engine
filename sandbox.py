"""
sandbox.py — Interactive shadow-mode signal sandbox.

Drives the real SignalEngine class with synthetic price + OBI data so you
can find the exact parameter values that trigger (or suppress) a $5 shadow
order before the engine runs overnight.

Launch:
    cd ~/Developer/live_trading
    streamlit run sandbox.py
"""

import sys
import types
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

# ── Path bootstrap — allow imports without installing the package ──────────────
sys.path.insert(0, ".")

# Provide a minimal config.risk_params so signals.py can import it
# (values here are display-only; MAX_ORDER_NOTIONAL is the live $5 cap)
_risk = types.ModuleType("config.risk_params")
_risk.MAX_ORDER_NOTIONAL = 5.00
_risk.SYMBOL_CAPS = {"ETH/USD": 3_000.0, "BTC/USD": 5_000.0}
_cfg_pkg = types.ModuleType("config")
sys.modules.setdefault("config", _cfg_pkg)
sys.modules.setdefault("config.risk_params", _risk)

from strategy.signals import SignalEngine, WINDOW  # noqa: E402

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Shadow Signal Sandbox",
    page_icon="🕵️",
    layout="wide",
)

st.title("🕵️ Shadow Signal Sandbox")
st.caption(
    "Drives the live `SignalEngine` with synthetic ETH/USD data. "
    "Adjust the sliders to find the exact moment the dual-gate fires a \\$5 shadow order."
)

# ── Sidebar controls ──────────────────────────────────────────────────────────
with st.sidebar:
    st.header("Price Series")
    base_price = st.slider("Base price (μ)", 500, 5000, 2100, step=50)
    noise_sigma = st.slider("Noise σ (price volatility)", 1, 100, 15, step=1)
    crash_sigma = st.slider("Crash depth (× σ below μ)", 0.0, 6.0, 3.0, step=0.1)
    rng_seed = st.slider("Random seed", 0, 99, 42, step=1)

    st.divider()
    st.header("Signal Thresholds")
    z_entry = st.slider("Z_ENTRY (enter when z <)", -4.0, -0.5, -1.25, step=0.05)
    z_exit = st.slider("Z_EXIT  (exit when z >)", -2.0, 0.5, -0.50, step=0.05)
    obi_theta = st.slider("OBI_THETA (min buy pressure)", 0.0, 0.9, 0.10, step=0.01)
    obi_levels = st.slider("OBI depth levels (N)", 1, 10, 5, step=1)

    st.divider()
    st.header("Order Book")
    bid_qty = st.slider("Bid depth (top N total)", 1, 1000, 300, step=10)
    ask_qty = st.slider("Ask depth (top N total)", 1, 1000, 100, step=10)

    st.divider()
    st.header("Account")
    notional_per_trade = st.slider("Notional per trade ($)", 1.0, 5.0, 5.0, step=0.5)

# ── Simulate ──────────────────────────────────────────────────────────────────
rng = np.random.default_rng(rng_seed)

# Build price series: WINDOW warmup bars at base_price±noise, then a crash
n_warmup = WINDOW
warmup_prices = base_price + rng.normal(0, noise_sigma, n_warmup)
crash_price = base_price - crash_sigma * noise_sigma
recovery_prices = base_price + rng.normal(0, noise_sigma, 40)

all_prices = np.concatenate([warmup_prices, [crash_price], recovery_prices])
n_total = len(all_prices)

# Run SignalEngine tick-by-tick
eng = SignalEngine(
    symbols=["ETH/USD"],
    window=WINDOW,
    z_entry=z_entry,
    z_exit=z_exit,
    obi_theta=obi_theta,
    obi_levels=obi_levels,
    notional_per_trade=notional_per_trade,
)

# Inject OBI (same value held constant throughout — realistic for sandbox)
rho = (bid_qty - ask_qty) / (bid_qty + ask_qty + 1e-8)
eng.update_orderbook(
    {
        "type": "orderbook",
        "symbol": "ETH/USD",
        "bids": [[base_price - 1, bid_qty]],
        "asks": [[base_price + 1, ask_qty]],
    }
)

zscores = []
signals_fired = []
entries = []
exits = []

for i, px in enumerate(all_prices):
    bar = {
        "type": "bar",
        "symbol": "ETH/USD",
        "close": float(px),
        "open": float(px),
        "high": float(px),
        "low": float(px),
        "volume": 100,
        "timestamp": f"t{i}",
        "recv_ns": 0,
    }
    sig = eng.evaluate(bar)

    buf = eng._state["ETH/USD"].price_buf
    z = buf.zscore(float(px))
    zscores.append(z)

    if sig is not None:
        signals_fired.append(i)
        entries.append((i, px, sig))

    # Detect exit (in_position flipped back to False after being True)
    if i > 0 and not eng._state["ETH/USD"].in_position and len(signals_fired) > 0:
        if i not in signals_fired:
            exits.append(i)

# ── Metrics row ───────────────────────────────────────────────────────────────
col1, col2, col3, col4 = st.columns(4)

obi_color = "normal" if rho > obi_theta else "inverse"
col1.metric(
    "OBI  ρ", f"{rho:.3f}", delta=f"threshold {obi_theta:.2f}", delta_color=obi_color
)

crash_z = (
    zscores[n_warmup]
    if n_warmup < len(zscores) and zscores[n_warmup] is not None
    else None
)
col2.metric(
    "Crash bar z-score",
    f"{crash_z:.3f}" if crash_z else "N/A",
    delta=f"entry gate {z_entry}",
    delta_color="normal" if (crash_z is not None and crash_z < z_entry) else "inverse",
)

col3.metric(
    "Shadow orders fired",
    len(signals_fired),
    delta="✓ triggered" if signals_fired else "✗ no trigger",
)

if signals_fired and entries:
    _, entry_px, entry_sig = entries[0]
    col4.metric(
        "Shadow order size",
        f"${entry_sig['notional']:.2f}",
        delta=f"{entry_sig['qty']} ETH @ ${entry_sig['limit_px']:.2f}",
    )
else:
    col4.metric("Shadow order size", "—", delta="no fill")

# ── Dual-gate status panel ────────────────────────────────────────────────────
gate1 = crash_z is not None and crash_z < z_entry
gate2 = rho > obi_theta

g1_icon = "🟢" if gate1 else "🔴"
g2_icon = "🟢" if gate2 else "🔴"
both = (
    "🟢 **BOTH GATES OPEN — order fires**"
    if (gate1 and gate2)
    else "🔴 **Gates not both open — no order**"
)

st.markdown(
    f"""
    | Gate | Condition | Value | Threshold | Status |
    |------|-----------|-------|-----------|--------|
    | 1 — Mean Reversion | z < Z_ENTRY | `{crash_z:.3f}` | `{z_entry}` | {g1_icon} |
    | 2 — OBI Confirmation | ρ > OBI_THETA | `{rho:.3f}` | `{obi_theta}` | {g2_icon} |

    {both}
    """
)

# ── Charts ────────────────────────────────────────────────────────────────────
fig = make_subplots(
    rows=2,
    cols=1,
    shared_xaxes=True,
    row_heights=[0.6, 0.4],
    subplot_titles=["ETH/USD Price", "Z-Score"],
    vertical_spacing=0.08,
)

xs = list(range(n_total))

# Price trace
fig.add_trace(
    go.Scatter(
        x=xs,
        y=all_prices.tolist(),
        name="Close",
        line=dict(color="#4C9BE8", width=1.5),
    ),
    row=1,
    col=1,
)

# Entry markers
for idx, px, _ in entries:
    fig.add_trace(
        go.Scatter(
            x=[idx],
            y=[px],
            mode="markers",
            marker=dict(symbol="triangle-up", size=14, color="#00D46A"),
            name="Entry",
            showlegend=(idx == entries[0][0]),
        ),
        row=1,
        col=1,
    )

# Warmup / live boundary
fig.add_vline(
    x=n_warmup - 1,
    line_dash="dash",
    line_color="gray",
    annotation_text="warmup ends",
    row=1,
    col=1,
)

# Z-score trace
z_vals = [z if z is not None else 0.0 for z in zscores]
fig.add_trace(
    go.Scatter(
        x=xs,
        y=z_vals,
        name="z-score",
        line=dict(color="#F5A623", width=1.5),
    ),
    row=2,
    col=1,
)

# Threshold lines on z-score panel
fig.add_hline(
    y=z_entry,
    line_dash="dot",
    line_color="#FF4444",
    annotation_text=f"Z_ENTRY ({z_entry})",
    row=2,
    col=1,
)
fig.add_hline(
    y=z_exit,
    line_dash="dot",
    line_color="#00D46A",
    annotation_text=f"Z_EXIT ({z_exit})",
    row=2,
    col=1,
)
fig.add_hline(y=0, line_dash="solid", line_color="rgba(255,255,255,0.15)", row=2, col=1)

fig.update_layout(
    height=520,
    template="plotly_dark",
    margin=dict(l=40, r=20, t=40, b=20),
    legend=dict(orientation="h", y=1.02),
    hovermode="x unified",
)
fig.update_xaxes(title_text="Bar index", row=2, col=1)
fig.update_yaxes(title_text="Price (USD)", row=1, col=1)
fig.update_yaxes(title_text="z", row=2, col=1)

st.plotly_chart(fig, use_container_width=True)

# ── Shadow order detail ───────────────────────────────────────────────────────
if entries:
    st.subheader("Shadow Order Log")
    for i, (bar_idx, px, sig) in enumerate(entries):
        st.code(
            f"[SHADOW EXECUTION] Mock fill\n"
            f"  symbol    = {sig['symbol']}\n"
            f"  side      = {sig['side'].value}\n"
            f"  qty       = {sig['qty']} ETH\n"
            f"  limit_px  = ${sig['limit_px']:.2f}\n"
            f"  notional  = ${sig['notional']:.2f}\n"
            f"  bar_index = {bar_idx}  (crash bar = {n_warmup})\n"
            f"  z_score   = {zscores[bar_idx]:.4f}\n"
            f"  obi_rho   = {rho:.4f}",
            language="text",
        )
else:
    st.info(
        "No shadow order triggered with current parameters. "
        "Try increasing the **Crash depth** slider or "
        "lowering **Z_ENTRY** / **OBI_THETA**.",
        icon="ℹ️",
    )

# ── Parameter summary (copy-paste ready for .zshenv) ─────────────────────────
with st.expander("Export parameters → shell env"):
    st.code(
        f"export EXECUTION_MODE=SHADOW\n"
        f"export ALPACA_TRADING_MODE=paper\n"
        f"# Signal thresholds (edit strategy/signals.py to apply)\n"
        f"# Z_ENTRY    = {z_entry}\n"
        f"# Z_EXIT     = {z_exit}\n"
        f"# OBI_THETA  = {obi_theta}\n"
        f"# OBI_LEVELS = {obi_levels}\n"
        f"# NOTIONAL   = {notional_per_trade}",
        language="bash",
    )
