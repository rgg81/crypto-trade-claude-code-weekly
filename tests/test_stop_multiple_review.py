"""Monthly evaluation of the stop distance (`atr_mult`), with a NOISE GATE.

An aggregate replay is not evidence. On live data (310 trades) atr_mult=0.75 replayed +$268 better
than the live 2.0 — but split in half it was -$188 then +$423, i.e. regime luck, not edge. Every
candidate except 1.75 flipped sign between halves, and 1.75's effect was ~$29 (~0.3% of equity).

So the review may only propose a change when the improvement is CONSISTENT across both halves of
the sample AND materially large. Anything else reports "no change justified" — a monthly review
that chases the aggregate number would ratchet the desk onto whatever the last regime rewarded.
"""
import pytest

from scripts.monthly_review import stop_multiple_verdict

LIVE = 2.0


def _r(total, first, second):
    return {"total": total, "first_half": first, "second_half": second}


def test_noise_is_rejected_even_when_aggregate_looks_great():
    """The exact live 0.75 case: biggest aggregate gain, opposite signs per half."""
    v = stop_multiple_verdict({0.75: _r(267.54, -188.42, 423.20)}, live_mult=LIVE)
    assert v["recommend"] is None
    assert "consistent" in v["reason"].lower()


def test_consistent_but_immaterial_gain_is_rejected():
    """The live 1.75 case: same sign both halves, but ~$29 total is not worth a risk change."""
    v = stop_multiple_verdict({1.75: _r(29.20, 5.76, 23.44)}, live_mult=LIVE, min_gain=100.0)
    assert v["recommend"] is None
    assert "material" in v["reason"].lower()


def test_consistent_and_material_gain_is_recommended():
    v = stop_multiple_verdict({1.5: _r(400.0, 150.0, 250.0)}, live_mult=LIVE, min_gain=100.0)
    assert v["recommend"] == 1.5
    assert v["expected_gain"] == pytest.approx(400.0)


def test_best_qualifying_candidate_wins_not_the_best_aggregate():
    """A noisy 0.75 must not beat a consistent 1.5 just by having a bigger headline number."""
    v = stop_multiple_verdict(
        {0.75: _r(900.0, -400.0, 1300.0), 1.5: _r(400.0, 150.0, 250.0)},
        live_mult=LIVE, min_gain=100.0)
    assert v["recommend"] == 1.5


def test_a_losing_candidate_is_never_recommended():
    v = stop_multiple_verdict({1.0: _r(-178.09, -332.80, 137.70)}, live_mult=LIVE)
    assert v["recommend"] is None


def test_live_multiple_is_never_recommended_as_a_change():
    v = stop_multiple_verdict({LIVE: _r(0.0, 0.0, 0.0)}, live_mult=LIVE, min_gain=0.0)
    assert v["recommend"] is None


def test_empty_candidates_is_safe():
    v = stop_multiple_verdict({}, live_mult=LIVE)
    assert v["recommend"] is None
    assert v["candidates"] == {}


def test_verdict_records_every_candidate_for_the_report():
    """The report must show the rejected ones too — that's the audit trail for why nothing moved."""
    cands = {0.75: _r(267.54, -188.42, 423.20), 1.75: _r(29.20, 5.76, 23.44)}
    v = stop_multiple_verdict(cands, live_mult=LIVE)
    assert set(v["candidates"]) == {0.75, 1.75}
    assert v["candidates"][0.75]["consistent"] is False
    assert v["candidates"][1.75]["consistent"] is True


def test_close_only_caveat_is_carried_in_the_verdict():
    """The replay has no intrabar high/low, so it UNDERCOUNTS stop hits and flatters tight stops.
    A reader of the report must not mistake it for a faithful backtest."""
    v = stop_multiple_verdict({1.5: _r(400.0, 150.0, 250.0)}, live_mult=LIVE, min_gain=100.0)
    assert "close-only" in v["caveat"].lower()
