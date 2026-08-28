"""The turnover replay harness must be trustworthy before any parameter is changed on its say-so.

The desk's problem is not its edge — it earned +$564.22 gross over 71 days — but that fees took
$561.30 of it, 99.5%. The biggest identified waste is structural: 123 of 381 opens (32.3%) are a
close and reopen of the SAME symbol in the SAME cycle, a full 0.14% round trip that changes a leg's
size without changing the desk's view.

Before touching a single parameter I want evidence, so this replays the 355 recorded cycles under
alternative turnover settings and prices the trade-off. These tests pin the harness's arithmetic —
if the simulator is wrong, every recommendation drawn from it is wrong.

Sizing is deliberately held CONSTANT per leg across parameter sets. Real sizing swung 0.85x ->
0.11x -> 0.5x driven by the breaker ladder, which depends on the equity path, which in turn
depends on the policy being tested — circular. Fixing notional makes the comparison apples-to-
apples, and the harness is validated instead against TURNOVER, which is parameter-driven and
directly observable in the record.
"""
import pytest

from scripts.replay_turnover import (
    FEE_PER_FILL,
    fees_for,
    forward_return,
    funding_pnl,
    leg_pnl,
)


def test_fee_is_the_binance_taker_rate():
    assert FEE_PER_FILL == pytest.approx(0.0007)


def test_a_round_trip_costs_two_fills():
    """The 0.14% figure the whole analysis rests on."""
    assert fees_for(opens=1, closes=1, notional=1000.0) == pytest.approx(1000.0 * 0.0014)
    assert fees_for(opens=0, closes=0, notional=1000.0) == 0.0


def test_a_close_and_reopen_of_one_symbol_is_a_full_round_trip():
    """The 32.3% waste: same symbol, same cycle, two fills, no change of view."""
    assert fees_for(opens=1, closes=1, notional=500.0) == pytest.approx(0.70)


def test_forward_return_is_simple_price_change():
    assert forward_return(100.0, 110.0) == pytest.approx(0.10)
    assert forward_return(100.0, 90.0) == pytest.approx(-0.10)
    assert forward_return(0.0, 10.0) == 0.0, "no price -> no return, never a divide-by-zero"


def test_leg_pnl_signs_with_direction():
    """A short profits when price falls. Getting this backwards would invert every conclusion."""
    assert leg_pnl("long", 1000.0, 100.0, 110.0) == pytest.approx(+100.0)
    assert leg_pnl("short", 1000.0, 100.0, 110.0) == pytest.approx(-100.0)
    assert leg_pnl("short", 1000.0, 100.0, 90.0) == pytest.approx(+100.0)


def test_funding_matches_the_desks_own_convention():
    """A LONG pays when the rate is positive; a SHORT receives it. Same sign convention as
    exits.py ('positive = we PAID it') and _long_favorability (carry favours long when funding
    is negative). Pro-rated over the 4h cycle against the symbol's funding interval."""
    # rate +0.01% per 8h interval, 4h cycle -> half an interval
    assert funding_pnl("long", 10_000.0, 0.0001, 8.0) == pytest.approx(-0.50)
    assert funding_pnl("short", 10_000.0, 0.0001, 8.0) == pytest.approx(+0.50)
    # negative rate flips it: the long now COLLECTS
    assert funding_pnl("long", 10_000.0, -0.0001, 8.0) == pytest.approx(+0.50)
    # a 4h funding interval means a whole event per cycle
    assert funding_pnl("long", 10_000.0, 0.0001, 4.0) == pytest.approx(-1.00)


def test_funding_degrades_safely_on_bad_inputs():
    assert funding_pnl("long", 1000.0, None, 8.0) == 0.0
    assert funding_pnl("long", 1000.0, 0.0001, 0.0) == 0.0
    assert funding_pnl("long", 1000.0, 0.0001, None) == pytest.approx(-0.05)  # default 8h
