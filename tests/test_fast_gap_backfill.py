"""Fast-loop DOWNTIME-GAP backfill: after a multi-tick outage, the exit sweep must replay the
COMPLETED 15m bars that were never served and close any position whose stop/TP/liq triggered
mid-gap (even if the latest forming bar has since recovered). At 10x a missed mid-gap liquidation
is the worst tail: the book would carry a position that honestly no longer exists.

The fix lives ONLY in non-protected code: a `last_served_candle` anchor in scheduling.py and a
backfill in fast_loop.run_exit_sweep that reuses the PROTECTED `cycle.audit_and_reflect` /
`exits.detect_exit` verbatim (by feeding each missed bar as the latest via frame slicing). No
protected module is edited; no limit is weakened — this only makes exits MORE faithful.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from futures_fund.config import Settings
from futures_fund.fast_loop import run_exit_sweep
from futures_fund.market_data import FundingInfo
from futures_fund.models import MmrBracket, SymbolSpec
from futures_fund.scheduling import last_served_candle
from futures_fund.state import Position, load_positions, save_positions

_R2U = {"ETHUSDT": "ETH/USDT:USDT", "BTCUSDT": "BTC/USDT:USDT"}
_U2R = {v: k for k, v in _R2U.items()}


def _frame(start: datetime, lows, highs, closes):
    """15m bars from `start`; per-bar low/high/close lists (equal length)."""
    n = len(closes)
    return pd.DataFrame({
        "timestamp": pd.date_range(start, periods=n, freq="15min", tz="UTC"),
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
        return SymbolSpec(symbol=_U2R.get(sym, "ETHUSDT"), tick_size=0.01, step_size=0.001,
                          min_notional=5.0,
                          mmr_brackets=[MmrBracket(notional_floor=0, notional_cap=1_000_000,
                                                   mmr=0.004, maint_amount=0.0, max_leverage=125)])


def _held_eth(state_dir):
    save_positions(state_dir, [Position(
        symbol="ETHUSDT", direction="long", qty=1.0, entry=100.0, stop=95.0, take_profits=[130.0],
        leverage=3.0, margin=33.3, liq_price=70.0, opened_cycle=1,
        opened_ts=datetime(2026, 6, 8, tzinfo=UTC))])


def _write_prior_fast_report(state_dir, cycle_no: int, candle_iso: str, swept: bool = True):
    d = Path(state_dir) / "fast" / "cycle" / str(cycle_no)
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.json").write_text(json.dumps(
        {"cycle": cycle_no, "loop": "fast", "candle": candle_iso, "closed": 0, "carried": 1,
         "swept": swept}))


def _held_eth_opened(state_dir, opened_ts, *, stop=95.0, liq=70.0, tps=(130.0,)):
    save_positions(state_dir, [Position(
        symbol="ETHUSDT", direction="long", qty=1.0, entry=100.0, stop=stop, take_profits=list(tps),
        leverage=3.0, margin=33.3, liq_price=liq, opened_cycle=1, opened_ts=opened_ts)])


_SETTINGS = Settings(account_size_usdt=10_000.0, symbols=["ETH/USDT:USDT"], timeframe="15m")


# ---- Step A: the served-candle anchor helper -------------------------------------------------

def test_last_served_candle_returns_most_recent_completed(tmp_path):
    """The anchor = the candle served by the highest completed fast cycle (None if none)."""
    td = tmp_path / "s"
    now = datetime(2026, 6, 8, 6, 7, tzinfo=UTC)
    assert last_served_candle(td, now, tf_minutes=15, loop="fast") is None
    _write_prior_fast_report(td, 3, "2026-06-08T05:00:00+00:00")
    _write_prior_fast_report(td, 7, "2026-06-08T05:45:00+00:00")
    got = last_served_candle(td, now, tf_minutes=15, loop="fast")
    assert got == datetime(2026, 6, 8, 5, 45, tzinfo=UTC)


# ---- Step B: the downtime-gap backfill -------------------------------------------------------

def test_gap_backfill_closes_stop_hit_on_missed_intermediate_bar(tmp_path):
    """A stop breached by a COMPLETED gap bar (not the latest, which recovered) must still close."""
    s, m = tmp_path / "s", tmp_path / "m"
    _held_eth(s)
    # last served candle = 00:00; ~2h outage; now floors to 02:30 (forming bar). Bars 00:15..02:15
    # were never served. The 01:00 bar dips to low 94 (< stop 95); EVERY other bar (incl latest
    # 02:30) stays >= 96. Latest-bar-only logic would MISS this; the backfill must catch it.
    start = datetime(2026, 6, 8, 0, 0, tzinfo=UTC)
    n = 11  # 00:00, 00:15, ... 02:30
    lows = [96.0] * n
    lows[4] = 94.0  # the 01:00 bar (index 4) breaches the stop mid-gap
    highs = [99.0] * n
    closes = [98.0] * n
    ex = _FakeEx({"ETH/USDT:USDT": _frame(start, lows, highs, closes)})
    _write_prior_fast_report(s, 5, "2026-06-08T00:00:00+00:00")
    now = datetime(2026, 6, 8, 2, 37, tzinfo=UTC)  # floor_tf -> 02:30
    rep = run_exit_sweep(ex, _SETTINGS, s, m, now, 6)
    assert rep["closed"] == 1, "stop breached on a missed gap bar must close the position"
    assert load_positions(s) == [] or len(load_positions(s)) == 0


def test_no_gap_does_not_reprocess_already_served_bar(tmp_path):
    """Normal cadence: the prior-served bar is NOT re-checked (exclusive lower bound) — guards
    against double-processing a bar already served live last tick."""
    s, m = tmp_path / "s", tmp_path / "m"
    _held_eth(s)
    # prior served candle = 00:15 (last tick's forming bar). now floors to 00:30. The ONLY gap
    # interval is (00:15, 00:30) -> empty. The 00:15 bar dips to 94 but was already served live
    # last tick; it must NOT be re-closed now. Latest 00:30 bar is clear (96).
    start = datetime(2026, 6, 8, 0, 0, tzinfo=UTC)
    lows = [96.0, 94.0, 96.0]   # 00:00, 00:15(breach, already served), 00:30(latest, clear)
    ex = _FakeEx({"ETH/USDT:USDT": _frame(start, lows, [99.0] * 3, [98.0] * 3)})
    _write_prior_fast_report(s, 5, "2026-06-08T00:15:00+00:00")
    now = datetime(2026, 6, 8, 0, 37, tzinfo=UTC)  # floor_tf -> 00:30
    rep = run_exit_sweep(ex, _SETTINGS, s, m, now, 6)
    assert rep["closed"] == 0, "an already-served bar must not be re-processed"
    assert len(load_positions(s)) == 1


def test_gap_backfill_idempotent_no_double_close(tmp_path):
    """Re-running after a gap close does not double-close or double-credit (position is gone)."""
    s, m = tmp_path / "s", tmp_path / "m"
    _held_eth(s)
    start = datetime(2026, 6, 8, 0, 0, tzinfo=UTC)
    n = 11
    lows = [96.0] * n
    lows[4] = 94.0
    ex = _FakeEx({"ETH/USDT:USDT": _frame(start, lows, [99.0] * n, [98.0] * n)})
    _write_prior_fast_report(s, 5, "2026-06-08T00:00:00+00:00")
    now = datetime(2026, 6, 8, 2, 37, tzinfo=UTC)
    rep1 = run_exit_sweep(ex, _SETTINGS, s, m, now, 6)
    assert rep1["closed"] == 1
    from futures_fund.state import load_account
    bal_after = load_account(s, _SETTINGS.account_size_usdt).balance
    rep2 = run_exit_sweep(ex, _SETTINGS, s, m, now + timedelta(minutes=15), 7)
    assert rep2["closed"] == 0
    assert load_account(s, _SETTINGS.account_size_usdt).balance == bal_after


# ---- adversarial-review fixes ----------------------------------------------------------------

def test_backfill_does_not_close_on_bars_predating_opened_ts(tmp_path):
    """MUST-FIX: the fast anchor does NOT advance on a strategic open, so during a >15m strategic
    lock-hold the gap window can contain bars that PREDATE a just-opened position. detect_exit has
    no opened_ts guard, so a stop/liq/TP the symbol hit BEFORE the position existed would spuriously
    close it at a price never traded. A position must NOT be closed on any pre-entry gap bar."""
    base = datetime(2026, 6, 8, 0, 0, tzinfo=UTC)
    for kind in ("stop_liq", "tp"):
        s, m = tmp_path / f"s_{kind}", tmp_path / f"m_{kind}"
        # position opened 02:00; the 01:00 gap bar (index 4) predates it and breaches a level
        _held_eth_opened(s, datetime(2026, 6, 8, 2, 0, tzinfo=UTC))
        n = 11  # 00:00 .. 02:30
        lows, highs = [96.0] * n, [99.0] * n
        if kind == "stop_liq":
            lows[4] = 69.0   # below liq 70 AND stop 95 -> would spuriously stop/liquidate
        else:
            highs[4] = 131.0  # above tp 130 -> would spuriously take-profit
        ex = _FakeEx({"ETH/USDT:USDT": _frame(base, lows, highs, [98.0] * n)})
        _write_prior_fast_report(s, 5, "2026-06-08T00:00:00+00:00")
        now = datetime(2026, 6, 8, 2, 37, tzinfo=UTC)  # floor -> 02:30
        rep = run_exit_sweep(ex, _SETTINGS, s, m, now, 6)
        assert rep["closed"] == 0, f"{kind}: pre-entry bar must not close a not-yet-open position"
        assert len(load_positions(s)) == 1


def test_backfill_internal_error_does_not_abort_live_sweep(tmp_path, monkeypatch):
    """SHOULD-FIX: a backfill exception must degrade to latest-bar-only and never crash the sweep —
    the live latest-bar exit check is the load-bearing safety path."""
    s, m = tmp_path / "s", tmp_path / "m"
    _held_eth(s)  # stop 95, opened 2026-06-08 (predates all bars)
    base = datetime(2026, 6, 8, 0, 0, tzinfo=UTC)
    n = 11
    lows = [96.0] * n
    lows[-1] = 94.0  # the LATEST (forming) bar breaches the stop -> live audit must still close it
    ex = _FakeEx({"ETH/USDT:USDT": _frame(base, lows, [99.0] * n, [98.0] * n)})
    _write_prior_fast_report(s, 5, "2026-06-08T00:00:00+00:00")

    def _boom(*a, **k):
        raise RuntimeError("backfill blew up")
    monkeypatch.setattr("futures_fund.fast_loop._replay_missed_bars", _boom)
    now = datetime(2026, 6, 8, 2, 37, tzinfo=UTC)
    rep = run_exit_sweep(ex, _SETTINGS, s, m, now, 6)
    assert rep["closed"] == 1, "live latest-bar exit must run even when backfill errors"
    assert any("backfill" in a.lower() for a in rep.get("alerts", []))
    assert len(load_positions(s)) == 0


def test_halt_window_does_not_hide_missed_intermediate_exit(tmp_path):
    """SHOULD-FIX: halted (non-sweeping) ticks must NOT advance the backfill anchor, else a stop/liq
    that triggers during a halt is silently missed once the halt clears (the fast sweep is the only
    15m liquidation guard while the desk rides halted-but-open positions)."""
    s, m = tmp_path / "s", tmp_path / "m"
    _held_eth(s)  # opened 2026-06-08, stop 95
    base = datetime(2026, 6, 8, 0, 0, tzinfo=UTC)
    _write_prior_fast_report(s, 1, "2026-06-08T00:00:00+00:00", swept=True)  # last REAL sweep
    cands = ["00:15", "00:30", "00:45", "01:00", "01:15", "01:30", "01:45", "02:00",
             "02:15", "02:30", "02:45", "03:00", "03:15", "03:30", "03:45"]
    for i, cand in enumerate(cands, start=2):  # halted no-sweep ticks advance reports to 03:45
        _write_prior_fast_report(s, i, f"2026-06-08T{cand}:00+00:00", swept=False)
    n = 17  # 00:00 .. 04:00
    lows = [96.0] * n
    lows[12] = 94.0  # the 03:00 bar breached the stop DURING the halt; 04:00 recovered (96)
    ex = _FakeEx({"ETH/USDT:USDT": _frame(base, lows, [99.0] * n, [98.0] * n)})
    now = datetime(2026, 6, 8, 4, 7, tzinfo=UTC)  # floor 04:00; account NOT halted (halt cleared)
    rep = run_exit_sweep(ex, _SETTINGS, s, m, now, 99)
    assert rep["closed"] == 1, "a stop breached during a halt window must close once halt clears"


def test_backfill_alerts_when_gap_exceeds_fetch_window(tmp_path):
    """NIT: if prev_served predates the oldest fetched bar, the backfill is partial — alert the
    operator rather than silently dropping early-outage bars."""
    s, m = tmp_path / "s", tmp_path / "m"
    _held_eth(s)
    base = datetime(2026, 6, 8, 0, 0, tzinfo=UTC)  # frame's oldest bar
    ex = _FakeEx({"ETH/USDT:USDT": _frame(base, [96.0] * 11, [99.0] * 11, [98.0] * 11)})
    _write_prior_fast_report(s, 5, "2026-06-01T00:00:00+00:00")  # a week before the frame
    now = datetime(2026, 6, 8, 2, 37, tzinfo=UTC)
    rep = run_exit_sweep(ex, _SETTINGS, s, m, now, 6)
    assert any("fetch window" in a.lower() for a in rep.get("alerts", [])), \
        "a partial backfill (gap older than the fetched window) must alert"


def test_fetch_window_alert_is_per_symbol_not_global_min(tmp_path):
    """A deep-history symbol must NOT mask a thin symbol's short fetch window: the partial-backfill
    alert must fire (and name the thin symbol) whenever ANY held symbol's history starts after
    prev_served, even if another symbol reaches back past it."""
    s, m = tmp_path / "s", tmp_path / "m"
    save_positions(s, [
        Position(symbol="ETHUSDT", direction="long", qty=1.0, entry=100.0, stop=95.0,
                 take_profits=[130.0], leverage=3.0, margin=33.3, liq_price=70.0, opened_cycle=1,
                 opened_ts=datetime(2026, 6, 7, tzinfo=UTC)),
        Position(symbol="BTCUSDT", direction="long", qty=1.0, entry=100.0, stop=95.0,
                 take_profits=[130.0], leverage=3.0, margin=33.3, liq_price=70.0, opened_cycle=1,
                 opened_ts=datetime(2026, 6, 7, tzinfo=UTC)),
    ])
    # ETH reaches back BEFORE prev_served (deep); BTC's frame starts AFTER it (thin/newly-listed).
    eth = _frame(datetime(2026, 6, 7, 20, 0, tzinfo=UTC), [96.0] * 30, [99.0] * 30, [98.0] * 30)
    btc = _frame(datetime(2026, 6, 8, 12, 0, tzinfo=UTC), [96.0] * 12, [99.0] * 12, [98.0] * 12)
    ex = _FakeEx({"ETH/USDT:USDT": eth, "BTC/USDT:USDT": btc})
    _write_prior_fast_report(s, 5, "2026-06-08T00:00:00+00:00")  # prev_served between the starts
    now = datetime(2026, 6, 8, 14, 7, tzinfo=UTC)
    rep = run_exit_sweep(ex, _SETTINGS, s, m, now, 6)
    alerts = " ".join(rep.get("alerts", [])).lower()
    assert "fetch window" in alerts, "thin symbol's short window must still raise the partial alert"
    assert "btcusdt" in alerts, "the alert must NAME the symbol whose history is too short"
