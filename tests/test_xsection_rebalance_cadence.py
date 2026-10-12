"""The factor book rebalances DAILY — a day of candles after the last rebalance it COMPLETED.

It used to rebalance when `cycle % 6 == 0`. Cycle numbers count SERVED ticks, not candles, so every
unserved candle (machine asleep, session down, Binance ban) pushed the schedule one candle later.
Live, 2026-10-07 -> 10-12: cy576 rebalanced at 00:00Z Oct 7, the desk then went 29 candles unserved,
and the catch-up tick cy579 (08:00Z Oct 12) HELD the book because 579 % 6 != 0. The `cycle % 6` rule
would have kept a 25-day momentum ranking unrefreshed until cy582: ~5.7 days, against a mandate
of daily.

The fix keys the cadence on CANDLE TIME since the last completed rebalance. Under uninterrupted
service it reproduces the old schedule exactly. It never rebalances more than once a day, and after
an outage it rebalances on the first tick back.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "xsection_book_cli", Path(__file__).resolve().parents[1] / "scripts" / "xsection_book_cli.py")
xb = importlib.util.module_from_spec(_spec)
sys.modules["xsection_book_cli"] = xb
_spec.loader.exec_module(xb)

H4 = timedelta(hours=4)
T0 = datetime(2026, 10, 7, 0, 0, tzinfo=UTC)          # the live cy576 rebalance candle


def _cycle(state: Path, n: int, candle: datetime, *, rebalanced: bool | None = None,
           completed: bool = True) -> None:
    """A cycle folder as the desk leaves it: report.json once the gate ran, plus (new-era cycles)
    the planner's marker saying whether this cycle rebalanced."""
    d = state / "cycle" / str(n)
    d.mkdir(parents=True, exist_ok=True)
    if completed:
        (d / "report.json").write_text(json.dumps(
            {"cycle": n, "candle": candle.isoformat(),
             "ran_at": (candle + timedelta(minutes=21)).isoformat()}))
    if rebalanced is not None:
        (d / xb.PLAN_FILE).write_text(json.dumps({"cycle": n, "rebalancing": rebalanced}))


def _legacy(state: Path, first: int, last: int, first_candle: datetime) -> None:
    """Pre-fix history: every candle served, no markers (the `cycle % 6` era)."""
    for i, n in enumerate(range(first, last + 1)):
        _cycle(state, n, first_candle + i * H4)


def test_a_long_outage_rebalances_on_the_first_tick_back(tmp_path):
    """The live case. cy570..578 served (cy576 rebalanced @ 00:00Z Oct 7), then 29 candles unserved.
    The catch-up tick must rebalance; `579 % 6 != 0` held the book instead."""
    _legacy(tmp_path, 570, 578, T0 - 6 * H4)
    now = datetime(2026, 10, 12, 8, 20, tzinfo=UTC)
    due, why = xb.rebalance_due(tmp_path, 579, now, every=6)
    assert due, why
    assert "576" in why
    assert 579 % 6 != 0, "the old rule would have held this tick"


def test_uninterrupted_service_reproduces_the_old_daily_schedule(tmp_path):
    """Every candle served -> exactly the `cycle % 6` cadence: nothing changes in normal running."""
    _legacy(tmp_path, 570, 576, T0 - 6 * H4)
    for k, n in enumerate(range(577, 583), start=1):
        due, why = xb.rebalance_due(tmp_path, n, T0 + k * H4 + timedelta(minutes=20), every=6)
        assert due == (n % 6 == 0), (n, why)
        _cycle(tmp_path, n, T0 + k * H4, rebalanced=due)


def test_never_rebalances_twice_within_a_day(tmp_path):
    """After a catch-up rebalance shifts the phase, a cycle NUMBER divisible by 6 must not trigger a
    second rebalance 8h later. Rebalances never become more frequent than daily."""
    c = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
    _legacy(tmp_path, 574, 579, T0 - 2 * H4)
    _cycle(tmp_path, 580, c, rebalanced=True)
    _cycle(tmp_path, 581, c + H4, rebalanced=False)
    due, why = xb.rebalance_due(tmp_path, 582, c + 2 * H4 + timedelta(minutes=20), every=6)
    assert not due, why


def test_the_shifted_phase_then_holds_daily(tmp_path):
    c = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
    _cycle(tmp_path, 580, c, rebalanced=True)
    for k, n in enumerate(range(581, 587), start=1):
        due, why = xb.rebalance_due(tmp_path, n, c + k * H4 + timedelta(minutes=20), every=6)
        assert due == (n == 586), (n, why)
        _cycle(tmp_path, n, c + k * H4, rebalanced=due)


def test_a_marker_saying_no_overrides_the_legacy_modulo(tmp_path):
    """A new-era cycle whose number is divisible by 6 but which did NOT rebalance must not be read
    as a rebalance — that would push the next one 8h late."""
    c = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
    _cycle(tmp_path, 580, c, rebalanced=True)
    _cycle(tmp_path, 581, c + H4, rebalanced=False)
    _cycle(tmp_path, 582, c + 2 * H4, rebalanced=False)
    for k, n in enumerate(range(583, 586), start=3):
        _cycle(tmp_path, n, c + k * H4, rebalanced=False)
    due, why = xb.rebalance_due(tmp_path, 586, c + 6 * H4 + timedelta(minutes=20), every=6)
    assert due, why
    assert "580" in why


def test_a_rebalance_that_never_completed_does_not_count(tmp_path):
    """The planner marked cy580 a rebalance but the gate never wrote its report (crash/outage).
    That rebalance did not happen, so the next tick must still be due."""
    _legacy(tmp_path, 570, 579, T0 - 6 * H4)
    _cycle(tmp_path, 580, datetime(2026, 10, 12, 12, 0, tzinfo=UTC), rebalanced=True,
           completed=False)
    due, why = xb.rebalance_due(tmp_path, 581, datetime(2026, 10, 12, 16, 20, tzinfo=UTC), every=6)
    assert due, why
    assert "576" in why


def test_the_planners_own_cycle_folder_is_ignored(tmp_path):
    """A RETRY re-runs the SAME cycle number over a crashed folder that may already carry a marker.
    Only EARLIER cycles say when the last rebalance happened."""
    _legacy(tmp_path, 570, 578, T0 - 6 * H4)
    _cycle(tmp_path, 579, datetime(2026, 10, 12, 8, 0, tzinfo=UTC), rebalanced=True)
    due, why = xb.rebalance_due(tmp_path, 579, datetime(2026, 10, 12, 8, 20, tzinfo=UTC), every=6)
    assert due, why
    assert "576" in why


def test_the_window_counts_candles_not_wall_clock(tmp_path):
    _legacy(tmp_path, 570, 576, T0 - 6 * H4)
    due, why = xb.rebalance_due(tmp_path, 577, T0 + timedelta(hours=23, minutes=59), every=6)
    assert not due, why
    due, why = xb.rebalance_due(tmp_path, 577, T0 + timedelta(hours=24, minutes=1), every=6)
    assert due, why


def test_an_unreadable_report_is_skipped_not_fatal(tmp_path):
    _legacy(tmp_path, 564, 576, T0 - 12 * H4)
    (tmp_path / "cycle" / "576" / "report.json").write_text("{not json")
    due, why = xb.rebalance_due(tmp_path, 577, T0 + H4 + timedelta(minutes=20), every=6)
    assert due, why                       # falls back to cy570, which was 7 candles ago
    assert "570" in why


def test_a_corrupt_marker_falls_back_to_the_legacy_rule(tmp_path):
    _legacy(tmp_path, 570, 576, T0 - 6 * H4)
    (tmp_path / "cycle" / "576" / xb.PLAN_FILE).write_text("garbage")
    due, why = xb.rebalance_due(tmp_path, 577, T0 + H4 + timedelta(minutes=20), every=6)
    assert not due, why
    assert "576" in why


@pytest.mark.parametrize("cycle", [6, 7, 12, 13])
def test_with_no_history_it_keeps_the_legacy_modulo(tmp_path, cycle):
    due, _ = xb.rebalance_due(tmp_path, cycle, T0, every=6)
    assert due == (cycle % 6 == 0)


def test_a_served_candle_from_the_future_never_makes_it_due(tmp_path):
    """Clock skew: a last-rebalance candle AFTER now gives a negative window — hold, don't churn."""
    _cycle(tmp_path, 576, T0 + 10 * H4, rebalanced=True)
    due, why = xb.rebalance_due(tmp_path, 577, T0 + timedelta(minutes=20), every=6)
    assert not due, why


# --- behavioural: the real planner, klines stubbed ---------------------------------------------

_SYMS = [f"S{i}USDT" for i in range(10)]


def _rows(i: int, n: int = 300) -> list[list]:
    """Distinct trends per symbol (S0 strongest down ... S9 strongest up) plus a little noise."""
    import math
    drift = (i - 4.5) * 0.002
    out, c = [], 100.0
    for t in range(n):
        c *= 1 + drift + 0.03 * math.sin(t * (0.7 + 0.13 * i))   # vol above VOL_FLOOR
        out.append([t * 14_400_000, c, c * 1.01, c * 0.99, c, 0, 0, 0])
    return out


def _run_planner(tmp_path, monkeypatch, cycle: int, now: datetime) -> dict:
    cdir = tmp_path / "cycle" / str(cycle)
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "universe.json").write_text(json.dumps({"universe": [{"symbol": s} for s in _SYMS]}))
    (tmp_path / "positions.json").write_text("[]")
    monkeypatch.setattr(xb, "_klines", lambda sym, *a, **k: _rows(_SYMS.index(sym)))
    monkeypatch.setattr(sys, "argv", ["xsection_book_cli.py", "--cycle", str(cycle),
                                      "--state", str(tmp_path), "--now", now.isoformat()])
    xb.main()
    return json.loads((cdir / xb.PLAN_FILE).read_text())


def test_the_planner_rebalances_the_catch_up_tick(tmp_path, monkeypatch, capsys):
    _legacy(tmp_path, 570, 578, T0 - 6 * H4)
    marker = _run_planner(tmp_path, monkeypatch, 579, datetime(2026, 10, 12, 8, 20, tzinfo=UTC))
    out = json.loads(capsys.readouterr().out)
    assert out["rebalancing"] is True and marker["rebalancing"] is True
    assert "576" in out["rebalance_reason"]
    props = json.loads((tmp_path / "cycle" / "579" / "proposals.json").read_text())["proposals"]
    assert {p["direction"] for p in props} == {"long", "short"}, "a rebalance must build the book"


def test_the_planner_holds_inside_the_day_and_says_so(tmp_path, monkeypatch, capsys):
    _legacy(tmp_path, 570, 576, T0 - 6 * H4)
    marker = _run_planner(tmp_path, monkeypatch, 577, T0 + H4 + timedelta(minutes=20))
    out = json.loads(capsys.readouterr().out)
    assert out["rebalancing"] is False and marker["rebalancing"] is False
    props = json.loads((tmp_path / "cycle" / "577" / "proposals.json").read_text())
    assert props["proposals"] == []
