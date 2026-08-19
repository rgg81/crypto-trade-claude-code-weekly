"""Every tick must report accumulated PnL since inception.

The one-line summary showed the book and the current equity, but never the number the operator
actually wants: is this desk up or down since it started, and by how much. Reading that off a bare
equity figure requires remembering the inception balance, which nobody does.

Inception is the FIRST entry in state/equity-history.jsonl (cycle 1), not a hardcoded constant —
`reset_desk.py` archives the history and starts a new one, so a hardcoded baseline would silently
report PnL against a desk that no longer exists.

Calc-vigilance (HARD RULE 5): a SKIP tick cannot mark to market without hitting Binance, so it
reports the last LOGGED equity and labels it `@cyN`. The pacing bug (memory: pacing-wtd-live-equity)
came from silently using a stale last-logged equity as if it were live; the label is what stops this
repeating it.
"""
import json

import pytest

from scripts import auto_cycle
from scripts.auto_cycle import _inception_equity, _last_logged, _pnl_line

INCEPTION = 10011.908079533556          # live cy1, 2026-06-17
LATEST = 10081.302544202072             # live cy298


@pytest.fixture
def desk(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    (state / "equity-history.jsonl").write_text(
        json.dumps({"ts": "2026-06-17T21:26:56+00:00", "equity": INCEPTION, "cycle": 1}) + "\n"
        + json.dumps({"ts": "2026-06-18T00:11:45+00:00", "equity": 10007.64, "cycle": 2}) + "\n"
        + json.dumps({"ts": "2026-08-18T12:17:39+00:00", "equity": LATEST, "cycle": 298}) + "\n")
    (state / "account.json").write_text(json.dumps(
        {"balance": LATEST, "peak_equity": 10668.832556278849, "halt": False}))
    monkeypatch.setattr(auto_cycle, "ROOT", str(tmp_path))
    return tmp_path


def test_inception_is_the_first_logged_equity(desk):
    assert _inception_equity() == pytest.approx(INCEPTION)


def test_last_logged_is_the_final_entry(desk):
    assert _last_logged() == (298, pytest.approx(LATEST))


def test_the_live_numbers(desk):
    """Re-derived by hand: 10081.302544 - 10011.908080 = +69.394465, /10011.908080 = +0.693118%.
    Drawdown: (10668.832556 - 10081.302544) / 10668.832556 = 5.5065%."""
    line = _pnl_line(LATEST)
    assert "PnL $+69.39 (+0.69%)" in line
    assert "peak $10668.83" in line
    assert "dd 5.51%" in line


def test_a_skip_tick_labels_its_equity_as_last_logged(desk):
    """No live mark without a Binance call — say so rather than implying it is current."""
    assert "@cy298" in _pnl_line()
    assert "@cy" not in _pnl_line(LATEST), "a live gate equity must NOT be labelled stale"


def test_a_losing_desk_reports_a_minus(desk):
    line = _pnl_line(9500.0)
    assert "PnL $-511.91 (-5.11%)" in line


def test_drawdown_is_zero_at_a_new_peak(desk):
    assert "dd 0.00%" in _pnl_line(10668.832556278849)
    assert "dd 0.00%" in _pnl_line(99999.0), "above peak must clamp, never report negative dd"


def test_malformed_lines_are_skipped_not_fatal(desk):
    p = desk / "state" / "equity-history.jsonl"
    p.write_text("not json\n" + json.dumps({"cycle": 1}) + "\n"
                 + json.dumps({"ts": "x", "equity": INCEPTION, "cycle": 1}) + "\n")
    assert _inception_equity() == pytest.approx(INCEPTION)


def test_a_desk_with_no_history_degrades_quietly(tmp_path, monkeypatch):
    """A fresh desk (or one mid-reset) must still print a summary line, never crash the driver."""
    (tmp_path / "state").mkdir()
    monkeypatch.setattr(auto_cycle, "ROOT", str(tmp_path))
    assert _inception_equity() is None
    assert _last_logged() == (None, None)
    assert _pnl_line() == "" and _pnl_line(10000.0) == ""


def test_a_zero_inception_cannot_divide_by_zero(desk):
    (desk / "state" / "equity-history.jsonl").write_text(
        json.dumps({"ts": "t", "equity": 0.0, "cycle": 1}) + "\n")
    assert _pnl_line(100.0) == ""


def test_every_tick_reports_pnl():
    """The ask was 'in every tick' — pin it so a newly added early-return cannot go quiet.

    All eight exits from main() print a book; all eight must print PnL alongside it. The DUE
    summary passes the LIVE gate equity; every hold/skip path takes the last-logged fallback.
    """
    import inspect
    src = inspect.getsource(auto_cycle.main)
    assert src.count("_pnl_line()") == 7, "a hold/skip path prints a book without PnL"
    # The DUE summary uses the LIVE mark — and specifically the POST-guard one, since the guard's
    # second gate pass re-marks the book (see test_reported_equity_post_guard.py).
    assert "_pnl_line(_reported_equity(rep, rep2))" in src
    assert "_pnl_line(rep['equity'])" not in src, "the stale pre-guard equity must not be reported"
    assert src.count("_pnl_line") == src.count("return _exit_code(longs, shorts)")
