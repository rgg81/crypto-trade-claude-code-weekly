"""Repeated bans must back off exponentially, not retry at a fixed cadence.

cy301 was blocked three fires running, and the ban got LONGER each time:

    banned to 02:17:59 -> fire 02:47 (29m of silence) -> banned to 02:54:59  (~8m)
    banned to 02:54:59 -> fire 03:17 (22m of silence) -> banned to 03:51:43  (~34m)

Two things this data settles. First, it refutes the fixed cooldown's premise: 29 minutes of
complete silence still drew a 418 on the very first `fetch_tickers`, so "we fire too soon after
expiry" was the wrong diagnosis. Second, Binance escalates repeat -1003 offenders — every fire that
draws a 418 is itself another violation, so a fixed 30-minute retry actively feeds the escalation
it is trying to wait out.

The desk's own volume is not the trigger (~60 requests per tick against a 2400/min limit, and 29 of
30 prior cycles completed on it), so the only lever available is HOW OFTEN we knock. Back off
exponentially per consecutive ban and the escalation runs out of fuel.

Safety: backing off never touches the book. Positions stay open and dollar-neutral the whole time —
the desk simply stops rotating until the IP clears, which costs edge, not safety. The cap keeps
that bounded to roughly one 4h candle.
"""
from scripts.auto_cycle import (
    _BAN_BACKOFF_CAP_MS,
    _BAN_COOLDOWN_MS,
    _ban_cooldown_ms,
    _ban_remaining_ms,
    _clear_ban,
    _consecutive_bans,
    _record_ban,
)

D = 1787104303005            # the live cy301 deadline, 03:51:43


def test_the_first_ban_waits_the_base_cooldown():
    """A one-off ban must behave exactly as before — no new penalty for a transient blip."""
    assert _ban_cooldown_ms(0) == _BAN_COOLDOWN_MS
    assert _ban_cooldown_ms(1) == _BAN_COOLDOWN_MS


def test_it_doubles_per_consecutive_ban():
    assert _ban_cooldown_ms(2) == 2 * _BAN_COOLDOWN_MS
    assert _ban_cooldown_ms(3) == 4 * _BAN_COOLDOWN_MS
    assert _ban_cooldown_ms(4) == 8 * _BAN_COOLDOWN_MS


def test_it_is_capped_so_the_desk_cannot_stall_indefinitely():
    """Bounded to ~one 4h candle: the book is held, not abandoned, but it must resume on its own."""
    assert _BAN_BACKOFF_CAP_MS == 4 * 60 * 60 * 1000
    assert _ban_cooldown_ms(99) == _BAN_BACKOFF_CAP_MS
    assert all(_ban_cooldown_ms(n) <= _BAN_BACKOFF_CAP_MS for n in range(0, 200))


def test_it_never_goes_backwards():
    seq = [_ban_cooldown_ms(n) for n in range(0, 20)]
    assert seq == sorted(seq)


def test_recording_a_ban_counts_it(tmp_path):
    assert _consecutive_bans(tmp_path) == 0
    _record_ban(tmp_path, D)
    assert _consecutive_bans(tmp_path) == 1
    _record_ban(tmp_path, D + 1000)
    assert _consecutive_bans(tmp_path) == 2


def test_a_successful_fetch_clears_the_streak(tmp_path):
    """Without this the desk would back off forever after one bad night."""
    _record_ban(tmp_path, D)
    _record_ban(tmp_path, D)
    assert _consecutive_bans(tmp_path) == 2
    _clear_ban(tmp_path)
    assert _consecutive_bans(tmp_path) == 0
    assert _ban_remaining_ms(tmp_path, now_ms=D - 1000) == 0, "a cleared ban must not still hold"


def test_the_live_cy301_streak_backs_off_to_forty_minutes(tmp_path):
    """Three consecutive bans -> 4x base = 40 min past the deadline, instead of retrying at 30."""
    for _ in range(3):
        _record_ban(tmp_path, D)
    assert _ban_cooldown_ms(_consecutive_bans(tmp_path)) == 40 * 60 * 1000
    held = _ban_remaining_ms(tmp_path, now_ms=D + 30 * 60_000,
                             cooldown_ms=_ban_cooldown_ms(_consecutive_bans(tmp_path)))
    assert held == 10 * 60_000, "a 30-min-after-lapse fire must still be held"


def test_a_corrupt_counter_degrades_to_the_base_cooldown(tmp_path):
    (tmp_path / "ban.json").write_text('{"banned_until_ms": 1, "consecutive": "lots"}')
    assert _consecutive_bans(tmp_path) == 0
    assert _ban_cooldown_ms(_consecutive_bans(tmp_path)) == _BAN_COOLDOWN_MS


def test_clearing_a_desk_that_was_never_banned_is_safe(tmp_path):
    _clear_ban(tmp_path)
    assert _consecutive_bans(tmp_path) == 0


def test_the_guard_uses_the_escalating_backoff():
    """Wiring, and the scout-success reset — without both, none of this takes effect."""
    import inspect

    from scripts import auto_cycle
    src = inspect.getsource(auto_cycle.main)
    assert "_ban_cooldown_ms(_consecutive_bans(_state))" in src
    assert "_clear_ban(_state)" in src, "a successful scout must reset the streak"
