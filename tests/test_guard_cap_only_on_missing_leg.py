"""The 35% trim cap guards against a MISSING leg — not against a structurally capped sleeve.

`_guard_trim` caps its trim at 35% whenever neutralising would need more, on the theory that a
large required trim means a leg is missing and shrinking to match it would liquidate the book
(cy290-291: L3/S1, deployment 0.85x -> 0.11x). That heuristic is right when the counts differ. It
is wrong when both sleeves are FULL.

cy326, L3/S3 with every leg present:

    ZECUSDT      long  $164.31  stop 15.396%  risk 0.00250
    1000PEPEUSDT long  $157.84  stop 16.026%  risk 0.00250
    XRPUSDT      long  $180.26  stop 14.035%  risk 0.00250
    BTCUSDT      short $458.09  stop  4.117%  risk 0.00190
    BNBUSDT      short $356.67  stop  5.117%  risk 0.00184
    ETHUSDT      short $356.14  stop  5.413%  risk 0.00195

Every long sits at risk_mult = 1.0 — exactly the caution x breaker per-trade risk of 0.0025 — so
each is pinned at `ptr * equity / stop_frac` and CANNOT grow. Their stops are three times wider than
the shorts', so the long sleeve maxes out at $502.41 against a $1170.90 short sleeve. The pre-sizer
saw this and set balanced_gross to 502.41; the only way to neutralise is to shrink the SHORTS by
70.4%. The cap refused, leaving tilt 0.3981 — a $669 net short, 6.6% of equity, for a full 4h
candle, and the same thing again the cycle before.

Nothing is missing here, so nothing needs protecting: cap only when the leg counts differ.
Trimming still only ever SHRINKS, so a full trim cannot add risk — it costs deployment, and
dollar-neutrality is the mandate's hard invariant while deployment is not.
"""
import pytest

from scripts.auto_cycle import _GUARD_TRIM_CAP, _guard_trim


def test_a_full_book_trims_all_the_way_to_neutral():
    """THE REGRESSION: cy326 needed 70.4% off a full L3/S3 book and was capped at 35%."""
    big, frac, capped = _guard_trim(502.41, 1170.90, counts_balanced=True)
    assert big == "short"
    assert capped is False
    assert frac == pytest.approx(0.5709, abs=1e-4)


def test_the_full_trim_actually_neutralises():
    gl, gs = 502.41, 1170.90
    _, frac, _ = _guard_trim(gl, gs, counts_balanced=True)
    assert gs * (1 - frac) == pytest.approx(gl, rel=1e-3)


def test_a_missing_leg_is_still_capped():
    """cy295 (L3/S0) and cy325 (L1/S3): counts differ, so the cap still protects deployment."""
    assert _guard_trim(1482.83, 0.0, counts_balanced=False) == ("long", _GUARD_TRIM_CAP, True)
    big, frac, capped = _guard_trim(697.18, 2535.09, counts_balanced=False)
    assert (big, frac, capped) == ("short", _GUARD_TRIM_CAP, True)


def test_small_trims_are_unchanged_either_way():
    """cy317 (0.225) and cy321 (0.2936) were already under the cap — behaviour must not move."""
    for gl, gs in ((1000.0, 1290.3), (1000.0, 1415.6)):
        a = _guard_trim(gl, gs, counts_balanced=True)
        b = _guard_trim(gl, gs, counts_balanced=False)
        assert a == b
        assert a[2] is False


def test_the_default_stays_conservative():
    """An unspecified caller keeps the capped behaviour rather than silently trimming deeper."""
    assert _guard_trim(502.41, 1170.90)[2] is True


def test_the_trim_never_rounds_up():
    """Invariant preserved: the guard may only ever shrink LESS than the true difference."""
    gl, gs = 502.41, 1170.90
    _, frac, _ = _guard_trim(gl, gs, counts_balanced=True)
    assert frac <= abs(gs - gl) / max(gl, gs)


def test_the_caller_passes_the_count_balance():
    import inspect

    from scripts import auto_cycle
    src = inspect.getsource(auto_cycle.main)
    assert 'counts_balanced=e["n_long"] == e["n_short"]' in src
