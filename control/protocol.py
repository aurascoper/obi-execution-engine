"""
control/protocol.py — Newline-delimited JSON protocol for HL engine control plane.

Phase 1: read-only commands (get, get_all, snapshot).
Phase 2 (future): write commands (set_z, set_notional) — validation shipped now.
"""

from __future__ import annotations

import json

# ── Serialisation ─────────────────────────────────────────────────────────────

def serialize(obj: dict) -> bytes:
    """Encode a dict as a newline-delimited JSON message."""
    return json.dumps(obj, default=str, separators=(",", ":")).encode() + b"\n"


def deserialize(data: bytes) -> dict:
    """Decode a newline-delimited JSON message back to a dict."""
    return json.loads(data.strip())


# ── Phase 2 param validation (shipped now, wired later) ──────────────────────

def validate_params(params: dict) -> str | None:
    """
    Validate a proposed parameter update dict.

    Returns an error string if invalid, or None if all checks pass.
    Expected keys (all optional — only present keys are validated):
      z_entry, z_exit, z_short_entry, z_exit_short, notional
    """
    z_entry       = params.get("z_entry")
    z_exit        = params.get("z_exit")
    z_short_entry = params.get("z_short_entry")
    z_exit_short  = params.get("z_exit_short")
    notional      = params.get("notional")

    # ── z_entry: negative, [-5.0, -0.1] ──────────────────────────────────────
    if z_entry is not None:
        if not isinstance(z_entry, (int, float)):
            return "z_entry must be a number"
        if not (-5.0 <= z_entry <= -0.1):
            return f"z_entry must be in [-5.0, -0.1], got {z_entry}"

    # ── z_exit: negative, closer to zero than z_entry ────────────────────────
    if z_exit is not None:
        if not isinstance(z_exit, (int, float)):
            return "z_exit must be a number"
        if z_exit >= 0:
            return f"z_exit must be negative, got {z_exit}"
        if z_entry is not None and z_exit <= z_entry:
            return f"z_exit ({z_exit}) must be closer to zero than z_entry ({z_entry})"

    # ── z_short_entry: positive, [0.1, 5.0] ─────────────────────────────────
    if z_short_entry is not None:
        if not isinstance(z_short_entry, (int, float)):
            return "z_short_entry must be a number"
        if not (0.1 <= z_short_entry <= 5.0):
            return f"z_short_entry must be in [0.1, 5.0], got {z_short_entry}"

    # ── z_exit_short: positive, closer to zero than z_short_entry ────────────
    if z_exit_short is not None:
        if not isinstance(z_exit_short, (int, float)):
            return "z_exit_short must be a number"
        if z_exit_short <= 0:
            return f"z_exit_short must be positive, got {z_exit_short}"
        if z_short_entry is not None and z_exit_short >= z_short_entry:
            return (
                f"z_exit_short ({z_exit_short}) must be closer to zero "
                f"than z_short_entry ({z_short_entry})"
            )

    # ── notional: [10.0, 550.0] ──────────────────────────────────────────────
    if notional is not None:
        if not isinstance(notional, (int, float)):
            return "notional must be a number"
        if not (10.0 <= notional <= 550.0):
            return f"notional must be in [10.0, 550.0], got {notional}"

    return None
