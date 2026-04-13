"""
config/settings.py — Environment-driven configuration.

Execution modes (set via EXECUTION_MODE env var):
  SHADOW  — Strategy runs fully; orders are logged but never submitted.
             Use this to verify signal quality and latency on live data feeds
             before committing real capital.
  PAPER   — Orders submitted to Alpaca paper trading endpoint. Real fills,
             fake money. (default)
  LIVE    — Orders submitted to Alpaca live endpoint. Real capital.
             Requires ALPACA_TRADING_MODE=live and live API keys.
"""

import os
from dataclasses import dataclass
from enum import Enum


class ExecutionMode(str, Enum):
    SHADOW = "SHADOW"
    PAPER  = "PAPER"
    LIVE   = "LIVE"

    @classmethod
    def from_env(cls) -> "ExecutionMode":
        raw = os.environ.get("EXECUTION_MODE", "PAPER").upper().strip()
        try:
            return cls(raw)
        except ValueError:
            valid = [m.value for m in cls]
            raise ValueError(
                f"Invalid EXECUTION_MODE={raw!r}. Must be one of: {valid}"
            )


@dataclass(frozen=True)
class Settings:
    api_key:        str
    api_secret:     str
    base_url:       str
    data_url:       str
    stream_url:     str
    paper:          bool           # True → paper endpoint; False → live endpoint
    execution_mode: ExecutionMode  # SHADOW | PAPER | LIVE
    # Crypto data feed always uses paper credentials — same data, avoids burning
    # the live key's single free-tier WebSocket connection slot.
    data_key:       str = ""
    data_secret:    str = ""
    log_dir:        str = "logs"


def load() -> Settings:
    exec_mode = ExecutionMode.from_env()

    # Determine brokerage endpoint from ALPACA_TRADING_MODE
    trading_mode = os.environ.get("ALPACA_TRADING_MODE", "paper").lower()
    if trading_mode == "live":
        key    = os.environ["ALPACA_API_KEY_LIVE"]
        secret = os.environ["ALPACA_API_SECRET_LIVE"]
        paper  = False
    else:
        key    = os.environ["ALPACA_API_KEY_ID"]
        secret = os.environ["ALPACA_API_SECRET_KEY"]
        paper  = True

    # Guard: SHADOW mode should never reach the live endpoint
    if exec_mode == ExecutionMode.LIVE and paper:
        raise RuntimeError(
            "EXECUTION_MODE=LIVE requires ALPACA_TRADING_MODE=live. "
            "Set both explicitly to prevent accidental live trading."
        )

    # Data feed always uses paper credentials — crypto market data is identical
    # for paper and live, and this preserves the live key's connection slot.
    data_key    = os.environ.get("ALPACA_API_KEY_ID",       key)
    data_secret = os.environ.get("ALPACA_API_SECRET_KEY", secret)

    return Settings(
        api_key        = key,
        api_secret     = secret,
        base_url       = os.environ.get(
            "ALPACA_BASE_URL",
            "https://api.alpaca.markets" if trading_mode == "live"
            else "https://paper-api.alpaca.markets",
        ),
        data_url       = os.environ.get(
            "ALPACA_DATA_URL", "wss://stream.data.alpaca.markets/v2"
        ),
        stream_url     = os.environ.get(
            "ALPACA_STREAM_URL", "wss://stream.alpaca.markets/v2"
        ),
        paper          = paper,
        execution_mode = exec_mode,
        data_key       = data_key,
        data_secret    = data_secret,
    )
