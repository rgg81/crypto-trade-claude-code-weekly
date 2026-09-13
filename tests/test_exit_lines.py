"""The tick output never said which legs the exit audit closed, or when.

cy443 closed UAIUSDT (stop) and LSKUSDT (take-profit) in preflight. The console showed only
`hold 37` and a book two longs shorter; finding the closes, their PnL, and that UAI had really
crossed its stop ~20h earlier took a forensic pass through the journal and the raw candles.

Now that the strategic audit replays candles no tick saw, a close can land on an EARLIER candle
than the one being served. That must be printed, not reconstructed.
"""
from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "auto_cycle", Path(__file__).resolve().parents[1] / "scripts" / "auto_cycle.py")
ac = importlib.util.module_from_spec(_spec)
sys.modules["auto_cycle"] = ac
_spec.loader.exec_module(ac)

_CY443 = {"audit": {"closed": 2, "carried": 0, "alerts": [], "closes": [
    {"close": "UAIUSDT", "reason": "stop", "pnl": -23.972,
     "bar_close": "2026-09-12T16:00:00+00:00"},
    {"close": "LSKUSDT", "reason": "take_profit", "pnl": 132.53}]}}


def test_it_names_each_close_with_its_reason_and_pnl():
    out = ac.exit_lines(_CY443)
    assert "UAI stop $-23.97" in out
    assert "LSK take_profit $+132.53" in out


def test_a_close_booked_on_an_earlier_candle_says_when_it_really_closed():
    out = ac.exit_lines(_CY443)
    assert "UAI stop $-23.97 @09-12 16:00Z" in out
    assert "LSK take_profit $+132.53 @" not in out, "a close on the served candle carries no stamp"


def test_it_surfaces_a_backfill_alert():
    ctx = {"audit": {"closed": 0, "carried": 3, "closes": [],
                     "alerts": ["gap-backfill skipped after internal error"]}}
    out = ac.exit_lines(ctx)
    assert "ALERT" in out and "gap-backfill skipped" in out


def test_it_is_silent_when_nothing_closed_and_nothing_is_wrong():
    assert ac.exit_lines({"audit": {"closed": 0, "carried": 39, "closes": [], "alerts": []}}) == ""


def test_a_legacy_or_broken_context_never_breaks_the_tick():
    for ctx in ({}, {"audit": None}, {"audit": {"closed": 1, "carried": 0}}, None):
        assert isinstance(ac.exit_lines(ctx), str)


def test_main_prints_the_exits_after_preflight():
    src = inspect.getsource(ac.main)
    i_pf, i_ex = src.find('run(["scripts/preflight.py"'), src.find("exit_lines(")
    assert i_ex > i_pf > 0, "the exits must be printed once preflight has produced them"
