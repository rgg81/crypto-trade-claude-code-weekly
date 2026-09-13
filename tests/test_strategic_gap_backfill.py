"""The 4h exit audit only ever looked at the candle that was FORMING when the tick ran.

`cycle.audit_and_reflect` (PROTECTED) reads `ctx.frames[sym].iloc[-1]`, and Binance returns the
in-progress kline, so each tick checks stops against a candle that is ~25 minutes old. The other
~3h35m of every candle is never checked — not during downtime, during NORMAL cadence. Two live
cases on 2026-09-12/13:

    UAIUSDT long, stop 0.55041. cy441 audited the 12:00Z candle at 12:25Z (clear). The candle then
    fell to 0.5344. cy442 looked only at the 16:00Z candle. The stop was not booked until cy443 —
    about 20h after a resting stop would have filled.

    BEATUSDT short, stop 0.10608. The 09-12 00:00Z candle wicked to 0.1100 after its audit, then
    recovered. No later audit ever saw it: the leg stayed open, and the paper book showed -$2.40
    where a live resting stop would have realised -$14.27 — a gap that always flatters the desk.

The fast loop already solved this (`fast_loop._replay_missed_bars`, reusing the PROTECTED
`audit_and_reflect`/`detect_exit` verbatim via frame slicing). The strategic path never got it.

ONE DELIBERATE DIFFERENCE. The fast replay starts strictly AFTER the last served candle. The gate
stamps `report["candle"]` with the candle that was forming when the tick ran, so for the 4h loop
that candle was only PARTLY audited — its tail is exactly the UAI case. The strategic replay must
INCLUDE it. A trigger in its full range that was not present at the audit necessarily happened
after the audit, so re-checking it is correct, provided the position existed — with its current
stop — at the candle's open. Positions opened, or stops trailed, inside that candle are not checked
against it (their pre-event prices would fire a stop that never traded).
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from futures_fund.config import Settings
from futures_fund.costs import count_funding_events
from futures_fund.cycle import CycleContext
from futures_fund.exits import detect_exit
from futures_fund.journal import append_decision, read_all_decisions
from futures_fund.market_data import FundingInfo
from futures_fund.memory_layout import ensure_memory_layout
from futures_fund.models import MmrBracket, SymbolSpec
from futures_fund.orchestration import audit_with_gap_backfill
from futures_fund.state import AccountState, Position

RAW, UNI = "UAIUSDT", "UAI/USDT:USDT"
_SLIP = 2.0  # cycle._SLIPPAGE_BPS


def _ts(day, hour, minute=0):
    return datetime(2026, 9, day, hour, minute, tzinfo=UTC)


def _ctx(start, lows, highs=None, closes=None, *, rate=0.0):
    n = len(lows)
    highs = highs or [max(1.0, lo * 1.1) for lo in lows]
    closes = closes or [(lo + hi) / 2 for lo, hi in zip(lows, highs, strict=True)]
    df = pd.DataFrame({"timestamp": pd.date_range(start, periods=n, freq="4h", tz="UTC"),
                       "open": closes, "high": highs, "low": lows, "close": closes,
                       "volume": 1.0})
    spec = SymbolSpec(symbol=RAW, tick_size=0.0001, step_size=0.001, min_notional=5.0,
                      mmr_brackets=[MmrBracket(notional_floor=0, notional_cap=1_000_000, mmr=0.004,
                                               maint_amount=0.0, max_leverage=125)])
    fr = FundingInfo(symbol=UNI, current_rate=rate, next_funding_ts=_ts(1, 0), interval_hours=8.0,
                     mark_price=closes[-1], index_price=closes[-1])
    settings = Settings(account_size_usdt=10_000.0, symbols=[UNI], timeframe="4h")
    return CycleContext(settings, {UNI: df}, {UNI: fr}, {UNI: spec}, {RAW: UNI}, {RAW: spec},
                        {RAW: closes[-1]})


def _long(stop, *, opened=None, stop_ts=None, decision_id=None, entry=0.7863, qty=100.0):
    return Position(symbol=RAW, direction="long", qty=qty, entry=entry, stop=stop,
                    take_profits=[entry * 3], leverage=1.0, margin=qty * entry,
                    liq_price=entry * 0.01, opened_cycle=426, opened_ts=opened or _ts(9, 20),
                    decision_id=decision_id, stop_ts=stop_ts)


def _short(stop, *, entry=0.0816, qty=582.0):
    return Position(symbol=RAW, direction="short", qty=qty, entry=entry, stop=stop,
                    take_profits=[entry * 0.3], leverage=1.0, margin=qty * entry,
                    liq_price=entry * 2.0, opened_cycle=432, opened_ts=_ts(10, 20))


def _served(state_dir: Path, cycle: int, candle: datetime):
    d = state_dir / "cycle" / str(cycle)
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.json").write_text(json.dumps(
        {"cycle": cycle, "candle": candle.isoformat(), "ran_at": candle.isoformat()}))


def _report():
    return {"cycle": 0, "halted": False, "opened": 0, "closed": 0, "carried": 0,
            "stuck_close": 0, "equity": 10_000.0, "actions": []}


def _run(tmp_path, ctx, positions, now, report=None):
    m = tmp_path / "m"
    ensure_memory_layout(m)
    acct = AccountState(balance=10_000.0, peak_equity=10_000.0)
    rep = report if report is not None else _report()
    left = audit_with_gap_backfill(ctx, positions, acct, m, now, rep, tmp_path / "s")
    return left, rep, acct


# ---- the two live cases ---------------------------------------------------------------------

def test_UAI_a_stop_crossed_in_the_served_candles_unaudited_tail_closes(tmp_path):
    """cy441 served the 12:00Z candle at 12:25Z; it fell through the stop AFTER that audit. At the
    16:23Z tick the forming 16:00Z candle is clear, so a latest-bar-only audit misses it."""
    _served(tmp_path / "s", 441, _ts(12, 12))
    #            00:00  04:00  08:00  12:00(served) 16:00(forming)
    lows = [0.74, 0.74, 0.74, 0.5344, 0.5604]
    left, rep, _ = _run(tmp_path, _ctx(_ts(12, 0), lows), [_long(0.55041)], _ts(12, 16, 23))

    assert rep["closed"] == 1, "a stop crossed in the served candle's tail must close"
    assert left == []
    act = rep["actions"][0]
    assert act["reason"] == "stop"
    assert act["bar_close"] == _ts(12, 16).isoformat(), "say WHICH candle it really closed on"


def test_BEAT_a_stop_crossed_during_downtime_closes_though_price_recovered(tmp_path):
    """16h unserved: a middle candle wicked through the short's stop and price came back."""
    _served(tmp_path / "s", 442, _ts(12, 16))
    #        12:00  16:00(served) 20:00  00:00  04:00  08:00(forming)
    highs = [0.0938, 0.0939, 0.0909, 0.1100, 0.0878, 0.0862]
    lows = [0.080] * 6
    left, rep, acct = _run(tmp_path, _ctx(_ts(12, 12), lows, highs), [_short(0.10608)],
                           _ts(13, 8, 25))

    assert rep["closed"] == 1 and left == []
    assert rep["actions"][0]["reason"] == "stop"
    # booked at the STOP (plus slippage), which is what a resting stop fills on a continuous wick
    expected = detect_exit(_short(0.10608), bar_high=0.1100, bar_low=0.080, funding_rate=0.0,
                           funding_events=0, slippage_bps=_SLIP)
    assert rep["actions"][0]["pnl"] == pytest.approx(expected.realized_pnl)
    assert acct.balance == pytest.approx(10_000.0 + expected.realized_pnl)


# ---- booking honesty ------------------------------------------------------------------------

def test_a_replayed_close_is_booked_at_its_candles_close_with_funding_stopping_there(tmp_path):
    """Funding and exit_ts must end where the position really ended, not when the desk noticed.
    Opened 00:00Z, crossed in the 12:00Z candle, noticed 08:25Z the next day: 2 funding events
    (08:00, 16:00) belong to the trade, not 4."""
    m = tmp_path / "m"
    ensure_memory_layout(m)
    did = append_decision(m, {"ts": _ts(12, 0), "cycle": 438, "symbol": RAW, "direction": "long",
                              "entry": 0.7863, "stop": 0.55041})
    pos = _long(0.55041, opened=_ts(12, 0), decision_id=did)
    _served(tmp_path / "s", 441, _ts(12, 12))
    #        00:00 04:00 08:00 12:00(served) 16:00 20:00 00:00 04:00 08:00(forming)
    lows = [0.74, 0.74, 0.74, 0.5344, 0.60, 0.60, 0.60, 0.60, 0.60]
    ctx = _ctx(_ts(12, 0), lows, rate=0.0005)

    left, rep, _ = _run(tmp_path, ctx, [pos], _ts(13, 8, 25))

    assert rep["closed"] == 1
    events = count_funding_events(_ts(12, 0), _ts(12, 16), 8)
    to_now = count_funding_events(_ts(12, 0), _ts(13, 8, 25), 8)
    assert events < to_now, "scenario must discriminate"
    expected = detect_exit(pos, bar_high=1.0, bar_low=0.5344, funding_rate=0.0005,
                           funding_events=events, slippage_bps=_SLIP)
    assert rep["actions"][0]["pnl"] == pytest.approx(expected.realized_pnl)
    rec = next(d for d in read_all_decisions(m) if d["id"] == did)
    assert datetime.fromisoformat(str(rec["exit_ts"])) == _ts(12, 16)


# ---- no false exits -------------------------------------------------------------------------

def test_candles_before_the_served_candle_are_not_replayed(tmp_path):
    """They were fully replayed by the tick that followed them. Re-scanning history would re-test
    old candles against stops that may since have moved."""
    _served(tmp_path / "s", 441, _ts(12, 12))
    lows = [0.74, 0.74, 0.5344, 0.60, 0.60]  # the crossing candle is 08:00Z, BEFORE the served one
    left, rep, _ = _run(tmp_path, _ctx(_ts(12, 0), lows), [_long(0.55041)], _ts(12, 16, 23))
    assert rep["closed"] == 0 and len(left) == 1


def test_a_position_opened_inside_the_served_candle_ignores_its_pre_entry_prices(tmp_path):
    """Opened by the 12:25Z gate: the 12:00-12:25 dip happened before the position existed."""
    _served(tmp_path / "s", 441, _ts(12, 12))
    lows = [0.74, 0.74, 0.74, 0.5344, 0.60]
    pos = _long(0.55041, opened=_ts(12, 12, 26))
    left, rep, _ = _run(tmp_path, _ctx(_ts(12, 0), lows), [pos], _ts(12, 16, 23))
    assert rep["closed"] == 0 and len(left) == 1


def test_a_stop_trailed_inside_the_served_candle_ignores_its_pre_trail_prices(tmp_path):
    """Trailed 0.55 -> 0.60 at 12:26Z. The 0.58 print came BEFORE the trail, against a 0.55 stop."""
    _served(tmp_path / "s", 441, _ts(12, 12))
    lows = [0.74, 0.74, 0.74, 0.58, 0.61]
    pos = _long(0.60, stop_ts=_ts(12, 12, 26))
    left, rep, _ = _run(tmp_path, _ctx(_ts(12, 0), lows), [pos], _ts(12, 16, 23))
    assert rep["closed"] == 0 and len(left) == 1


def test_the_same_candle_DOES_close_an_untrailed_stop(tmp_path):
    """Control for the test above: without the trail the candle must be checked — otherwise the
    trail guard could pass by simply never checking the served candle at all."""
    _served(tmp_path / "s", 441, _ts(12, 12))
    lows = [0.74, 0.74, 0.74, 0.58, 0.61]
    left, rep, _ = _run(tmp_path, _ctx(_ts(12, 0), lows), [_long(0.60)], _ts(12, 16, 23))
    assert rep["closed"] == 1 and left == []


# ---- fail-safe and idempotence --------------------------------------------------------------

def test_with_no_served_candle_it_degrades_to_the_latest_bar_only(tmp_path):
    """No anchor means no bound on what to replay: keep the legacy behaviour exactly."""
    lows = [0.74, 0.5344, 0.74, 0.74, 0.60]
    left, rep, _ = _run(tmp_path, _ctx(_ts(12, 0), lows), [_long(0.55041)], _ts(12, 16, 23))
    assert rep["closed"] == 0 and len(left) == 1


def test_a_backfill_error_never_skips_the_live_audit(tmp_path, monkeypatch):
    """The latest-bar check is the load-bearing safety path; a replay bug must not take it down."""
    def _boom(*a, **k):
        raise RuntimeError("replay blew up")
    monkeypatch.setattr("futures_fund.fast_loop._replay_missed_bars", _boom)
    _served(tmp_path / "s", 441, _ts(12, 12))
    lows = [0.74, 0.74, 0.74, 0.74, 0.50]  # the LATEST candle crosses
    left, rep, _ = _run(tmp_path, _ctx(_ts(12, 0), lows), [_long(0.55041)], _ts(12, 16, 23))
    assert rep["closed"] == 1 and left == []
    assert any("backfill" in a.lower() for a in rep.get("alerts", []))


def test_a_replay_that_fails_MIDWAY_never_double_closes(tmp_path, monkeypatch):
    """The dangerous error is not the one before the replay starts — it is one AFTER it has already
    closed something. If the caller then falls back to the ORIGINAL position list, the live audit
    re-closes the same leg and credits its PnL twice."""
    import futures_fund.fast_loop as fl
    real, calls = fl._sliced_ctx, {"n": 0}

    def _second_bar_explodes(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("second replayed candle blew up")
        return real(*a, **k)
    monkeypatch.setattr(fl, "_sliced_ctx", _second_bar_explodes)
    _served(tmp_path / "s", 440, _ts(12, 8))
    #        00:00 04:00 08:00(served, crosses) 12:00(explodes) 16:00(forming, crosses too)
    lows = [0.74, 0.74, 0.50, 0.74, 0.50]
    doomed, survivor = _long(0.55041), _long(0.10, decision_id=None, qty=50.0)
    # the survivor keeps the replay loop alive past the first close, so it actually REACHES the
    # candle that explodes — with one position the loop would stop early and prove nothing
    left, rep, acct = _run(tmp_path, _ctx(_ts(12, 0), lows), [doomed, survivor], _ts(12, 16, 23))

    assert calls["n"] >= 2, "the scenario must reach the failing candle"
    assert rep["closed"] == 1, "closed once on the replay, never again by the live audit"
    assert left == [survivor]
    assert acct.balance == pytest.approx(10_000.0 + rep["actions"][0]["pnl"])
    assert any("backfill" in a.lower() for a in rep.get("alerts", []))


def test_a_rerun_of_the_same_tick_never_double_closes(tmp_path):
    """A DUE RETRY re-runs preflight on the same candle."""
    _served(tmp_path / "s", 441, _ts(12, 12))
    ctx = _ctx(_ts(12, 0), [0.74, 0.74, 0.74, 0.5344, 0.60])
    left, rep, acct = _run(tmp_path, ctx, [_long(0.55041)], _ts(12, 16, 23))
    assert rep["closed"] == 1
    bal = acct.balance
    rep2 = _report()
    left2 = audit_with_gap_backfill(ctx, left, acct, tmp_path / "m", _ts(12, 16, 40), rep2,
                                    tmp_path / "s")
    assert rep2["closed"] == 0 and left2 == [] and acct.balance == bal


# ---- visibility -----------------------------------------------------------------------------

def test_the_audit_summary_carries_every_close_and_alert():
    """context.json's audit kept only counts, so a close booked on an EARLIER candle — or a replay
    that stopped part-way — would be recorded nowhere an operator reads."""
    from futures_fund.orchestration import _audit_summary
    rep = {"closed": 2, "carried": 0, "alerts": ["gap-backfill stopped at the 12:00 bar"],
           "actions": [{"close": "UAIUSDT", "reason": "stop", "pnl": -23.97,
                        "bar_close": "2026-09-12T16:00:00+00:00"},
                       {"close": "LSKUSDT", "reason": "take_profit", "pnl": 132.53}]}
    s = _audit_summary(rep)
    assert (s["closed"], s["carried"]) == (2, 0)
    assert s["closes"] == rep["actions"]
    assert s["alerts"] == rep["alerts"]
    assert _audit_summary({"closed": 0, "carried": 0, "actions": []})["alerts"] == []


def test_both_preflight_exits_publish_the_full_audit():
    """The halted early-return is exactly when an exit replay matters most."""
    import inspect

    from futures_fund.orchestration import preflight_step
    assert inspect.getsource(preflight_step).count('"audit": _audit_summary(report)') == 2


# ---- wiring ---------------------------------------------------------------------------------

def test_preflight_runs_the_backfill_not_the_bare_latest_bar_audit():
    """A helper nothing calls reads as fixed and protects nothing."""
    import inspect

    from futures_fund.orchestration import preflight_step
    src = inspect.getsource(preflight_step)
    assert "audit_with_gap_backfill(" in src
    assert "audit_and_reflect(" not in src, "preflight must not bypass the backfill"
