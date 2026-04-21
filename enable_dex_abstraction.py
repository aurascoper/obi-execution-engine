#!/usr/bin/env python3
"""Call agent_enable_dex_abstraction via agent wallet (no master sig needed)."""

from config.settings import load as load_settings
from execution.hl_manager import HyperliquidOrderManager

cfg = load_settings()
mgr = HyperliquidOrderManager(
    cfg, strategy_tag="dex_abs", default_leverage=10, coins=["BTC"], is_cross=True
)
resp = mgr._exchange.agent_enable_dex_abstraction()
print(resp)
