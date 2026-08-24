"""Candles must be present AND fresh before the cycle runs — never stale.

Operator requirement: "it's a requirement to have the data before the cycle... do not let the data
go stale before the cycle. Non-negotiable."

So auto_cycle warms every series it is about to use through the local binance-proxy, BEFORE
preflight builds the briefs, and checks the newest candle is actually current. Two failure modes
this closes:

* COLD DATA. preflight pulling ~14 uncached series inside the cycle is the burst that tripped the
  -1003 bans. Warming first means the proxy has already coalesced and cached them, so preflight
  reads warm data and the cycle never races the rate limiter.
* STALE DATA. A proxy that answers from cache while its upstream is broken would happily serve
  candles hours old, and the desk would size a book on a stale tape without noticing. A series
  whose newest candle is older than `max_age_intervals` is reported so the caller can HOLD instead
  of trading on it.

Freshness is measured against the newest candle's OPEN time, with a 2-interval allowance: one for
the candle still forming (never closed, so never the newest closed bar) and one for clock/publish
slack. Tighter than that would false-alarm every cycle; looser would let a genuinely dead feed
through.
"""
from datetime import UTC, datetime, timedelta

import pytest

from scripts.auto_cycle import _interval_ms, _newest_open_ms, _stale_series

H4 = 4 * 3600 * 1000


def _now_ms(dt=None):
    return int((dt or datetime.now(UTC)).timestamp() * 1000)


def test_interval_ms_covers_the_timeframes_the_desk_uses():
    assert _interval_ms("4h") == H4
    assert _interval_ms("15m") == 15 * 60 * 1000
    assert _interval_ms("1h") == 3600 * 1000
    assert _interval_ms("1d") == 24 * 3600 * 1000


def test_newest_open_ms_reads_the_last_row():
    rows = [[1000, "1", "1", "1", "1", "1"], [2000, "1", "1", "1", "1", "1"]]
    assert _newest_open_ms(rows) == 2000
    assert _newest_open_ms([]) is None


def test_current_data_is_not_stale():
    now = _now_ms()
    assert _stale_series(now - H4, "4h", now_ms=now) is False        # last closed candle
    assert _stale_series(now, "4h", now_ms=now) is False             # forming candle


def test_the_two_interval_allowance_is_the_boundary():
    now = _now_ms()
    assert _stale_series(now - 2 * H4, "4h", now_ms=now) is False
    assert _stale_series(now - 2 * H4 - 60_000, "4h", now_ms=now) is True


def test_a_dead_feed_is_caught():
    """A proxy serving hours-old cache while its upstream is broken must not pass."""
    now = _now_ms()
    assert _stale_series(now - 12 * H4, "4h", now_ms=now) is True
    day_old = _now_ms(datetime.now(UTC) - timedelta(days=1))
    assert _stale_series(day_old, "4h", now_ms=now) is True


def test_no_data_at_all_counts_as_stale():
    assert _stale_series(None, "4h", now_ms=_now_ms()) is True


@pytest.mark.parametrize("tf", ["15m", "1h", "4h"])
def test_staleness_scales_with_the_timeframe(tf):
    now, iv = _now_ms(), _interval_ms(tf)
    assert _stale_series(now - 2 * iv, tf, now_ms=now) is False
    assert _stale_series(now - 3 * iv, tf, now_ms=now) is True


def test_the_cycle_warms_before_preflight():
    """Wiring: the warm must happen BEFORE preflight builds the briefs, or it is pointless."""
    import inspect

    from scripts import auto_cycle
    src = inspect.getsource(auto_cycle.main)
    assert "_warm_klines(" in src
    assert src.index("_warm_klines(") < src.index("scripts/preflight.py"), (
        "warming after preflight would not protect the cycle's own fetches")
