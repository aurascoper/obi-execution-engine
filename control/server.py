"""
control/server.py — Unix-domain-socket control plane for HLEngine (Phase 1: read-only).

Runs inside the engine's asyncio TaskGroup. Never raises — all client errors
are caught and logged so the trading loop is never disrupted.
"""

from __future__ import annotations

import asyncio
import math
import os
import time

import structlog

from control.protocol import deserialize, serialize
from strategy.signals import SignalEngine

log = structlog.get_logger("hl_control")

# Max message size from a client (guard against abuse / garbage).
_MAX_MSG_BYTES = 4096


class ControlPlaneServer:
    def __init__(
        self,
        signals: SignalEngine,
        engine_meta: dict,
        sock_path: str = "/tmp/hl_engine.sock",
    ) -> None:
        self._signals = signals
        self._meta = engine_meta
        self._sock_path = sock_path
        self._server: asyncio.AbstractServer | None = None

    # ── Public lifecycle ──────────────────────────────────────────────────────

    async def serve(self) -> None:
        """Start the Unix server. Runs until stop() is called or the task is cancelled."""
        # Clean up stale socket from previous run / crash.
        try:
            os.unlink(self._sock_path)
        except FileNotFoundError:
            pass

        self._server = await asyncio.start_unix_server(
            self._handle_client, path=self._sock_path
        )
        log.info("ctl_server_start", sock=self._sock_path)

        try:
            async with self._server:
                await self._server.serve_forever()
        except asyncio.CancelledError:
            pass
        finally:
            self._cleanup_socket()

    def stop(self) -> None:
        """Signal the server to shut down (called from engine.stop())."""
        if self._server is not None:
            self._server.close()
        self._cleanup_socket()

    # ── Connection handler ────────────────────────────────────────────────────

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not raw or len(raw) > _MAX_MSG_BYTES:
                writer.close()
                await writer.wait_closed()
                return

            req = deserialize(raw)
            cmd = req.get("cmd", "")
            log.info("ctl_command", cmd=cmd, params=req.get("params"))

            resp = self._dispatch(cmd, req.get("params") or {})
            writer.write(serialize(resp))
            await writer.drain()
        except asyncio.TimeoutError:
            log.warning("ctl_client_timeout")
        except Exception:
            log.exception("ctl_handler_error")
            try:
                writer.write(serialize({"ok": False, "error": "internal_error"}))
                await writer.drain()
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    # ── Command dispatch ──────────────────────────────────────────────────────

    def _dispatch(self, cmd: str, params: dict) -> dict:
        if cmd == "get":
            return self._cmd_get(params)
        if cmd == "get_all":
            return self._cmd_get_all()
        if cmd == "snapshot":
            return self._cmd_snapshot()
        return {"ok": False, "error": f"unknown_command: {cmd}"}

    # ── Read commands ─────────────────────────────────────────────────────────

    def _cmd_get(self, params: dict) -> dict:
        coin = str(params.get("coin", "")).strip()
        # Try coin name ("BTC") or full symbol ("BTC/USD").
        if "/" in coin:
            symbol = coin
        else:
            symbol = f"{coin.upper()}/USD"
        st = self._signals._state.get(symbol)
        # Fallback: try DEX-prefixed lookup (e.g. "SP500" → "xyz:SP500/USD").
        if st is None:
            for key, state in self._signals._state.items():
                if ":" in key and key.endswith(f":{coin.upper()}/USD"):
                    st = state
                    symbol = key
                    break
        if st is None:
            return {"ok": False, "error": f"unknown_symbol: {coin}"}
        return {"ok": True, "data": self._symbol_detail(st)}

    def _cmd_get_all(self) -> dict:
        data: dict[str, dict] = {}
        for sym, st in self._signals._state.items():
            data[sym] = {
                "z_entry":       st.z_entry or self._signals._z_entry,
                "z_exit":        st.z_exit or self._signals._z_exit,
                "z_short_entry": st.z_short_entry or self._signals._z_short_entry,
                "z_exit_short":  st.z_exit_short or self._signals._z_exit_short,
            }
        return {"ok": True, "data": data}

    def _cmd_snapshot(self) -> dict:
        symbols: dict[str, dict] = {}
        for sym, st in self._signals._state.items():
            symbols[sym] = self._symbol_detail(st)
        return {
            "ok": True,
            "data": {
                "engine": self._meta,
                "symbols": symbols,
                "ts": time.time(),
            },
        }

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _symbol_detail(self, st) -> dict:
        """Full per-symbol state snapshot for get / snapshot commands."""
        return {
            "symbol":        st.symbol,
            "z_entry":       st.z_entry or self._signals._z_entry,
            "z_exit":        st.z_exit or self._signals._z_exit,
            "z_short_entry": st.z_short_entry or self._signals._z_short_entry,
            "z_exit_short":  st.z_exit_short or self._signals._z_exit_short,
            "obi":           _safe_float(st.obi),
            "best_bid":      _safe_float(st.best_bid),
            "best_ask":      _safe_float(st.best_ask),
            "positions":     dict(st.positions),
            "entry_prices":  {k: _safe_float(v) for k, v in st.entry_prices.items()},
        }

    def _cleanup_socket(self) -> None:
        try:
            os.unlink(self._sock_path)
        except FileNotFoundError:
            pass


def _safe_float(v: float) -> float | None:
    """Convert NaN/Inf to None for JSON serialisation."""
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return None
    return v
