#!/usr/bin/env python3
"""
hl_watchdog.py — Standalone circuit breaker for Hyperliquid perps.

Polls HL accountValue every 60s. If daily drawdown exceeds the configured
threshold, sends SIGTERM to the HL engine process and exits.

Usage:
    venv/bin/python3 hl_watchdog.py &

Reads HL_WALLET_ADDRESS from env (or .env via dotenv). No engine imports.
"""
from __future__ import annotations

import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Load .env if present (for HL_WALLET_ADDRESS)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

POLL_INTERVAL_S      = 60
MAX_DAILY_LOSS       = 50.0
LOG_PATH             = Path(__file__).resolve().parent / "logs" / "hl_watchdog.log"
ENGINE_SCRIPT        = "hl_engine.py"

def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{ts}  {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass

def _find_engine_pid() -> int | None:
    """Find the PID of a running hl_engine.py process."""
    import subprocess
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", ENGINE_SCRIPT], text=True, stderr=subprocess.DEVNULL
        )
        for line in out.strip().splitlines():
            pid = int(line.strip())
            if pid != os.getpid():
                return pid
    except (subprocess.CalledProcessError, ValueError):
        pass
    return None

def _get_account_value(info, addr: str) -> float:
    s = info.user_state(addr)
    balances = info.post("/info", {"type": "spotClearinghouseState", "user": addr}).get("balances", [])
    for b in balances:
        if b["coin"] == "USDC":
            return float(b["total"])
    return float(s["marginSummary"]["accountValue"])

def main() -> int:
    addr = os.environ.get("HL_WALLET_ADDRESS")
    if not addr:
        _log("FATAL: HL_WALLET_ADDRESS not set")
        return 1

    try:
        from hyperliquid.info import Info
    except ImportError:
        _log("FATAL: hyperliquid SDK not installed")
        return 1

    info = Info(base_url="https://api.hyperliquid.xyz", skip_ws=True)

    baseline = _get_account_value(info, addr)
    _log(f"watchdog started  baseline=${baseline:.2f}  threshold=${MAX_DAILY_LOSS:.2f}")

    while True:
        time.sleep(POLL_INTERVAL_S)
        try:
            current = _get_account_value(info, addr)
        except Exception as exc:
            _log(f"poll error: {type(exc).__name__}: {exc}")
            continue

        delta = current - baseline
        _log(f"poll  current=${current:.2f}  delta=${delta:+.2f}")

        if delta <= -MAX_DAILY_LOSS:
            _log(f"CIRCUIT BREAKER TRIPPED  delta=${delta:.2f}  threshold=-${MAX_DAILY_LOSS}")
            pid = _find_engine_pid()
            if pid:
                _log(f"sending SIGTERM to PID {pid}")
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    _log(f"PID {pid} already gone")
            else:
                _log("engine PID not found — may already be stopped")
            return 2

    return 0

if __name__ == "__main__":
    sys.exit(main())
