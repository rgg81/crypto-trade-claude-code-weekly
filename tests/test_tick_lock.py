"""HARD RULE 8 was not being kept: auto_cycle ran the whole tick OUTSIDE the run lock.

`scripts/run_loops.py` takes the single-flight lock, decides whether the candle is due, and then
EXITS — releasing the lock — before auto_cycle runs scout, preflight, the book, reclassify and both
gate passes. Nothing re-checks. And a candle is only marked served AFTER the gate executes
(`gate_execute_cli.py` stamps `report["candle"]` last), so for the whole multi-minute tick the
candle still reads as due.

Two runs that overlap therefore BOTH execute: a manual status run started a few minutes before a
cron fire, or a tick that outlasts the 30-minute cadence (60s warm timeouts x ~100 symbols against a
stalled proxy). auto_cycle's own docstring promised the lock made that safe. It did not.

The lock now spans the tick: acquired after the due-check, due re-checked once held, released on
every exit path, its lease kept fresh by a heartbeat that is itself capped so a hung tick cannot
wedge the desk forever.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from futures_fund.runlock import LOCK_NAME, try_acquire

_spec = importlib.util.spec_from_file_location(
    "auto_cycle", Path(__file__).resolve().parents[1] / "scripts" / "auto_cycle.py")
ac = importlib.util.module_from_spec(_spec)
sys.modules["auto_cycle"] = ac
_spec.loader.exec_module(ac)

CYCLE = 7
_GATE = {"cycle": CYCLE, "opened": 0, "closed": 0, "reduced": 0, "equity": 10_000.0,
         "halted": False, "exposure": {"net": 0.0, "tilt": 0.0, "n_long": 1, "n_short": 1,
                                       "gross_long": 100.0, "gross_short": 100.0}}


def _lock_is_ours(state: Path) -> bool:
    p = state / LOCK_NAME
    try:
        return json.loads(p.read_text()).get("pid") == os.getpid()
    except (OSError, ValueError):
        return False


@pytest.fixture
def desk(tmp_path, monkeypatch):
    """A DUE tick whose every stage is faked, recording whether WE held the lock at that stage."""
    state = tmp_path / "state"
    cdir = state / "cycle" / str(CYCLE)
    cdir.mkdir(parents=True)
    (state / "positions.json").write_text(json.dumps([
        {"symbol": "AAAUSDT", "direction": "long", "qty": 1.0, "entry": 100.0},
        {"symbol": "BBBUSDT", "direction": "short", "qty": 1.0, "entry": 100.0}]))
    seen: dict[str, bool] = {}
    fail_at = {"stage": None}

    def fake_run(args, **kw):
        stage = Path(args[0]).stem
        if stage != "run_loops":
            seen[stage] = _lock_is_ours(state)
        if stage == fail_at["stage"]:
            raise RuntimeError(f"{stage} crashed")
        out = ""
        if stage == "run_loops":
            out = json.dumps({"strategic": {"due": True, "cycle": CYCLE}})
        elif stage == "scout_cli":
            (cdir / "universe.json").write_text(
                json.dumps({"universe": [{"symbol": "AAA/USDT:USDT"}]}))
        elif stage == "preflight" and fail_at["stage"] != "no_context":
            (cdir / "context.json").write_text("{}")
        elif stage == "xsection_book_cli":
            (cdir / "proposals.json").write_text("{}")
        elif stage == "gate_execute_cli":
            out = json.dumps(_GATE, indent=2)
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")

    monkeypatch.setattr(ac, "ROOT", str(tmp_path))
    monkeypatch.setattr(ac, "run", fake_run)
    monkeypatch.setattr(ac, "_warm_klines", lambda *a, **k: (1, [], None))
    monkeypatch.setattr(ac, "load_settings",
                        lambda: SimpleNamespace(exchange=SimpleNamespace(klines_proxy_url="")))
    monkeypatch.setattr(ac, "_check_monthly_review", lambda: False)
    # raising=False so these tests can be run against a build without the re-check (the pre-fix
    # tick) and fail on the LOCK assertion itself; test_still_due_requires_the_SAME_cycle pins it.
    monkeypatch.setattr(ac, "_still_due", lambda state_dir, cycle: True, raising=False)
    return SimpleNamespace(state=state, seen=seen, fail_at=fail_at)


# ---- the lock spans the tick ----------------------------------------------------------------

def test_the_lock_is_held_from_scout_through_the_gate(desk, capsys):
    assert ac.main() == 0
    for stage in ("scout_cli", "preflight", "xsection_book_cli", "reclassify_cli",
                  "gate_execute_cli"):
        assert desk.seen.get(stage) is True, f"{stage} ran without this tick holding the run lock"
    assert "SUMMARY cycle 7" in capsys.readouterr().out
    assert not (desk.state / LOCK_NAME).exists(), "released once the tick is done"


def test_a_tick_already_running_makes_the_second_one_stand_down(desk, capsys):
    """The overlap that double-executed: run_loops said due, but a live tick holds the lock."""
    lock = desk.state / LOCK_NAME
    lock.write_text(json.dumps({"pid": 999999999, "owner": "auto_cycle",
                                "start_ts": datetime.now(UTC).isoformat()}))
    before = lock.read_text()

    assert ac.main() == 0, "a balanced book held by another tick is not a failure"

    out = capsys.readouterr().out
    assert "HOLD-LOCKED" in out
    assert desk.seen == {}, "no stage may run while another tick is the writer"
    assert lock.read_text() == before, "the other tick's lock must be left exactly as it was"


def test_a_candle_served_while_waiting_for_the_lock_is_not_run_twice(desk, monkeypatch, capsys):
    monkeypatch.setattr(ac, "_still_due", lambda state_dir, cycle: False)
    ac.main()
    assert "scout_cli" not in desk.seen and "gate_execute_cli" not in desk.seen
    assert "SKIP" in capsys.readouterr().out
    assert not (desk.state / LOCK_NAME).exists()


def test_the_lock_is_released_on_a_hold_path(desk, capsys):
    desk.fail_at["stage"] = "no_context"          # preflight produces no context -> HOLD
    ac.main()
    assert "HOLD-ON-DATA-OUTAGE" in capsys.readouterr().out
    assert not (desk.state / LOCK_NAME).exists()


def test_the_lock_is_released_when_a_stage_crashes(desk):
    desk.fail_at["stage"] = "preflight"
    with pytest.raises(RuntimeError):
        ac.main()
    assert not (desk.state / LOCK_NAME).exists(), "a crash must not leave the desk locked"


def test_still_due_requires_the_SAME_cycle(tmp_path, monkeypatch):
    """If the lock wait crossed a candle boundary, the pipeline prepared for cycle 7 must not run
    against candle 8's due status — the next fire serves candle 8 from scratch."""
    import futures_fund.scheduling as sch
    monkeypatch.setattr(sch, "cycle_due", lambda *a, **k: ("FRESH", 8, ""))
    assert ac._still_due(str(tmp_path), 7) is False
    assert ac._still_due(str(tmp_path), 8) is True
    monkeypatch.setattr(sch, "cycle_due", lambda *a, **k: ("SKIP", 8, ""))
    assert ac._still_due(str(tmp_path), 8) is False


# ---- the heartbeat --------------------------------------------------------------------------

def test_a_beat_refreshes_our_lease_within_the_cap(tmp_path):
    start = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    assert try_acquire(tmp_path, start, owner="auto_cycle")[0] is True
    assert ac._beat(tmp_path, start, start + timedelta(minutes=25), max_lease_s=7200) is True
    assert try_acquire(tmp_path, start + timedelta(minutes=45))[0] is False


def test_a_beat_past_the_cap_stops_refreshing(tmp_path):
    """A hung tick must eventually become reclaimable, or one stuck process wedges the desk."""
    start = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    assert try_acquire(tmp_path, start, owner="auto_cycle")[0] is True
    assert ac._beat(tmp_path, start, start + timedelta(hours=2, minutes=1),
                    max_lease_s=7200) is False
    assert try_acquire(tmp_path, start + timedelta(hours=2, minutes=2))[0] is True


def test_the_heartbeat_thread_stops_when_the_lock_is_released(tmp_path):
    handle = ac._hold_tick_lock(str(tmp_path), beat_every_s=0.01)
    assert handle is not None and _lock_is_ours(tmp_path)
    time.sleep(0.05)
    handle.release()
    assert not handle.thread.is_alive()
    assert not (tmp_path / LOCK_NAME).exists()


def test_a_tick_whose_lease_was_reclaimed_does_not_delete_the_new_holders_lock(tmp_path):
    """Past the lease cap a hung tick's lock is legitimately reclaimed. When the hung tick finally
    finishes, its release must not delete the NEW holder's lock and let a third tick in."""
    handle = ac._hold_tick_lock(str(tmp_path), beat_every_s=3600)
    (tmp_path / LOCK_NAME).write_text(json.dumps(
        {"pid": 999999999, "owner": "auto_cycle", "start_ts": datetime.now(UTC).isoformat()}))
    handle.release()
    assert (tmp_path / LOCK_NAME).exists()
    assert json.loads((tmp_path / LOCK_NAME).read_text())["pid"] == 999999999


def test_hold_tick_lock_returns_none_when_another_tick_holds_it(tmp_path):
    (tmp_path / LOCK_NAME).write_text(json.dumps(
        {"pid": 999999999, "owner": "auto_cycle", "start_ts": datetime.now(UTC).isoformat()}))
    assert ac._hold_tick_lock(str(tmp_path), beat_every_s=0.01) is None
