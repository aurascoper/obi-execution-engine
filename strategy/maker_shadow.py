"""
strategy/maker_shadow.py — Phase 7d shadow-mode adapter for MakerPolicy.

Live-path guarantees:
    • Import never crashes the engine if torch / weights are missing.
    • `.suggest()` returns `None` when the policy is unavailable — the caller
      falls back to its existing heuristic. No behavior change in that case.
    • Shadow mode executes with the heuristic and logs the policy's action
      alongside; cutover to `rl` is a separate env flag.

Responsibilities:
    1. Load `MakerPolicy` weights lazily from `MAKER_POLICY_WEIGHTS`.
    2. Accept a `MakerState` from the engine and return `(Action, log_prob)`.
    3. Maintain a rolling latency sample so `latency_p50/p95` features stay
       fresh between `.suggest()` calls.

The engine owns the order-tracker / book inputs and composes the state; this
module stays a pure inference wrapper (parallel to `strategy.hedge_shadow`).
"""

from __future__ import annotations

import pathlib
from collections import deque
from typing import Optional

import numpy as np

try:
    import torch  # noqa: F401

    _TORCH_OK = True
except Exception:  # noqa: BLE001
    _TORCH_OK = False

from models.maker_policy import (
    Action,
    MakerState,
    latency_percentiles,
)


class MakerShadow:
    """Loads MakerPolicy once, answers `.suggest()` cheaply on every tick."""

    __slots__ = (
        "_policy",
        "_weights_path",
        "_latency_window",
        "_latency_sample",
        "_loaded",
        "_load_error",
    )

    def __init__(
        self,
        weights_path: str,
        *,
        latency_window: int = 128,
    ) -> None:
        self._weights_path = str(weights_path)
        self._latency_window = int(latency_window)
        self._latency_sample: deque[float] = deque(maxlen=self._latency_window)
        self._policy = None
        self._loaded = False
        self._load_error: Optional[str] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    def try_load(self) -> bool:
        """Attempt to load weights. Returns True on success, False otherwise.
        Safe to call repeatedly — no-op if already loaded; memoizes errors."""
        if self._loaded:
            return True
        if self._load_error is not None:
            return False
        if not _TORCH_OK:
            self._load_error = "torch_unavailable"
            return False
        path = pathlib.Path(self._weights_path)
        if not path.exists():
            self._load_error = f"weights_missing:{self._weights_path}"
            return False
        try:
            from models.maker_policy import load as load_policy

            self._policy = load_policy(str(path))
            self._loaded = True
            return True
        except Exception as exc:  # noqa: BLE001
            self._load_error = f"load_failed:{type(exc).__name__}"
            return False

    @property
    def ready(self) -> bool:
        return self._loaded

    @property
    def last_error(self) -> Optional[str]:
        return self._load_error

    # ── Latency feed (called from hl_manager timing shim) ────────────────────
    def record_rpc_latency(self, wall_ms: float) -> None:
        """Push a single RPC round-trip into the rolling sample."""
        try:
            v = float(wall_ms)
        except (TypeError, ValueError):
            return
        if v >= 0.0:
            self._latency_sample.append(v)

    def latency_features(self) -> tuple[float, float]:
        return latency_percentiles(self._latency_sample)

    # ── Inference ────────────────────────────────────────────────────────────
    def suggest(
        self,
        state: MakerState,
        *,
        deterministic: bool = True,
    ) -> Optional[dict]:
        """Return `{action, action_name, log_prob, probs}` or None if not ready.

        `deterministic=True` matches shadow-mode usage: we log the argmax action
        so the counterfactual is reproducible. Training should sample instead.
        """
        if not self.try_load():
            return None
        assert self._policy is not None
        action_idx, logp = self._policy.act(state, deterministic=deterministic)
        probs = self._policy.action_distribution(state)
        return {
            "action": int(action_idx),
            "action_name": Action(action_idx).name,
            "log_prob": float(logp.detach().item())
            if hasattr(logp, "detach")
            else float(logp),
            "probs": probs.tolist(),
        }


# ── Helpers used by the engine to build MakerState without torch ───────────
def build_state(
    *,
    obi: float,
    log_gofi: float,
    mlofi: float,
    spread_bps: float,
    depth_top3_bps: float,
    my_rest_age_ms: float,
    my_rest_dist_bps: float,
    latency_p50_ms: float,
    latency_p95_ms: float,
    queue_position_pct: float,
    inventory_notional: float,
) -> MakerState:
    """Keyword-only MakerState constructor — matches the engine's call style.

    Clamps the bounded scalars (obi, log_gofi, mlofi, queue_position_pct) to
    their declared ranges so an upstream bug can't push the policy off-manifold.
    """
    return MakerState(
        obi=float(np.clip(obi, -1.0, 1.0)),
        log_gofi=float(np.clip(log_gofi, -1.0, 1.0)),
        mlofi=float(np.clip(mlofi, -1.0, 1.0)),
        spread_bps=max(0.0, float(spread_bps)),
        depth_top3_bps=max(0.0, float(depth_top3_bps)),
        my_rest_age_ms=max(0.0, float(my_rest_age_ms)),
        my_rest_dist_bps=max(0.0, float(my_rest_dist_bps)),
        latency_p50_ms=max(0.0, float(latency_p50_ms)),
        latency_p95_ms=max(0.0, float(latency_p95_ms)),
        queue_position_pct=float(np.clip(queue_position_pct, 0.0, 1.0)),
        inventory_notional=float(inventory_notional),
    )


# ── Structured-log event helper (keeps event schema in one place) ──────────
def shadow_event_payload(
    *,
    symbol: str,
    cloid: Optional[str],
    heuristic_action: str,
    suggestion: Optional[dict],
    state: MakerState,
) -> dict:
    """Shape of the `maker_shadow` structlog event. The engine should emit this
    exactly — the gate script in scripts/maker_gate.py parses these fields."""
    payload = {
        "event": "maker_shadow",
        "symbol": symbol,
        "cloid": cloid,
        "heuristic": heuristic_action,
        "state": list(state),
    }
    if suggestion is not None:
        payload.update(
            {
                "policy_action": suggestion["action_name"],
                "policy_action_idx": suggestion["action"],
                "policy_log_prob": suggestion["log_prob"],
                "policy_probs": suggestion["probs"],
            }
        )
    else:
        payload["policy_action"] = None
    return payload


_OUTCOME_EVENT_NAMES = ("maker_fill", "maker_cancel", "maker_taker")
_OUTCOME_LABELS = {
    "maker_fill": "filled",
    "maker_cancel": "cancelled",
    "maker_taker": "taker",
}


def outcome_payload(
    *,
    event: str,  # one of _OUTCOME_EVENT_NAMES
    cloid: str,
    symbol: str,
    reason: Optional[str] = None,
    slippage_bps: Optional[float] = None,
    adverse_selection_bps: Optional[float] = None,
    fill_qty: Optional[float] = None,
    fill_px: Optional[float] = None,
) -> dict:
    """Payload for `maker_fill` / `maker_cancel` / `maker_taker` log events.

    Keyed by `cloid` so `scripts/maker_gate.py` can join with `maker_shadow`.
    `slippage_bps` and `adverse_selection_bps` may be `None` when unmeasured
    (V1 leaves adverse_selection_bps unset; V2 wires the deferred mid capture).
    """
    if event not in _OUTCOME_EVENT_NAMES:
        raise ValueError(f"event must be one of {_OUTCOME_EVENT_NAMES}, got {event!r}")
    return {
        "cloid": str(cloid).lower(),
        "symbol": symbol,
        "outcome": _OUTCOME_LABELS[event],
        "reason": reason,
        "slippage_bps": slippage_bps,
        "adverse_selection_bps": adverse_selection_bps,
        "fill_qty": fill_qty,
        "fill_px": fill_px,
    }
