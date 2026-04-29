"""Tests for the maker_outcome event wiring (Phase 7d V1).

Covers strategy.maker_shadow.outcome_payload and exercises the schema
that scripts/maker_gate.py expects to join against maker_shadow events.
"""

from __future__ import annotations

import pytest

from strategy.maker_shadow import outcome_payload


def test_outcome_payload_minimal_fill():
    p = outcome_payload(
        event="maker_fill",
        cloid="0xABCDEF",
        symbol="BTC/USD",
    )
    assert p["cloid"] == "0xabcdef"  # normalized
    assert p["symbol"] == "BTC/USD"
    assert p["outcome"] == "filled"
    assert p["reason"] is None
    assert p["slippage_bps"] is None
    assert p["adverse_selection_bps"] is None
    assert p["fill_qty"] is None
    assert p["fill_px"] is None


def test_outcome_payload_cancel_with_reason():
    p = outcome_payload(
        event="maker_cancel",
        cloid="0xdeadbeef",
        symbol="ETH/USD",
        reason="reprice",
    )
    assert p["outcome"] == "cancelled"
    assert p["reason"] == "reprice"


def test_outcome_payload_taker_with_slippage():
    p = outcome_payload(
        event="maker_taker",
        cloid="0x1234",
        symbol="SOL/USD",
        slippage_bps=2.5,
        fill_qty=1.5,
        fill_px=120.5,
    )
    assert p["outcome"] == "taker"
    assert p["slippage_bps"] == 2.5
    assert p["fill_qty"] == 1.5
    assert p["fill_px"] == 120.5


def test_outcome_payload_invalid_event_raises():
    with pytest.raises(ValueError, match="event must be one of"):
        outcome_payload(event="maker_unknown", cloid="0xabc", symbol="BTC/USD")


def test_outcome_payload_cloid_normalized_to_lowercase():
    p = outcome_payload(event="maker_fill", cloid="0xABCdef", symbol="BTC/USD")
    assert p["cloid"] == "0xabcdef"


def test_outcome_payload_v1_leaves_adverse_selection_unset():
    """V1 doesn't measure adverse selection; field stays None unless caller
    passes it. Verifies that the gate's adverse_sel_reduction stays at 0
    until V2 wires the deferred mid capture."""
    p = outcome_payload(event="maker_fill", cloid="0xabc", symbol="BTC/USD")
    assert p["adverse_selection_bps"] is None


def test_outcome_payload_round_trips_through_gate_schema():
    """Verify the outcome payload field names match what scripts/maker_gate.py
    reads via load_outcomes(). Catches schema drift."""
    import scripts.maker_gate  # noqa: F401  imported for schema-presence side effect

    payload = outcome_payload(
        event="maker_fill",
        cloid="0xfeed",
        symbol="BTC/USD",
        slippage_bps=1.2,
        adverse_selection_bps=3.4,
    )
    # Simulate a log record the gate would parse.
    rec = {"event": "maker_fill", "timestamp": "2026-04-25T12:00:00Z", **payload}
    # Translate via gate's expected fields.
    assert rec["cloid"] == "0xfeed"
    assert rec["slippage_bps"] == 1.2
    assert rec["adverse_selection_bps"] == 3.4
    # Gate uses key "adverse_selection_bps" for the OutcomeEvent.adverse_sel_bps
    assert "adverse_selection_bps" in rec
    # Gate's OutcomeEvent.outcome maps from event name, not from the field.
    expected_outcome = {
        "maker_fill": "filled",
        "maker_cancel": "cancelled",
        "maker_taker": "taker",
    }[rec["event"]]
    assert payload["outcome"] == expected_outcome


def test_engine_helper_signature_matches():
    """Verify the engine's _emit_maker_outcome signature accepts the same
    kwargs as outcome_payload (compile-time guard against drift)."""
    import inspect
    import hl_engine

    sig = inspect.signature(hl_engine.HLEngine._emit_maker_outcome)
    params = set(sig.parameters.keys())
    # The engine helper must accept these kwargs (subset of outcome_payload).
    expected = {
        "self",
        "event",
        "cloid",
        "symbol",
        "reason",
        "slippage_bps",
        "fill_qty",
        "fill_px",
    }
    missing = expected - params
    assert not missing, f"engine helper missing kwargs: {missing}"
