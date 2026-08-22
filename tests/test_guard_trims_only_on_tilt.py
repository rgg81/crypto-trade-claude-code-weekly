"""A count imbalance is a REFILL problem, not a TRIM problem.

The post-gate guard fired on `abs(tilt) > 0.03 OR n_long != n_short`. The second condition is
wrong on its own: trimming a sleeve moves DOLLARS, and it cannot add the missing leg that caused
the count imbalance. When the book is already dollar-neutral, trimming just pays fees and can make
neutrality slightly worse.

Live at cy319 (only 3 names passed the pump filter, so the book was L1/S2 but balanced):

    gate: net $+1 tilt 0.0009 L1/S2
    NEUTRALITY GUARD: tilt 0.001 -> trimming long sleeve by 0.0018
      after guard: net $+1 tilt 0.0011        <- WORSE than before the trim

The tilt was 33x inside the band. Nothing needed correcting, and the trim moved it the wrong way.

A genuine imbalance that also skews the dollars still trips the tilt condition, so nothing that
mattered is lost — cy317 (tilt 0.127, L3/S3) and cy318 (tilt 0.098, L2/S2) both still trim, and
neither was a count mismatch in the first place. The imbalance stays SURFACED (HARD RULE 8); it
just no longer triggers a pointless round-trip.
"""
import pytest

from scripts.auto_cycle import _GUARD_TILT_BAND, _guard_should_trim


def _exp(tilt, n_long, n_short):
    return {"tilt": tilt, "n_long": n_long, "n_short": n_short}


def test_the_live_cy319_case_must_not_trim():
    """THE REGRESSION: balanced book, uneven counts -> the trim made tilt worse."""
    assert _guard_should_trim(_exp(0.0009, 1, 2)) is False


def test_a_real_tilt_still_trims():
    """cy318 and cy317: genuinely oversized sleeves, both must still be corrected."""
    assert _guard_should_trim(_exp(0.0982, 2, 2)) is True
    assert _guard_should_trim(_exp(0.1268, 3, 3)) is True


def test_a_tilted_and_count_imbalanced_book_still_trims():
    """The case the count condition was meant to catch is caught by tilt anyway."""
    assert _guard_should_trim(_exp(0.32, 3, 1)) is True


def test_the_band_edge_is_exclusive():
    assert _guard_should_trim(_exp(_GUARD_TILT_BAND, 3, 3)) is False
    assert _guard_should_trim(_exp(_GUARD_TILT_BAND + 1e-9, 3, 3)) is True


def test_a_negative_tilt_is_treated_by_magnitude():
    assert _guard_should_trim(_exp(-0.10, 3, 3)) is True
    assert _guard_should_trim(_exp(-0.001, 3, 3)) is False


@pytest.mark.parametrize("nl,ns", [(1, 2), (2, 1), (3, 2), (2, 3), (0, 3), (3, 0)])
def test_no_count_shape_alone_triggers_a_trim(nl, ns):
    """Including the naked-sleeve shapes: a flat side needs a REFILL, and trimming the surviving
    sleeve is what drove the cy295 ratchet (1483 -> 964 -> 626)."""
    assert _guard_should_trim(_exp(0.001, nl, ns)) is False


def test_the_band_matches_what_the_guard_documents():
    assert _GUARD_TILT_BAND == 0.03


def test_the_guard_uses_the_predicate():
    import inspect

    from scripts import auto_cycle
    src = inspect.getsource(auto_cycle.main)
    assert "if _guard_should_trim(e):" in src, "the trim must be gated by the predicate"
    assert '0.03 or e["n_long"] != e["n_short"]' not in src, (
        "the count condition must no longer be OR-ed into the trim trigger")
    # the mismatch is still reported, just not acted on with a trim
    assert 'e["n_long"] != e["n_short"] and not _guard_should_trim(e)' in src, (
        "a dollar-neutral count mismatch must still be surfaced (HARD RULE 8)")
