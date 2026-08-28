"""A resize must pay for itself. At a 10% deficit it never does.

MEASURED, over the 355 recorded cycles (scripts/replay_turnover.py):

  gross price edge      +$89.22 over 354 cycles on a $3,000 book
                     => 0.008401% per cycle per dollar deployed

A resize is a close+reopen: two taker fills, 0.14% of the leg's CURRENT notional, paid now, to buy
a larger leg later. Payback at the old `* 0.90` trigger:

    leg at 90% of target -> tops up $50, costs $0.63 ->  150 cycles (25 days) to break even
    leg at 75%           -> tops up $125, costs $0.53 ->  50 cycles ( 8 days)
    leg at 50%           -> tops up $250, costs $0.35 ->  17 cycles ( 3 days)
    leg at 25%           -> tops up $375, costs $0.17 ->   6 cycles ( 1 day)

A leg's actual holding period is 1-2 days (the book turns over in ~3-6 cycles), so the old trigger
bought top-ups that could never pay back before the leg rotated away. It fired on 123 of 381 opens
(32.3%), costing ~$86 of the ~$561 total fee bill for zero signal: replaying with resizes removed
changes GROSS not at all and improves net by exactly $86.10 in BOTH halves of the record.

Deleting the mechanism outright is wrong — it is the only gate-respecting way out of a deployment
collapse (held legs cannot pyramid; cy290-291 sat at 0.11x against a 0.85x target and could not
climb back). A genuine collapse is an ~87% deficit and still fires here. What stops is the routine
drift top-up.

The swap_margin was deliberately NOT tuned alongside this: every value flipped sign between halves
of the record (best in H1 = 1.25, best in H2 = 1.5), so choosing one would be fitting noise.
"""
import pytest

from futures_fund.blended_score import RESIZE_DEFICIT, deployment_resizes

EQ = 10_000.0
N = 3
PTR = 0.0025
STOP = dict.fromkeys(("AAAUSDT", "BBBUSDT", "CCCUSDT", "XXXUSDT", "YYYUSDT", "ZZZUSDT"), 0.02)
LONGS = {"AAAUSDT": "long", "BBBUSDT": "long", "CCCUSDT": "long"}
SHORTS = {"XXXUSDT": "short", "YYYUSDT": "short", "ZZZUSDT": "short"}
HOLD = {**LONGS, **SHORTS}


def _resize(fracs: dict[str, float]):
    """fracs = each leg's notional as a fraction of its per-slot target."""
    name_cap = 0.25 * EQ
    per_slot = min(EQ / 2.0, 3 * min(name_cap, PTR * EQ / 0.02)) / N
    notional = {s: per_slot * f for s, f in fracs.items()}
    return deployment_resizes(HOLD, notional, EQ, N, per_trade_risk_pct=PTR,
                              stop_frac_by_sym=STOP,
                              planned_opens_by_side={"long": 0, "short": 0})


def test_the_threshold_is_a_half_not_a_tenth():
    assert RESIZE_DEFICIT == 0.50


def test_a_ten_percent_drift_no_longer_churns():
    """THE FIX. 150 cycles to pay back a top-up on a leg that lives 1-2 days."""
    assert _resize(dict.fromkeys(HOLD, 0.90)) == set()


def test_a_quarter_deficit_still_does_not_churn():
    """75% of target -> 50 cycles (8 days) payback. Still far past a leg's life."""
    assert _resize(dict.fromkeys(HOLD, 0.75)) == set()


def test_a_genuine_collapse_still_refills():
    """cy290-291: the book sat at 0.11x of a 0.85x target and could not climb out. That is the
    case the mechanism exists for, and it must survive the change."""
    assert _resize(dict.fromkeys(HOLD, 0.13)) != set()


def test_the_boundary_is_where_payback_becomes_a_holding_period():
    """Just under half -> refill (~3 days payback). Just over -> leave it alone."""
    assert _resize(dict.fromkeys(HOLD, 0.49)) != set()
    assert _resize(dict.fromkeys(HOLD, 0.51)) == set()


def test_one_starved_leg_in_a_healthy_book_does_not_trigger_a_refill():
    """The book-level band decides first, and it is right to.

    Five legs at 95% and one at 20% leaves the BOOK near target, so no round trip is justified —
    the starved leg will rotate out long before a top-up could pay for itself. I expected this to
    refill the two starved legs and it does not; the band short-circuits, which is the correct
    reading of "is the book under-deployed?" rather than "is any leg under-deployed?".
    """
    assert _resize({**dict.fromkeys(HOLD, 0.95), "AAAUSDT": 0.20, "XXXUSDT": 0.20}) == set()


def test_in_a_starved_book_only_the_starved_legs_are_refilled():
    """When the book IS under-deployed, a leg that is already near target must not be dragged
    into someone else's refill and made to pay a round trip for nothing."""
    out = _resize({**dict.fromkeys(HOLD, 0.20), "CCCUSDT": 0.95, "ZZZUSDT": 0.95})
    assert "CCCUSDT" not in out and "ZZZUSDT" not in out, out
    assert out == {"AAAUSDT", "BBBUSDT", "XXXUSDT", "YYYUSDT"}, out


@pytest.mark.parametrize("frac", [0.55, 0.60, 0.70, 0.80, 0.90, 1.00])
def test_no_drift_level_above_the_threshold_ever_churns(frac):
    assert _resize(dict.fromkeys(HOLD, frac)) == set()
