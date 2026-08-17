"""A count-imbalanced book that is NOT mid-rotation must still be able to refill.

cy290-291 deadlock: short-sleeve opens were dropped, the neutrality guard trimmed the long sleeve
to match the surviving shorts (12%, then 83%), and deployment collapsed 0.85x -> 0.11x. The only
mechanism that can grow a shrunken book — `deployment_resizes` (close+reopen, since held legs
cannot pyramid) — returned an empty set on a book sitting 6x below target, because its
MID-ROTATION GUARD bails whenever a side is short a leg:

    if any(len(sides[d]) != n_per_side ...): return set()

That guard is correct while a rotation is in flight (the caller passes only KEPT legs, and the
rotation-in fills the empty slot this cycle). It is wrong when nothing is being opened: then the
missing leg is not "about to arrive", the book is simply stuck — and refusing to resize keeps it
stuck forever. The distinction is whether opens are PLANNED for the short-handed side.
"""
import pytest

from futures_fund.blended_score import deployment_resizes

EQ = 10072.38


def _book(long_notional, short_notional):
    """(holdings, notional_by_sym) for the given per-leg notionals."""
    holdings, notional = {}, {}
    for i, n in enumerate(long_notional):
        holdings[f"L{i}USDT"] = "long"
        notional[f"L{i}USDT"] = n
    for i, n in enumerate(short_notional):
        holdings[f"S{i}USDT"] = "short"
        notional[f"S{i}USDT"] = n
    return holdings, notional


def test_the_live_collapse_is_now_refilled():
    """The cy291 book: L3 dust + S1 dust, nothing planned. Must resize, not sit at 0.11x."""
    holdings, notional = _book([275.0, 278.0, 21.0], [579.0])
    out = deployment_resizes(holdings, notional, EQ, 3, planned_opens_by_side={})
    assert out, "a stuck, massively under-deployed book must be resized"


def test_mid_rotation_is_still_deferred():
    """The original purpose: a side short a slot that THIS cycle's rotation will fill."""
    holdings, notional = _book([1679.0, 1679.0, 1679.0], [1679.0, 1679.0])
    out = deployment_resizes(holdings, notional, EQ, 3,
                             planned_opens_by_side={"short": 1})
    assert out == set(), "must not resize while the rotation-in is about to fill the slot"


def test_full_and_well_deployed_book_is_left_alone():
    """No churn when the book is already near its achievable size."""
    holdings, notional = _book([1679.0, 1679.0, 1679.0], [1679.0, 1679.0, 1679.0])
    assert deployment_resizes(holdings, notional, EQ, 3, planned_opens_by_side={}) == set()


def test_full_but_starved_book_is_resized():
    """Pre-existing behaviour must be preserved: a full but frozen book still tops up."""
    holdings, notional = _book([200.0, 200.0, 200.0], [200.0, 200.0, 200.0])
    assert deployment_resizes(holdings, notional, EQ, 3, planned_opens_by_side={})


def test_any_rotation_in_flight_still_defers():
    """Conservative on purpose: a mid-rotation side's `landed` is computed from kept legs only and
    is over-large, so resizing against it misfires. Waiting one cycle costs nothing, and the
    deadlock case (no opens planned at all) still resolves."""
    holdings, notional = _book([275.0, 278.0], [21.0])
    out = deployment_resizes(holdings, notional, EQ, 3,
                             planned_opens_by_side={"long": 1})
    assert out == set()


def test_backwards_compatible_default_defers_like_before():
    """Called without the new argument (legacy callers), an imbalanced book still defers."""
    holdings, notional = _book([1679.0, 1679.0, 1679.0], [1679.0, 1679.0])
    assert deployment_resizes(holdings, notional, EQ, 3) == set()


def test_empty_book_is_safe():
    assert deployment_resizes({}, {}, EQ, 3, planned_opens_by_side={}) == set()


def test_zero_equity_is_safe():
    holdings, notional = _book([100.0], [100.0])
    assert deployment_resizes(holdings, notional, 0.0, 3, planned_opens_by_side={}) == set()


@pytest.mark.parametrize("n", [1, 2, 3])
def test_never_returns_symbols_outside_the_book(n):
    holdings, notional = _book([50.0] * n, [50.0])
    out = deployment_resizes(holdings, notional, EQ, 3, planned_opens_by_side={})
    assert out <= set(holdings)
