"""
control/client.py — Synchronous Unix-socket client for the HL engine control plane.

Designed for CLI use (hl_ctl.py). No asyncio — plain blocking sockets.
"""

from __future__ import annotations

import json
import os
import socket

from util.platform_compat import control_socket_path, supports_unix_sockets

_DEFAULT_SOCK = control_socket_path("hl_engine")
_TIMEOUT = 5.0
_BUF_SIZE = 65536


class ControlClient:
    def __init__(self, sock_path: str = _DEFAULT_SOCK) -> None:
        self._sock_path = sock_path

    # ── Public API ────────────────────────────────────────────────────────────

    def get(self, coin: str) -> dict:
        """Fetch per-symbol detail for one coin."""
        return self._request({"cmd": "get", "params": {"coin": coin}})

    def get_all(self) -> dict:
        """Fetch z-thresholds for all coins."""
        return self._request({"cmd": "get_all"})

    def snapshot(self) -> dict:
        """Fetch full engine snapshot (thresholds, positions, prices, meta)."""
        return self._request({"cmd": "snapshot"})

    # ── Transport ─────────────────────────────────────────────────────────────

    def _request(self, msg: dict) -> dict:
        if not supports_unix_sockets():
            raise ConnectionError(
                "Control plane is unavailable on Windows (AF_UNIX not supported). "
                "Run hl_ctl from a POSIX host or use a WSL shell."
            )
        if not os.path.exists(self._sock_path):
            raise ConnectionError(
                f"Control socket not found at {self._sock_path}. "
                f"Is hl_engine.py running?"
            )

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(_TIMEOUT)
        try:
            sock.connect(self._sock_path)
            payload = json.dumps(msg, separators=(",", ":")).encode() + b"\n"
            sock.sendall(payload)

            # Read until newline (server sends newline-delimited JSON).
            buf = b""
            while b"\n" not in buf:
                chunk = sock.recv(_BUF_SIZE)
                if not chunk:
                    break
                buf += chunk

            if not buf:
                raise ConnectionError("Empty response from control plane")

            return json.loads(buf.strip())
        except socket.timeout:
            raise ConnectionError("Control plane did not respond within 5 s")
        finally:
            sock.close()
