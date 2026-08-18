"""After a ban lapses, stay quiet a while before fetching again.

cy299 could not complete across five consecutive fires. Each time the pattern was identical: the
recorded ban lapsed, the very next fire fetched, and Binance re-banned within seconds.

    ban until 18:47:39 -> fire 18:52 -> re-banned to 18:55:42
    ban until 18:55:42 -> fire 19:17 -> re-banned to 19:44:48
    ban until 19:44:48 -> fire 19:47 -> re-banned to 19:52:53

Volume is NOT the cause. Measured per tick: scout is a single `fetch_tickers()` and preflight is
~42 calls over 14 briefs (one fetch_ohlcv + two funding calls each) — roughly 60 requests against a
stated limit of 2400/minute. 29 of the previous 30 cycles completed on the same footprint.

What repeats is the TIMING: the desk fires into an IP Binance has escalated penalties on, at the
instant the penalty expires. `_ban_remaining_ms` returning 0 the millisecond the deadline passes is
what lets that happen. A cooldown makes the desk skip that fire and come back on the next one, by
which point the IP has been quiet for ~30 minutes rather than ~2.

Bounded on purpose: at a 30-minute cadence a 10-minute cooldown costs at most ONE extra held fire,
so it can never wedge the desk. The book is held either way — this only decides when to fetch.
"""
import json

import pytest

from scripts.auto_cycle import _BAN_COOLDOWN_MS, _ban_remaining_ms, _record_ban

DEADLINE = 1787075573575        # the live cy299 ban


@pytest.fixture
def banned(tmp_path):
    _record_ban(tmp_path, DEADLINE)
    return tmp_path


def test_the_cooldown_is_ten_minutes():
    assert _BAN_COOLDOWN_MS == 10 * 60 * 1000


def test_default_behaviour_is_unchanged(banned):
    """No cooldown argument -> the original semantics the ban tests already pin."""
    assert _ban_remaining_ms(banned, now_ms=DEADLINE - 60_000) == 60_000
    assert _ban_remaining_ms(banned, now_ms=DEADLINE) == 0
    assert _ban_remaining_ms(banned, now_ms=DEADLINE + 5) == 0


def test_it_still_holds_just_after_the_ban_lapses(banned):
    """THE REGRESSION. cy299 fired ~2 min after lapse and was re-banned instantly."""
    two_min_after = DEADLINE + 2 * 60_000
    assert _ban_remaining_ms(banned, now_ms=two_min_after) == 0, "raw deadline has passed"
    assert _ban_remaining_ms(banned, now_ms=two_min_after,
                             cooldown_ms=_BAN_COOLDOWN_MS) == 8 * 60_000


def test_it_releases_once_the_cooldown_expires(banned):
    assert _ban_remaining_ms(banned, now_ms=DEADLINE + _BAN_COOLDOWN_MS,
                             cooldown_ms=_BAN_COOLDOWN_MS) == 0
    assert _ban_remaining_ms(banned, now_ms=DEADLINE + _BAN_COOLDOWN_MS + 1,
                             cooldown_ms=_BAN_COOLDOWN_MS) == 0


def test_it_costs_at_most_one_extra_fire(banned):
    """A 30-min cadence must never be wedged: the cooldown is shorter than one interval, so the
    fire AFTER the one that lands in cooldown always proceeds."""
    cadence = 30 * 60_000
    assert _BAN_COOLDOWN_MS < cadence
    worst = DEADLINE + 1                       # a fire landing 1ms after lapse -> held
    assert _ban_remaining_ms(banned, now_ms=worst, cooldown_ms=_BAN_COOLDOWN_MS) > 0
    assert _ban_remaining_ms(banned, now_ms=worst + cadence,
                             cooldown_ms=_BAN_COOLDOWN_MS) == 0


def test_no_ban_file_means_no_cooldown(tmp_path):
    """A desk that was never banned must not be held back."""
    assert _ban_remaining_ms(tmp_path, cooldown_ms=_BAN_COOLDOWN_MS) == 0


def test_a_corrupt_ban_file_does_not_hold_the_desk(tmp_path):
    (tmp_path / "ban.json").write_text("{not json")
    assert _ban_remaining_ms(tmp_path, cooldown_ms=_BAN_COOLDOWN_MS) == 0


def test_a_long_lapsed_ban_does_not_hold(tmp_path):
    """The stale 07:26 deadline sat in ban.json all day; it must not trigger a cooldown now."""
    _record_ban(tmp_path, DEADLINE)
    assert json.load(open(tmp_path / "ban.json"))["banned_until_ms"] == DEADLINE
    assert _ban_remaining_ms(tmp_path, now_ms=DEADLINE + 86_400_000,
                             cooldown_ms=_BAN_COOLDOWN_MS) == 0


def test_the_guard_applies_the_cooldown():
    """Wiring: the hold check in main() must pass the cooldown, or none of this takes effect."""
    import inspect

    from scripts import auto_cycle
    src = inspect.getsource(auto_cycle.main)
    assert "_ban_remaining_ms(_state, cooldown_ms=_BAN_COOLDOWN_MS)" in src
