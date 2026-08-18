"""A naked sleeve must exit non-zero.

cy295 executed L3/S0 — the mandate's one unbreakable violation — and `main()` returned 0. The only
alarm was the word "VIOLATION" inside a printed summary line, which nothing downstream parses: a
cron, a wrapper, or an operator scanning exit statuses would read that tick as healthy. HARD RULE 8
says be proactively alert, so the violation gets a machine-readable signal.

Exit 2 rather than 1 keeps it distinguishable from a crash/traceback: the book is intact and held,
it is simply one-sided.
"""
import json

from scripts import auto_cycle
from scripts.auto_cycle import _EXIT_FLAT, _book, _exit_code


def test_a_balanced_book_exits_zero():
    assert _exit_code(["LINK", "SOL"], ["XRP", "ETH"]) == 0


def test_an_empty_short_sleeve_exits_non_zero():
    """The exact cy295 shape: L3/S0."""
    assert _exit_code(["LINK", "SOL", "BTW"], []) == _EXIT_FLAT
    assert _EXIT_FLAT != 0


def test_an_empty_long_sleeve_exits_non_zero():
    assert _exit_code([], ["XRP", "ETH", "BTC"]) == _EXIT_FLAT


def test_a_fully_flat_book_exits_non_zero():
    assert _exit_code([], []) == _EXIT_FLAT


def test_flat_is_distinguishable_from_a_crash():
    """1 is reserved for an unhandled failure; a one-sided book is a different condition."""
    assert _EXIT_FLAT == 2


def test_the_live_cy295_positions_would_have_signalled(tmp_path, monkeypatch):
    """Wire it to the real reader: the three surviving longs with no short must exit non-zero."""
    state = tmp_path / "state"
    state.mkdir()
    (state / "positions.json").write_text(json.dumps([
        {"symbol": "LINKUSDT", "direction": "long", "qty": 1.0, "entry": 10.0, "stop": 9.7},
        {"symbol": "SOLUSDT", "direction": "long", "qty": 1.0, "entry": 100.0, "stop": 98.0},
        {"symbol": "BTWUSDT", "direction": "long", "qty": 1.0, "entry": 5.0, "stop": 4.0},
    ]))
    monkeypatch.setattr(auto_cycle, "ROOT", str(tmp_path))
    longs, shorts = _book()
    assert (longs, shorts) == (["LINK", "SOL", "BTW"], [])
    assert _exit_code(longs, shorts) == _EXIT_FLAT


def test_the_signal_does_not_depend_on_which_path_noticed():
    """DUE, SKIP and HOLD-ON-DATA-OUTAGE all report the same book, so they must agree. A naked
    book persists across the ~8 SKIP ticks between 4h candles and across a multi-tick Binance ban;
    signalling only on the DUE tick would go quiet for hours while the violation stands."""
    import inspect
    src = inspect.getsource(auto_cycle.main)
    assert src.count("_exit_code(longs, shorts)") == 3, (
        "every exit that reports a book must route through _exit_code")
    assert "return 0" not in src.split("def main")[-1].split("cdir =")[0], (
        "an early book-reporting path still hardcodes a success exit")
