"""The tick must report the equity of the book that actually stands.

The neutrality guard runs a SECOND gate pass: it closes part of the oversized sleeve, pays the
fees, and re-marks the book. So `rep` — the pre-guard report — is one pass stale whenever the guard
fires, and `rep2` (already computed, only used for a progress line) holds the real number.

Live at cy305 the guard trimmed the short sleeve 6.19% and the summary printed

    equity 10149.71 | PnL $+137.80 (+1.38%)

while the standing book reconciled to $10196.73 (balance $10198.45 + $-1.72 unrealized, re-derived
per HARD RULE 5 from state/cycle/305/context.json marks). A $47.02 error — 0.46% of equity — and it
flowed straight into the PnL figure, understating it by the same amount.

This matters more than the size suggests: the SUMMARY equity is also what gets compared against the
last logged equity, so a stale read makes the desk look like it lost money it never lost.
"""
import pytest

from scripts.auto_cycle import _reported_equity

PRE = {"equity": 10149.71}          # live cy305, pre-guard
POST = {"equity": 10196.73}         # the book that actually stands


def test_the_live_cy305_case():
    assert _reported_equity(PRE, POST) == pytest.approx(10196.73)
    assert _reported_equity(PRE, POST) - PRE["equity"] == pytest.approx(47.02, abs=0.01)


def test_without_a_guard_pass_the_first_report_stands():
    """The guard does not fire on a balanced tick — nothing to correct."""
    assert _reported_equity(PRE, None) == pytest.approx(10149.71)
    assert _reported_equity(PRE) == pytest.approx(10149.71)


def test_a_failed_guard_pass_falls_back_rather_than_crashing():
    """`_gate_exposure` returns None on a rate-limit mid-execute; the summary must still print."""
    assert _reported_equity(PRE, {}) == pytest.approx(10149.71)
    assert _reported_equity(PRE, {"exposure": {}}) == pytest.approx(10149.71)
    assert _reported_equity(PRE, {"equity": None}) == pytest.approx(10149.71)


def test_the_summary_uses_it():
    """Wiring: the pre-guard report must not reach the printed equity or the PnL line."""
    import inspect

    from scripts import auto_cycle
    src = inspect.getsource(auto_cycle.main)
    tail = src[src.index("SUMMARY cycle"):]
    assert "rep['equity']" not in tail, "summary still prints the stale pre-guard equity"
    assert "_reported_equity(rep, rep2)" in tail
    assert "_pnl_line(_reported_equity(rep, rep2))" in tail


def test_rep2_is_in_scope_for_the_summary():
    """rep2 was defined only inside the guard branch; the summary needs it either way."""
    import inspect

    from scripts import auto_cycle
    src = inspect.getsource(auto_cycle.main)
    assert "rep2 = None" in src, "rep2 must be initialised before the guard branch"
    assert src.index("rep2 = None") < src.index("SUMMARY cycle")
