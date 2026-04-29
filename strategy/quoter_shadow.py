"""strategy/quoter_shadow.py — Gate 3 shadow telemetry helper.

Pure observer. Given the engine's order-decision context, returns a dict
suitable for log emission describing what the math_core/quoter_policy.py
quoter would have recommended at this moment.

Not in any order or risk path. Imported by hl_engine.py at the existing
`sizing_runtime_shadow` hook only. The function NEVER raises (returns
{"shadow_status": "..."} on any input it can't handle); the caller still
wraps the call in try/except so even a programming bug in this module
cannot block live orders.

The synthetic framing: at decision time, treat the post-trade inventory
as if it were the principal of a fresh liquidation horizon (default 600s).
At t=0 of that horizon, e_t = q_t - q*(t) = 0, so regime is driven by
the OBI signal alone. This is exactly the "what would the quoter say
right now given OBI?" telemetry that Gate 3 needs.
"""

from __future__ import annotations

import math
from typing import Optional

from math_core.quoter_policy import (
    QuoterInputs,
    QuoterParams,
    Regime,
    Side,
    build_intent,
)


DEFAULT_HORIZON_S = 600.0
DEFAULT_TOUCH_SPREAD_BPS = 2.0


def shadow_quoter_payload(
    *,
    obi: Optional[float],
    intended_side_sign: int,
    notional_about_to_trade: float,
    notional_held_signed: float,
    mid: float,
    horizon_s: float = DEFAULT_HORIZON_S,
    touch_spread_bps: float = DEFAULT_TOUCH_SPREAD_BPS,
    quoter_params: Optional[QuoterParams] = None,
) -> dict:
    """Return shadow telemetry as a dict.

    Args:
        obi: latest OBI scalar from signals.py, or None if unavailable.
        intended_side_sign: +1 buy, -1 sell, 0 unknown.
        notional_about_to_trade: USD notional of the order under decision
            (always positive). Used to infer post-trade inventory when
            position is currently flat.
        notional_held_signed: signed USD notional currently held in the
            position. Positive = long, negative = short, 0 = flat.
        mid: latest mid price (positive). NaN/non-positive → skipped.
        horizon_s: synthetic flatten horizon for the quoter's framing.
        touch_spread_bps: fallback when engine doesn't surface spread.
        quoter_params: override defaults (typically None).

    Returns:
        Dict suitable for spreading into a log event. On invalid input,
        returns {"shadow_status": "skipped_<reason>"}.
    """
    if obi is None:
        return {"shadow_status": "skipped_no_obi"}
    try:
        obi_f = float(obi)
    except (TypeError, ValueError):
        return {"shadow_status": "skipped_bad_obi_type"}
    if not math.isfinite(obi_f):
        return {"shadow_status": "skipped_obi_nonfinite"}
    if not math.isfinite(mid) or mid <= 0:
        return {"shadow_status": "skipped_bad_mid"}
    if intended_side_sign not in (-1, 0, 1):
        return {"shadow_status": "skipped_bad_side_sign"}

    post_trade_inventory = float(notional_held_signed) + (
        float(intended_side_sign) * float(notional_about_to_trade)
    )
    if abs(post_trade_inventory) < 1e-6:
        return {
            "shadow_status": "skipped_post_trade_flat",
            "shadow_y_obi_seen": round(obi_f, 4),
        }

    params = quoter_params or QuoterParams()

    inp = QuoterInputs(
        t=0.0,
        T=horizon_s,
        q_t=post_trade_inventory,
        q_star_t=post_trade_inventory,
        mid=mid,
        touch_spread_bps=touch_spread_bps,
        y_toxicity=obi_f,
        initial_inventory=post_trade_inventory,
    )
    intent = build_intent(inp, params)

    return {
        "shadow_status": "ok",
        "shadow_regime": intent.regime.value,
        "shadow_order_type": intent.order_type.value,
        "shadow_side": intent.side.value,
        "shadow_delta_a_bps": intent.delta_a_bps,
        "shadow_delta_b_bps": intent.delta_b_bps,
        "shadow_clip_usd": round(intent.clip_size, 2),
        "shadow_ttl_s": intent.ttl_s,
        "shadow_e_t": round(intent.e_t, 4),
        "shadow_u_t": round(intent.u_t, 6),
        "shadow_reservation_price": round(intent.reservation_price, 6),
        "shadow_y_toxicity": round(obi_f, 4),
        "shadow_post_trade_inventory_usd": round(post_trade_inventory, 2),
        "shadow_horizon_s": horizon_s,
    }
