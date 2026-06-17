"""Trigger ingestion: the Trader emits `entry` (trader.md's documented field, shared with the
proposal shape), but PendingOrder requires `trigger_level`. The gate must normalize `entry` ->
`trigger_level` so a trigger using the natural Trader output ARMS instead of being silently
dropped, and it must SURFACE a reason when a trigger genuinely can't be built (Rule 8: never a
silent drop). Covered here on the pure helper `_build_trigger_order`."""
from futures_fund.orchestration import _build_trigger_order


def test_entry_field_normalizes_to_trigger_level():
    # A Trader trigger that uses `entry` (no `trigger_level`) must arm at that level.
    t = {"symbol": "SOLUSDT", "direction": "long", "kind": "stop_entry",
         "entry": 71.25, "stop": 69.8, "take_profits": [74.5, 77.5], "atr": 1.11,
         "confidence": 0.62, "require_oi_rising": True, "expires_cycle": 44}
    po, reason = _build_trigger_order(t, cycle_no=40)
    assert reason is None
    assert po is not None
    assert po.trigger_level == 71.25
    assert po.symbol == "SOLUSDT" and po.direction == "long" and po.kind == "stop_entry"
    assert po.stop == 69.8
    assert po.require_oi_rising is True
    assert po.expires_cycle == 44
    assert po.created_cycle == 40


def test_explicit_trigger_level_still_works():
    # Regression: a trigger already using `trigger_level` is unaffected.
    t = {"symbol": "ZECUSDT", "direction": "long", "kind": "stop_entry",
         "trigger_level": 476.96, "stop": 455.0, "take_profits": [525.3, 551.0], "atr": 13.6}
    po, reason = _build_trigger_order(t, cycle_no=40)
    assert reason is None and po is not None
    assert po.trigger_level == 476.96


def test_trigger_level_wins_when_both_present():
    # If both are present (prior cycles emitted both), trigger_level is authoritative.
    t = {"symbol": "TAOUSDT", "direction": "long", "kind": "stop_entry",
         "entry": 277.21, "trigger_level": 277.21, "stop": 260.0}
    po, reason = _build_trigger_order(t, cycle_no=33)
    assert reason is None and po is not None
    assert po.trigger_level == 277.21


def test_malformed_trigger_returns_reason_not_silent_drop():
    # No level at all -> cannot form a trigger -> a non-empty reason is returned (NOT swallowed).
    t = {"symbol": "BTCUSDT", "direction": "long", "kind": "stop_entry", "stop": 64850.0}
    po, reason = _build_trigger_order(t, cycle_no=40)
    assert po is None
    assert reason and "BTCUSDT" in reason


def test_malformed_trigger_missing_stop_returns_reason():
    # Missing the required `stop` -> reason surfaced, symbol named.
    t = {"symbol": "XRPUSDT", "direction": "long", "kind": "stop_entry", "entry": 1.19}
    po, reason = _build_trigger_order(t, cycle_no=40)
    assert po is None
    assert reason and "XRPUSDT" in reason
