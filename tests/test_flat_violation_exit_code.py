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


def test_no_path_in_main_can_hardcode_a_success_exit():
    """The invariant, stated totally — every exit from `main` routes through `_exit_code`.

    My first version of this test asserted `src.count("_exit_code(longs, shorts)") == 3` and looked
    for bare `return 0` only in the slice before `cdir =`. It passed while FIVE
    HOLD-ON-DATA-OUTAGE paths further down still returned 0, and cy296 duly held an L3/S0 book
    through a Binance ban and reported success. Counting occurrences pins a number; this pins the
    property, so a newly added early-return cannot slip past it.
    """
    import inspect
    src = inspect.getsource(auto_cycle.main)
    assert "return 0" not in src, (
        "a path in main() hardcodes a success exit instead of reporting the book")
    # Count-agnostic: EVERY return in main must be _exit_code(...). A hardcoded count would just
    # have to be bumped each time a path is added, which teaches nothing; this states the property
    # so a new early-return is checked rather than merely tallied.
    import ast
    import textwrap
    fn = ast.parse(textwrap.dedent(src)).body[0]
    returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
    assert returns, "main() must return explicitly"
    for r in returns:
        assert isinstance(r.value, ast.Call) and getattr(r.value.func, "id", "") == "_exit_code", (
            f"line {r.lineno}: return must route through _exit_code, got {ast.unparse(r.value)}")


def test_every_hold_path_reports_the_book_it_is_holding():
    """A hold that does not name the book cannot have judged it — three of them printed no book
    at all, so their exit code could only ever have been a guess."""
    import inspect
    src = inspect.getsource(auto_cycle.main)
    holds = [b for b in src.split("HOLD-ON-DATA-OUTAGE")[1:]]
    assert len(holds) == 6, f"expected 6 hold paths, found {len(holds)}"
    for i, block in enumerate(holds):
        head = block[:block.index("return _exit_code")]
        assert "vs SHORT" in head or "{book}" in head, (
            f"hold path {i} exits without reporting the book it held")
