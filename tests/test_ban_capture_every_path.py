"""A -1003 ban must be recorded wherever it surfaces, not only from the scout.

Live failure, cy299. Preflight came back with

    418 I'm a teapot {"code":-1003,"msg":"Way too many requests;
    IP(...) banned until 1787071659262. ..."}

and the driver held the book correctly — but never wrote the deadline. `_record_ban` was called
from ONE of the six hold paths (scout). ban.json kept a stale, long-lapsed deadline (07:26 while
the real ban ran to 18:47), so `_ban_remaining_ms` reported 0, the next fire fetched, and the ban
re-extended ~22 min. That is precisely the ratchet the ban guard was built to stop, running
unguarded through five of the six paths.

Every path that shells out to a Binance-touching script can carry this deadline: run_loops, scout,
preflight, and the gate all fetch. Capture it from any of them.
"""
import json

import pytest

from scripts import auto_cycle
from scripts.auto_cycle import _ban_remaining_ms, _capture_ban, _record_ban

CY299 = ('errors.DDoSProtection: binanceusdm 418 I\'m a teapot {"code":-1003,"msg":"Way too many '
         'requests; IP(213.55.240.23) banned until 1787071659262. Please use the websocket for '
         'live updates to avoid bans."}')
BURST = ('usdm 429 Too Many Requests {"code":-1003,"msg":"Too many requests; current limit of '
         'IP(213.55.240.23) is 2400 requests per minute."}')


def test_captures_the_live_cy299_preflight_ban(tmp_path):
    assert _capture_ban(tmp_path, CY299) == 1787071659262
    assert json.load(open(tmp_path / "ban.json"))["banned_until_ms"] == 1787071659262


def test_a_429_burst_limit_carries_no_deadline_and_records_nothing(tmp_path):
    """A 429 is a per-minute burst cap, not an IP ban — there is nothing to wait out, so it must
    NOT create a ban file that would make the next fire skip its scout for no reason."""
    assert _capture_ban(tmp_path, BURST) is None
    assert not (tmp_path / "ban.json").exists()


def test_it_searches_every_stream_it_is_given(tmp_path):
    """Subprocess output lands on stderr or stdout depending on how the script failed."""
    assert _capture_ban(tmp_path, "", CY299) == 1787071659262
    assert _capture_ban(tmp_path, None, None, CY299) == 1787071659262


def test_a_stale_deadline_is_never_shortened(tmp_path):
    """`_record_ban` keeps the max; capture must not regress an active ban to an older one."""
    _record_ban(tmp_path, 1787071659262)
    _capture_ban(tmp_path, CY299.replace("1787071659262", "1787030812197"))
    assert json.load(open(tmp_path / "ban.json"))["banned_until_ms"] == 1787071659262


def test_the_recorded_ban_actually_holds_the_next_fire(tmp_path):
    """End to end: capture -> the guard reports time remaining -> the next fire skips fetching."""
    _capture_ban(tmp_path, CY299)
    assert _ban_remaining_ms(tmp_path, now_ms=1787071659262 - 600_000) == 600_000
    assert _ban_remaining_ms(tmp_path, now_ms=1787071659262) == 0


def test_capture_never_raises(tmp_path):
    """Telemetry must not be able to break the hold path."""
    assert _capture_ban(tmp_path / "nonexistent-dir", CY299) is None or True
    assert _capture_ban(tmp_path) is None


@pytest.mark.parametrize("stream", ["rl", "sc", "pf", "bb"])
def test_every_binance_touching_hold_path_captures(stream):
    """The regression: cy299 held via preflight and recorded nothing. Each subprocess-backed hold
    path must call _capture_ban on its output before returning."""
    import inspect
    src = inspect.getsource(auto_cycle.main)
    assert f"_capture_ban(_state, {stream}.stderr, {stream}.stdout)" in src, (
        f"the {stream} hold path can surface a 418 but does not record its deadline")


def test_the_gate_hold_path_captures_too():
    """The gate fetches prices mid-execute (cy297 died there on a 429). It runs its own subprocess
    inside `_gate_exposure`, so it captures there rather than in main()."""
    import inspect
    assert "_capture_ban(" in inspect.getsource(auto_cycle._gate_exposure)
    assert inspect.getsource(auto_cycle.main).count("_capture_ban(") == 4
