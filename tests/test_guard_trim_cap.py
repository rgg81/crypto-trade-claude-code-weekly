"""The neutrality guard must not shrink the whole book to match a MISSING leg.

The guard restores dollar-neutrality by trimming the oversized sleeve by the full gross difference.
That is right for rotation asymmetry (a small tilt), but wrong when a leg is missing: at cy291 an
L3/S1 book meant trimming the long sleeve 83% to match one surviving short, and deployment
collapsed 0.85x -> 0.11x. Shrinking to match a hole is not neutrality, it is liquidation.

Cap chosen so the COMMON case still fully corrects: one missing leg out of n_per_side=3 needs
|3-2|/3 = 0.333. The cap only bites when more than one leg is gone (L3/S1 -> 0.667, L3/S0 -> 1.0),
i.e. exactly the runaway case. A capped pass leaves residual tilt for one cycle, which the refill
(deployment_resizes) then closes — bounded tilt beats an unbounded deployment collapse.
"""
import pytest

from scripts.auto_cycle import _GUARD_TRIM_CAP, _guard_trim


def test_balanced_book_needs_no_trim():
    side, frac, capped = _guard_trim(5000.0, 5000.0)
    assert frac == 0.0 and capped is False


def test_small_rotation_tilt_is_corrected_in_full():
    side, frac, capped = _guard_trim(5200.0, 5000.0)
    assert side == "long"
    assert frac == pytest.approx(200.0 / 5200.0, abs=1e-4)
    assert capped is False


def test_one_missing_leg_of_three_still_fully_corrects():
    """L3/S2 at equal leg size -> 1/3 trim. Must NOT be capped: this is the common case."""
    side, frac, capped = _guard_trim(3 * 1679.0, 2 * 1679.0)
    assert side == "long"
    assert frac == pytest.approx(1 / 3, abs=1e-3)
    assert capped is False, "the routine single-missing-leg case must still neutralise fully"


def test_the_cy291_collapse_is_capped():
    """L3/S1: uncapped this trimmed 83% and took deployment to 0.11x."""
    side, frac, capped = _guard_trim(3470.0, 579.0)
    assert side == "long"
    assert capped is True
    assert frac == _GUARD_TRIM_CAP
    assert frac < 0.834, "must shrink far less than the uncapped 83%"


def test_cap_applies_to_an_oversized_short_sleeve_too():
    side, frac, capped = _guard_trim(579.0, 3470.0)
    assert side == "short"
    assert capped is True and frac == _GUARD_TRIM_CAP


def test_cap_never_increases_a_trim():
    """The guard may only ever SHRINK less, never more, than it would have."""
    for gl, gs in ((5200.0, 5000.0), (3470.0, 579.0), (1000.0, 999.0), (10.0, 0.0)):
        _, frac, _ = _guard_trim(gl, gs)
        uncapped = abs(gl - gs) / max(gl, gs, 1e-9)
        assert frac <= uncapped + 1e-9


def test_a_wholly_empty_sleeve_is_capped_not_liquidating():
    """gross_short == 0 would otherwise trim the entire long sleeve away."""
    side, frac, capped = _guard_trim(3470.0, 0.0)
    assert capped is True and frac == _GUARD_TRIM_CAP


def test_zero_gross_book_is_safe():
    side, frac, capped = _guard_trim(0.0, 0.0)
    assert frac == 0.0 and capped is False
