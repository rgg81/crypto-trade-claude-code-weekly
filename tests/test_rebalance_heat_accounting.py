"""On a rebalance the gate vetoed new opens against heat from legs it was about to close.

`cycle.execute_proposals` (PROTECTED) builds `open_dicts` from EVERY current position and
heat-checks each new open against it — then, only afterwards, works out which positions this cycle
closes (`force_close` -> `closeable`). `consolidate` already reserves heat for SURVIVORS only
(`reserved` skips `closeable`), so the gate disagreed with itself: the book budget netted the
closes out, the per-open veto did not.

Live at cy456 (2026-09-15), a daily rebalance of a 37-leg book:

    target 20/side | open 14 close 11 hold 26
    VETOED 11 opens: no heat headroom (used 0.088 >= cap 0.080)
    -> book fell to 14/side (28 legs, from 38)

`used 0.088` was the 26 survivors (0.0557) PLUS ~0.032 from the 11 legs being closed. The real
post-rotation book carried 0.064 — under the 0.080 cap. Breadth, the measured edge, was cut by a
quarter for heat that no longer existed. It recurs on every rebalance: that is exactly when closes
and opens land together.

The fix checks opens against the book that WILL exist. No cap moves: a leg that genuinely
survives — kept, or a force-close that cannot be priced and so stays stuck open — still counts.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from futures_fund.config import Settings
from futures_fund.cycle import execute_proposals, fetch_context
from futures_fund.memory_layout import ensure_memory_layout
from futures_fund.models import MmrBracket, SymbolSpec, TradeProposal
from futures_fund.state import AccountState, Position

NOW = datetime(2026, 3, 1, tzinfo=UTC)


def _trend(start, step, seed, n=60):
    rng = np.random.default_rng(seed)
    close = start + step * np.arange(n) + rng.normal(0, 0.05, n)
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=n, freq="4h", tz="UTC"),
        "open": close, "high": close + 0.2, "low": close - 0.2, "close": close, "volume": 1.0})


class _Ex:
    """Per-symbol specs — the stock test exchange answers BTCUSDT for everything."""

    def __init__(self, frames):
        self.frames = frames

    def symbol_spec(self, symbol):
        return SymbolSpec(symbol=symbol.split("/")[0] + "USDT", tick_size=0.01, step_size=0.001,
                          min_notional=5.0,
                          mmr_brackets=[MmrBracket(notional_floor=0, notional_cap=1_000_000,
                                                   mmr=0.004, maint_amount=0.0, max_leverage=125)])

    def ohlcv(self, symbol, timeframe="4h", limit=500):
        return self.frames[symbol]

    def funding(self, symbol):
        from futures_fund.market_data import FundingInfo
        px = float(self.frames[symbol]["close"].iloc[-1])
        return FundingInfo(symbol=symbol, current_rate=0.0, next_funding_ts=NOW, interval_hours=8.0,
                           mark_price=px, index_price=px)


# All calm trends -> low_vol_trend everywhere: max_heat 0.10 at the healthy tier.
FRAMES = {"BTC/USDT:USDT": _trend(100.0, 0.8, 1), "SOL/USDT:USDT": _trend(100.0, 0.8, 2),
          "XRP/USDT:USDT": _trend(100.0, 0.8, 3), "ETH/USDT:USDT": _trend(200.0, -0.8, 4)}


def _held(symbol, ctx, heat=0.06, equity=10_000.0):
    """A long AT the current mark (zero unrealized, so equity stays 10k) carrying `heat`."""
    px = ctx.prices[symbol]
    dist = 12.0
    qty = heat * equity / dist
    return Position(symbol=symbol, direction="long", qty=qty, entry=px, stop=px - dist,
                    take_profits=[px + 3 * dist], leverage=1.0, margin=qty * px,
                    liq_price=px * 0.01, opened_cycle=1, opened_ts=datetime(2026, 2, 1, tzinfo=UTC))


def _run(tmp_path, symbols, positions, force_close):
    state, memory = tmp_path / "state", tmp_path / "memory"
    ensure_memory_layout(memory)
    ctx = fetch_context(_Ex(FRAMES), Settings(account_size_usdt=10_000.0, symbols=symbols,
                                             timeframe="4h"))
    held = [p(ctx) if callable(p) else p for p in positions]
    last = ctx.prices["ETHUSDT"]
    # A SHORT, so correlation-cluster trimming of same-direction legs cannot mask the result.
    prop = TradeProposal(symbol="ETHUSDT", direction="short", entry=last, stop=last + 4.0,
                         take_profits=[last - 8.0], atr=2.0, confidence=0.7, horizon_hours=4,
                         funding_rate=0.0)
    report = execute_proposals(ctx, [prop], contributing_agents=["trader"], positions=held,
                               account=AccountState(balance=10_000.0, peak_equity=10_000.0),
                               state_dir=state, memory_dir=memory, now=NOW, cycle_no=2,
                               close_absent=False, force_close=force_close)
    return report, _veto_reasons(state)


def _veto_reasons(state: Path) -> list[str]:
    p = state / "shadow-ledger.jsonl"
    if not p.exists():
        return []
    return [json.loads(line).get("reason", "") for line in p.read_text().splitlines() if line]


ALL = list(FRAMES)


def test_cy456_a_leg_being_closed_does_not_block_an_open_with_its_heat(tmp_path):
    """Kept 0.06 + closing 0.06 = 0.12 >= cap 0.10 was vetoed; the book that will exist is 0.06."""
    report, reasons = _run(tmp_path, ALL,
                           [lambda c: _held("SOLUSDT", c), lambda c: _held("XRPUSDT", c)],
                           force_close={"XRPUSDT"})
    assert not any("heat headroom" in r for r in reasons), reasons
    assert report["closed"] == 1 and report["opened"] == 1


def test_a_KEPT_leg_still_counts_against_the_cap(tmp_path):
    """Control: nothing is closing, so 0.12 of real heat must still veto. The fix must not simply
    stop counting held heat."""
    report, reasons = _run(tmp_path, ALL,
                           [lambda c: _held("SOLUSDT", c), lambda c: _held("XRPUSDT", c)],
                           force_close=set())
    assert any("no heat headroom" in r for r in reasons), reasons
    assert report["opened"] == 0


def test_a_force_close_that_cannot_be_priced_stays_open_and_still_counts(tmp_path):
    """A stuck close is not a close: XRP has no market data this cycle, so it cannot be closed and
    its heat is still on the book — exactly how `consolidate` reserves it."""
    ctx_symbols = ["BTC/USDT:USDT", "SOL/USDT:USDT", "ETH/USDT:USDT"]      # XRP unpriceable
    probe = fetch_context(_Ex(FRAMES), Settings(account_size_usdt=10_000.0,
                                                symbols=ALL, timeframe="4h"))
    stuck_xrp = _held("XRPUSDT", probe)
    report, reasons = _run(tmp_path, ctx_symbols,
                           [lambda c: _held("SOLUSDT", c), stuck_xrp],
                           force_close={"XRPUSDT"})
    assert any("no heat headroom" in r for r in reasons), reasons
    assert report["opened"] == 0
