"""Flat-book -45% halt tripwire: the fast exit sweep's empty-book early-return path must STILL
evaluate the drawdown-from-peak halt and report a real boolean `should_halt` (not None).

Before the fix, `run_exit_sweep` returned early on a flat book WITHOUT calling the monitor, so
`report["should_halt"]` was absent -> run_loops surfaced `null`. That is ambiguous (null reads as
"unknown/error", not "no halt") AND skips a pure equity-only safety check. The drawdown tripwire
(equity vs peak) does NOT depend on open positions, so a flat book at >=45% drawdown must still
HALT (block re-deploying into the hole before the -50% force-flatten). This only ADDS a check on
the empty-book path; it weakens no limit and touches no protected module (fast_loop is unprotected).
"""
from __future__ import annotations

from datetime import UTC, datetime

from futures_fund.config import Settings
from futures_fund.fast_loop import run_exit_sweep
from futures_fund.state import AccountState, is_halted, save_account, save_positions

_SETTINGS = Settings(account_size_usdt=10_000.0, symbols=["ETH/USDT:USDT"], timeframe="15m")


class _NoEx:
    """A flat book returns before any exchange call; this guards that invariant (access fails)."""

    def __getattr__(self, name):  # noqa: ANN001
        raise AssertionError(f"flat-book sweep must not touch the exchange (accessed {name!r})")


def _flat_account(state_dir, *, balance: float, peak: float) -> None:
    save_positions(state_dir, [])
    save_account(state_dir, AccountState(balance=balance, peak_equity=peak))


def test_flat_book_reports_should_halt_false_when_drawdown_under_threshold(tmp_path):
    s, m = tmp_path / "s", tmp_path / "m"
    _flat_account(s, balance=9000.0, peak=10_000.0)  # dd 10% < 45%
    now = datetime(2026, 6, 17, 8, 47, tzinfo=UTC)
    rep = run_exit_sweep(_NoEx(), _SETTINGS, s, m, now, 815)
    assert rep["closed"] == 0
    assert rep["carried"] == 0
    assert rep["should_halt"] is False, "flat book must report a real bool, never None"
    assert is_halted(s) is False


def test_flat_book_trips_halt_when_drawdown_at_or_past_threshold(tmp_path):
    s, m = tmp_path / "s", tmp_path / "m"
    _flat_account(s, balance=5000.0, peak=10_000.0)  # dd 50% >= 45% pre-flatten tripwire
    now = datetime(2026, 6, 17, 8, 47, tzinfo=UTC)
    rep = run_exit_sweep(_NoEx(), _SETTINGS, s, m, now, 815)
    assert rep["should_halt"] is True, "flat book at >=45% drawdown must signal HALT"
    assert is_halted(s) is True, "the halt flag must be persisted so new opens are blocked"
