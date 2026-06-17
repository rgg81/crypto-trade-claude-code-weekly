"""Inverse-sizing utility for the dollar-neutral pre-sizer: given a TARGET notional, return the
risk_pct that the gate's fixed-fractional qty_from_risk needs to hit it. The round-trip property is
the contract — notional_to_risk_pct is the exact algebraic inverse of sizing.qty_from_risk, so a
leg pre-sized to ~equity/2/n reaches that notional (subject to the gate's (0,1] risk_mult clamp and
the heat cap). Non-protected; reuses qty_from_risk untouched.
"""
import pytest

from futures_fund.notional_sizing import notional_to_risk_pct
from futures_fund.sizing import qty_from_risk


def test_round_trip_recovers_target_notional():
    equity, entry, stop, target = 10_000.0, 100.0, 95.0, 1_666.0
    rp = notional_to_risk_pct(target, entry, stop, equity)
    qty = qty_from_risk(equity, rp, entry, stop)
    assert qty * entry == pytest.approx(target, rel=1e-9)


def test_round_trip_short_side_stop_above_entry():
    equity, entry, stop, target = 10_000.0, 50.0, 52.5, 1_500.0  # short: stop above entry
    rp = notional_to_risk_pct(target, entry, stop, equity)
    qty = qty_from_risk(equity, rp, entry, stop)
    assert qty * entry == pytest.approx(target, rel=1e-9)


def test_wider_stop_needs_more_risk_pct():
    # same target notional, wider stop -> larger risk_pct (more $ at risk for the same notional)
    eq, entry, target = 10_000.0, 100.0, 2_000.0
    tight = notional_to_risk_pct(target, entry, 98.0, eq)   # 2% stop
    wide = notional_to_risk_pct(target, entry, 90.0, eq)    # 10% stop
    assert wide > tight > 0.0


def test_zero_stop_distance_is_zero():
    assert notional_to_risk_pct(1000.0, 100.0, 100.0, 10_000.0) == 0.0


def test_zero_or_negative_inputs_are_safe():
    assert notional_to_risk_pct(0.0, 100.0, 95.0, 10_000.0) == 0.0
    assert notional_to_risk_pct(1000.0, 100.0, 95.0, 0.0) == 0.0
    assert notional_to_risk_pct(1000.0, 0.0, 95.0, 10_000.0) == 0.0
