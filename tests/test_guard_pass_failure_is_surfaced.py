"""When the guard's gate pass fails, say so — do not imply the trim landed.

The neutrality guard runs a SECOND gate pass to apply its trim. If that pass dies (a rate-limit
mid-execute), `_gate_exposure` returns None and the trim never reaches the book — but the output
read as though it had. Live at cy322:

    NEUTRALITY GUARD: tilt 0.052 -> trimming short sleeve by 0.0994
    <ccxt DDoSProtection 429 traceback>
    SUMMARY cycle 322 | deployed | ... | equity 10122.83

Re-derived from state afterwards: long $640.09 vs short $712.01, tilt 0.0532 — essentially
unchanged from the 0.0523 the guard set out to fix. A reader sees "trimming ... by 0.0994" followed
by "deployed" and reasonably concludes the book was corrected. It was not, and it stays out of the
0.03 band until the next DUE candle ~4h later.

The book is safe throughout (deployed, both sleeves populated, $72 net on $10,123 equity = 0.7%),
so this is a REPORTING fix, not a risk fix. But an operator must be able to tell "guard trimmed"
from "guard tried and failed".
"""
from scripts.auto_cycle import _guard_outcome_line


def test_a_failed_pass_says_the_trim_did_not_land():
    msg = _guard_outcome_line(None, tilt_before=0.0523)
    assert "DID NOT LAND" in msg
    assert "0.0523" in msg
    assert "next DUE" in msg


def test_a_successful_pass_reports_the_corrected_book():
    rep2 = {"exposure": {"net": -27.0, "tilt": 0.0192, "n_long": 3, "n_short": 3}}
    msg = _guard_outcome_line(rep2, tilt_before=0.1721)
    assert "after guard" in msg
    assert "0.0192" in msg
    assert "DID NOT LAND" not in msg


def test_a_malformed_report_is_treated_as_a_failure():
    """Never claim success from a report we could not read."""
    assert "DID NOT LAND" in _guard_outcome_line({}, tilt_before=0.05)
    assert "DID NOT LAND" in _guard_outcome_line({"exposure": None}, tilt_before=0.05)


def test_the_guard_uses_it():
    import inspect

    from scripts import auto_cycle
    src = inspect.getsource(auto_cycle.main)
    assert "_guard_outcome_line(rep2" in src
    assert 'print(f"  after guard: net ${e2[\'net\']:+.0f}' not in src, (
        "the old success-only line must be gone; it printed nothing on failure")
