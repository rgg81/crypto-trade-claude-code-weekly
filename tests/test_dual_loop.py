"""Dual-loop integration: decisions carry loop/desk attribution, and the strategic open + fast
exit-sweep share ONE book consistently under the single-flight lock (strategic runs first)."""
from datetime import UTC, datetime

import numpy as np
import pandas as pd

from futures_fund.config import Settings
from futures_fund.cycle import execute_proposals, fetch_context
from futures_fund.fast_loop import run_exit_sweep
from futures_fund.journal import read_all_decisions
from futures_fund.market_data import FundingInfo
from futures_fund.memory_layout import ensure_memory_layout
from futures_fund.models import MmrBracket, SymbolSpec, TradeProposal
from futures_fund.runlock import single_flight, try_acquire
from futures_fund.state import AccountState, load_positions

_R2U = {"BTCUSDT": "BTC/USDT:USDT"}


class _Ex:
    """Exchange usable by the strategic open (4h ctx) AND the fast sweep (15m, raw->unified)."""
    def __init__(self, frames):
        self.frames = frames

    def unified_for_raw(self, raw):
        return _R2U.get(raw)

    def symbol_spec(self, symbol):
        return SymbolSpec(symbol="BTCUSDT", tick_size=0.01, step_size=0.001, min_notional=5.0,
                          mmr_brackets=[MmrBracket(notional_floor=0, notional_cap=1_000_000,
                                                   mmr=0.004, maint_amount=0.0, max_leverage=125)])

    def ohlcv(self, symbol, timeframe="4h", limit=500):
        return self.frames[(symbol, timeframe)]

    def funding(self, symbol):
        # timeframe-agnostic: use whatever frame we have for this symbol (4h or 15m)
        df = next(v for (sym, _tf), v in self.frames.items() if sym == symbol)
        last = float(df["close"].iloc[-1])
        return FundingInfo(symbol=symbol, current_rate=0.0,
                           next_funding_ts=datetime(2026, 1, 1, tzinfo=UTC), interval_hours=8.0,
                           mark_price=last, index_price=last)


def _frame(closes, lows=None, highs=None, freq="4h"):
    n = len(closes)
    lows = lows or [c - 0.2 for c in closes]
    highs = highs or [c + 0.2 for c in closes]
    return pd.DataFrame({"timestamp": pd.date_range("2026-06-08", periods=n, freq=freq, tz="UTC"),
                         "open": closes, "high": highs, "low": lows, "close": closes,
                         "volume": 1.0})


def _uptrend(n=60):
    c = list(100.0 + 0.8 * np.arange(n) + np.random.default_rng(3).normal(0, 0.05, n))
    return _frame(c)


def _settings():
    return Settings(account_size_usdt=10_000.0, symbols=["BTC/USDT:USDT"], timeframe="4h")


def test_strategic_open_records_loop_and_desk(tmp_path):
    s, m = tmp_path / "state", tmp_path / "memory"
    ensure_memory_layout(m)
    ex = _Ex({("BTC/USDT:USDT", "4h"): _uptrend()})
    ctx = fetch_context(ex, _settings())
    last = float(ctx.frames["BTC/USDT:USDT"]["close"].iloc[-1])
    prop = TradeProposal(symbol="BTCUSDT", direction="long", entry=last, stop=last - 4.0,
                         take_profits=[last + 8.0], atr=2.0, confidence=0.7, horizon_hours=8,
                         funding_rate=0.0)
    acct = AccountState(balance=10_000.0, peak_equity=10_000.0)
    rep = execute_proposals(ctx, [prop], ["momentum", "trader"], [], acct, s, m,
                            now=datetime(2026, 6, 8, tzinfo=UTC), cycle_no=1,
                            loop="strategic", desk_by_symbol={"BTCUSDT": "momentum"})
    assert rep["opened"] == 1
    opened = [d for d in read_all_decisions(m) if d.get("symbol") == "BTCUSDT"]
    assert opened and opened[-1]["loop"] == "strategic" and opened[-1]["desk"] == "momentum"


def test_strategic_open_then_fast_sweep_share_one_book(tmp_path):
    s, m = tmp_path / "state", tmp_path / "memory"
    ensure_memory_layout(m)
    # Strategic opens a long on the 4h uptrend...
    ex_open = _Ex({("BTC/USDT:USDT", "4h"): _uptrend()})
    ctx = fetch_context(ex_open, _settings())
    last = float(ctx.frames["BTC/USDT:USDT"]["close"].iloc[-1])
    prop = TradeProposal(symbol="BTCUSDT", direction="long", entry=last, stop=last - 4.0,
                         take_profits=[last + 8.0], atr=2.0, confidence=0.7, horizon_hours=8,
                         funding_rate=0.0)
    acct = AccountState(balance=10_000.0, peak_equity=10_000.0)
    execute_proposals(ctx, [prop], ["momentum", "trader"], [], acct, s, m,
                      now=datetime(2026, 6, 8, tzinfo=UTC), cycle_no=1, loop="strategic")
    assert len(load_positions(s)) == 1  # the strategic long is on the shared book

    # ...and the FAST 15m sweep manages the SAME position: a 15m bar gaps the stop -> closed.
    stop = last - 4.0
    fast_15m = _frame([last, stop - 1.0], lows=[last - 0.5, stop - 2.0],
                      highs=[last + 0.5, stop - 0.5], freq="15min")
    ex_fast = _Ex({("BTC/USDT:USDT", "15m"): fast_15m})
    rep = run_exit_sweep(ex_fast, _settings(), s, m, datetime(2026, 6, 8, 1, 0, tzinfo=UTC), 1)
    assert rep["closed"] == 1
    assert load_positions(s) == []  # fast loop closed the strategic position on the shared book


def test_lock_serializes_strategic_before_fast(tmp_path):
    s = tmp_path / "state"
    now = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
    # strategic holds the lock; a concurrent fast fire must stand down (single writer)
    with single_flight(s, now, owner="strategic") as strat_ok:
        assert strat_ok is True
        assert try_acquire(s, now, owner="fast")[0] is False  # fast blocked while strategic runs
    assert try_acquire(s, now, owner="fast")[0] is True       # released -> fast may run next
