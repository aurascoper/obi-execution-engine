"""Cross-platform compatibility helpers.

Windows asyncio event loops do not support ``loop.add_signal_handler`` or
``AF_UNIX`` sockets. This module centralizes the guarded fallbacks so the
engines stay identical between macOS/Linux and Windows.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import tempfile
from pathlib import Path
from typing import Callable


def install_shutdown_handlers(stop_fn: Callable[..., None]) -> None:
    """Register SIGINT/SIGTERM handlers that call ``stop_fn``.

    On POSIX: uses ``loop.add_signal_handler`` (the asyncio-native path).
    On Windows: falls back to ``signal.signal`` which is what asyncio
    supports there. SIGTERM on Windows is synthesized as SIGBREAK semantics
    by the runtime; we still register it when the symbol exists.
    """
    if sys.platform == "win32":
        signal.signal(signal.SIGINT, lambda *_: stop_fn())
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, lambda *_: stop_fn())
        return
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_fn)


def control_socket_path(name: str) -> str:
    """Return a writable control-socket path that works on POSIX and Windows.

    POSIX default: ``/tmp/<name>.sock``. Override with ``$HL_CONTROL_DIR``.
    Windows: ``tempfile.gettempdir()/<name>.sock``. The control server itself
    is skipped on Windows (AF_UNIX is unavailable); this helper only returns
    a consistent path for tooling that advertises the default.
    """
    env = os.environ.get("HL_CONTROL_DIR")
    if env:
        base = Path(env)
    elif sys.platform == "win32":
        base = Path(tempfile.gettempdir())
    else:
        base = Path("/tmp")
    base.mkdir(parents=True, exist_ok=True)
    return str(base / f"{name}.sock")


def supports_unix_sockets() -> bool:
    """True iff the current platform provides AF_UNIX."""
    return sys.platform != "win32"
