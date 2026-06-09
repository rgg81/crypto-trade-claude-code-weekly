"""Fast-loop exit sweep: closes held positions whose latest 15m candle hit stop/TP/liq, with
correct accounting; carries the rest; writes a fast-cycle report the due-gate can read."""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pandas as pd

from futures_fund.config import Settings
from futures_fund.fast_loop import run_exit_sweep
from futures_fund.market_data import FundingInfo
from futures_fund.models import MmrBracket, SymbolSpec
from futures_fund.state import Position, load_account, load_positions, save_positions

_R2U = {"ETHUSDT": "ETH/USDT:USDT"}


def _frame(lows, highs, closes):
    n = len(closes)
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-06-08", periods=n, freq="15min", tz="UTC"),
        "open": closes, "high": highs, "low": lows, "close": closes, "volume": 1.0})


class _FakeEx:
    def __init__(self, frames):
        self.frames = frames

    def unified_for_raw(self, raw):
        return _R2U.get(raw)

    def ohlcv(self, sym, tf="15m", limit=500):
        return self.frames[sym]

    def funding(self, sym):
        last = float(self.frames[sym]["close"].iloc[-1])
        return FundingInfo(symbol=sym, current_rate=0.0,
                           next_funding_ts=datetime(2026, 1, 1, tzinfo=UTC), interval_hours=8.0,
                           mark_price=last, index_price=last)

    def symbol_spec(self, sym):
        raw = {"ETH/USDT:USDT": "ETHUSDT"}.get(sym, "BTCUSDT")
        return SymbolSpec(symbol=raw, tick_size=0.01, step_size=0.001, min_notional=5.0,
                          mmr_brackets=[MmrBracket(notional_floor=0, notional_cap=1_000_000,
                                                   mmr=0.004, maint_amount=0.0, max_leverage=125)])


def _held_eth(state_dir):
    save_positions(state_dir, [Position(
        symbol="ETHUSDT", direction="long", qty=1.0, entry=100.0, stop=95.0, take_profits=[130.0],
        leverage=3.0, margin=33.3, liq_price=70.0, opened_cycle=1,
        opened_ts=datetime(2026, 6, 8, tzinfo=UTC))])


def test_stop_hit_closes_position(tmp_path):
    s, m = tmp_path / "s", tmp_path / "m"
    _held_eth(s)
    # latest 15m bar low 94 < stop 95 -> stop fires
    ex = _FakeEx({"ETH/USDT:USDT": _frame([97, 94], [99, 97], [98, 96])})
    rep = run_exit_sweep(ex, Settings(), s, m, datetime(2026, 6, 8, 12, 0, tzinfo=UTC), 1)
    assert rep["closed"] == 1
    assert load_positions(s) == []
    # realized PnL on a ~5-point loss at qty 1 plus costs -> balance below the 10k start
    assert load_account(s, 10_000.0).balance < 10_000.0


def test_no_exit_carries_position(tmp_path):
    s, m = tmp_path / "s", tmp_path / "m"
    _held_eth(s)
    # bar stays between stop (95) and TP (130): low 96, high 98 -> carry
    ex = _FakeEx({"ETH/USDT:USDT": _frame([96, 96], [98, 98], [97, 97])})
    rep = run_exit_sweep(ex, Settings(), s, m, datetime(2026, 6, 8, 12, 0, tzinfo=UTC), 2)
    assert rep["closed"] == 0
    assert len(load_positions(s)) == 1
    assert rep["carried"] == 1  # the held (non-exited) position is counted as carried


def _short(stop, liq, lev=10.0):
    return Position(symbol="ETHUSDT", direction="short", qty=1.0, entry=100.0, stop=stop,
                    take_profits=[90.0], leverage=lev, margin=10.0, liq_price=liq, opened_cycle=1,
                    opened_ts=datetime(2026, 6, 8, tzinfo=UTC))


def test_healthy_10x_position_does_not_spam_liq_alert(tmp_path):
    # a 10x short whose liq sits ~5% from mark (stop is the real exit) must NOT alert — at 10x that
    # is a HEALTHY position, and a 10% buffer would alert on essentially every trade (noise).
    s, m = tmp_path / "s", tmp_path / "m"
    save_positions(s, [_short(stop=103.0, liq=105.0)])  # mark ~100; liq 5% away; stop 3% away
    ex = _FakeEx({"ETH/USDT:USDT": _frame([99.5, 99.5], [100.5, 100.5], [100, 100])})  # no exit
    rep = run_exit_sweep(ex, Settings(), s, m, datetime(2026, 6, 8, 12, 0, tzinfo=UTC), 8)
    assert rep["carried"] == 1 and rep["alerts"] == []  # ~5% from liq -> quiet


def test_liq_alert_fires_only_when_genuinely_close(tmp_path):
    # liq within the 3% buffer (price gapped toward liq past where the stop should fire) -> alert
    s, m = tmp_path / "s", tmp_path / "m"
    save_positions(s, [_short(stop=110.0, liq=102.0)])  # mark ~100; liq only 2% away
    ex = _FakeEx({"ETH/USDT:USDT": _frame([99.5, 99.5], [100.5, 100.5], [100, 100])})  # no exit
    rep = run_exit_sweep(ex, Settings(), s, m, datetime(2026, 6, 8, 12, 0, tzinfo=UTC), 9)
    assert any("liquidation" in a for a in rep["alerts"])  # genuinely close -> alert fires


def test_deep_drawdown_trips_halt(tmp_path):
    # a -45%+ drawdown across a sweep must SET the HALT flag (the pre-flatten tripwire), not just
    # compute it. Seed an account at peak 10000 / balance 5000; a held ETH at a big LOSS pushes
    # equity below 55% of peak -> should_halt -> set_halt.
    from futures_fund.state import AccountState, load_account, save_account, save_positions
    s, m = tmp_path / "s", tmp_path / "m"
    save_positions(s, [Position(
        symbol="ETHUSDT", direction="long", qty=20.0, entry=100.0, stop=40.0, take_profits=[300.0],
        leverage=5.0, margin=400.0, liq_price=30.0, opened_cycle=1,
        opened_ts=datetime(2026, 6, 8, tzinfo=UTC))])
    save_account(s, AccountState(balance=5_000.0, peak_equity=10_000.0))
    # mark ~50 (above stop 40, below TP): unrealized = 20*(50-100) = -1000 -> equity ~4000 -> dd 60%
    ex = _FakeEx({"ETH/USDT:USDT": _frame([52, 50], [54, 52], [49, 49])})
    rep = run_exit_sweep(ex, Settings(), s, m, datetime(2026, 6, 8, 12, 0, tzinfo=UTC), 7)
    assert rep["should_halt"] is True
    assert load_account(s, 10_000.0).halt is True  # the tripwire actually tripped


def test_empty_book_writes_report_and_no_op(tmp_path):
    s, m = tmp_path / "s", tmp_path / "m"
    ex = _FakeEx({})
    rep = run_exit_sweep(ex, Settings(), s, m, datetime(2026, 6, 8, 12, 7, tzinfo=UTC), 5)
    assert rep["closed"] == 0 and rep["opened"] == 0
    written = json.loads((s / "fast" / "cycle" / "5" / "report.json").read_text())
    assert written["loop"] == "fast" and written["candle"].startswith("2026-06-08T12:00")


def test_report_served_candle_is_15m_floored(tmp_path):
    s, m = tmp_path / "s", tmp_path / "m"
    _held_eth(s)
    ex = _FakeEx({"ETH/USDT:USDT": _frame([96, 96], [98, 98], [97, 97])})
    run_exit_sweep(ex, Settings(), s, m, datetime(2026, 6, 8, 12, 52, tzinfo=UTC), 3)
    written = json.loads((s / "fast" / "cycle" / "3" / "report.json").read_text())
    assert written["candle"].startswith("2026-06-08T12:45")  # 12:52 floors to the 12:45 15m candle


def test_halted_account_skips_sweep(tmp_path):
    from futures_fund.state import AccountState, save_account
    s, m = tmp_path / "s", tmp_path / "m"
    _held_eth(s)
    save_account(s, AccountState(balance=10_000.0, peak_equity=10_000.0, halt=True))
    ex = _FakeEx({"ETH/USDT:USDT": _frame([97, 94], [99, 97], [98, 96])})  # stop would fire
    rep = run_exit_sweep(ex, Settings(), s, m, datetime(2026, 6, 8, 12, 0, tzinfo=UTC), 4)
    assert rep["halted"] is True and rep["closed"] == 0
    assert len(load_positions(s)) == 1  # halt -> position untouched
