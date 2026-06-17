"""Cost-aware rebalance gate (NON-protected, advisory). Calibrated from the REAL modeled cost
(TAKER 0.05%/fill + slippage) with a no-trade drift band, and funding priced with the CORRECT signed
sign (carry collected = a CREDIT that ADDS to the realignment edge, never a positive cost that
suppresses the carry trades). Advisory only — it never sizes or vetoes; the CIO reads its verdict
and a HOLD overrides a pacing PRESS.
"""
import pytest

from futures_fund.rebalance_cost import realignment_edge_usd, should_rebalance


def test_hold_when_drift_below_band():
    # tiny drift (8% < 15% band) -> HOLD even with a juicy edge (don't churn for noise)
    r = should_rebalance(current_notional=5_000.0, target_notional=5_400.0, expected_edge_usd=50.0)
    assert r["action"] == "hold"
    assert r["drift"] < 0.15


def test_hold_when_cost_exceeds_edge():
    # drift 40% (>= band) but the realignment edge ($0.50) is below the turnover cost -> HOLD
    r = should_rebalance(current_notional=5_000.0, target_notional=7_000.0, expected_edge_usd=0.50)
    assert r["action"] == "hold"
    assert r["net"] < 0


def test_rebalance_when_edge_beats_cost():
    # drift 40%, traded $2000, cost ~$1.4 (0.07%), edge $10 -> REBALANCE
    r = should_rebalance(current_notional=5_000.0, target_notional=7_000.0, expected_edge_usd=10.0)
    assert r["action"] == "rebalance"
    assert r["net"] > 0
    assert r["cost"] == pytest.approx(2_000.0 * (0.0005 + 0.0002), rel=1e-6)


def test_collected_funding_is_a_credit_that_raises_edge():
    # SHORT a positive-funding name -> we COLLECT funding -> edge INCLUDES a positive credit
    collected = realignment_edge_usd(target_notional=5_000.0, price_edge_bps=10.0,
                                     funding_rate=0.0003, direction="short", funding_events=3)
    # same price edge but funding PAID (long a positive-funding name) -> lower edge
    paid = realignment_edge_usd(target_notional=5_000.0, price_edge_bps=10.0,
                                funding_rate=0.0003, direction="long", funding_events=3)
    assert collected > paid
    # the funding component flips sign between the two
    price_only = 5_000.0 * 10.0 / 10_000.0
    assert collected > price_only > paid


def test_zero_funding_edge_is_just_price():
    e = realignment_edge_usd(target_notional=5_000.0, price_edge_bps=8.0,
                             funding_rate=0.0, direction="short", funding_events=2)
    assert e == pytest.approx(5_000.0 * 8.0 / 10_000.0)
