"""Ban-aware guard for the auto-cycle driver.

A Binance -1003 IP ban carries a `banned until <epoch_ms>` deadline. Every REST fetch made while
the ban is active RE-EXTENDS it ~22 min, so a driver that keeps calling scout during the ban never
lets it lapse (the ban ratchets ahead of real time — observed cy190). The fix: parse + persist the
ban deadline, and on the next fire HOLD *before any Binance call* until the deadline passes, so the
ban can actually expire and the cadence self-heals.
"""
import json

from scripts.auto_cycle import (
    _ban_remaining_ms,
    _parse_ban_until_ms,
    _record_ban,
)

_MSG = ('418 Unknown {"code":-1003,"msg":"Way too many requests; '
        'IP(213.55.240.23) banned until 1784857316143. '
        'Please use the websocket for live updates to avoid bans."}')


def test_parse_ban_until_ms_extracts_deadline():
    assert _parse_ban_until_ms(_MSG) == 1784857316143


def test_parse_ban_until_ms_none_when_absent():
    assert _parse_ban_until_ms("some unrelated network error") is None
    assert _parse_ban_until_ms("") is None


def test_record_and_remaining_ban(tmp_path):
    state = tmp_path
    _record_ban(state, 1784857316143)
    # 60s before the deadline -> still ~60000 ms of ban remaining
    assert _ban_remaining_ms(state, now_ms=1784857316143 - 60_000) == 60_000
    # exactly at / past the deadline -> 0 (lapsed, safe to fetch again)
    assert _ban_remaining_ms(state, now_ms=1784857316143) == 0
    assert _ban_remaining_ms(state, now_ms=1784857316143 + 5) == 0


def test_remaining_zero_when_no_ban_recorded(tmp_path):
    assert _ban_remaining_ms(tmp_path, now_ms=1784857316143) == 0


def test_record_ban_persists_max_deadline(tmp_path):
    # a later ban extends the stored deadline; an earlier one never shortens it
    _record_ban(tmp_path, 1784857316143)
    _record_ban(tmp_path, 1784857000000)  # earlier -> ignored
    stored = json.loads((tmp_path / "ban.json").read_text())
    assert stored["banned_until_ms"] == 1784857316143
