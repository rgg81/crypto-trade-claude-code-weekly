"""Fetch marks in ONE call, not one per position.

Timeouts cluster with total precision at Binance's 8-hourly funding settlement:

    FAIL 00:00 / 08:00 / 16:00 UTC   (cy368, cy370, cy372, cy374)
    OK   04:00 / 12:00 / 20:00 UTC   (cy366, cy367, cy369, cy371, cy373)

4-for-4 and 5-for-5. The gate priced the book with ~20 SEQUENTIAL fetch_funding_rate calls — hitting
the funding endpoint exactly when every perp on the exchange settles and the neighbour fleet
recalculates. One batch call removes both the long failure window and most of this desk's own
contribution to the spike.
"""
from __future__ import annotations

from futures_fund.exchange import FuturesExchange


class _BatchClient:
    def __init__(self):
        self.batch_calls = 0
        self.single_calls = 0

    def fetch_mark_prices(self, symbols=None):
        self.batch_calls += 1
        return {"BTC/USDT:USDT": {"markPrice": "100.5"},
                "ETH/USDT:USDT": {"markPrice": "50.25"}}

    def fetch_funding_rate(self, symbol):
        self.single_calls += 1
        return {"markPrice": "1.0"}


class _NoBatchClient(_BatchClient):
    fetch_mark_prices = None

    def fetch_funding_rate(self, symbol):
        self.single_calls += 1
        return {"markPrice": {"BTC/USDT:USDT": "100.5"}.get(symbol, "7.0")}


def test_marks_for_many_symbols_is_ONE_call():
    c = _BatchClient()
    ex = FuturesExchange(c, keyless=True)
    out = ex.mark_prices(["BTC/USDT:USDT", "ETH/USDT:USDT"])
    assert out == {"BTC/USDT:USDT": 100.5, "ETH/USDT:USDT": 50.25}
    assert c.batch_calls == 1
    assert c.single_calls == 0, "must not fall back to per-symbol when a batch call exists"


def test_only_requested_symbols_are_returned():
    ex = FuturesExchange(_BatchClient(), keyless=True)
    assert set(ex.mark_prices(["BTC/USDT:USDT"])) == {"BTC/USDT:USDT"}


def test_falls_back_to_per_symbol_when_the_venue_has_no_batch_endpoint():
    c = _NoBatchClient()
    ex = FuturesExchange(c, keyless=True)
    out = ex.mark_prices(["BTC/USDT:USDT"])
    assert out == {"BTC/USDT:USDT": 100.5}
    assert c.single_calls == 1


def test_a_symbol_the_batch_omits_is_absent_not_zero():
    """A missing mark must not silently become 0.0 — that would price a position at nothing."""
    ex = FuturesExchange(_BatchClient(), keyless=True)
    out = ex.mark_prices(["BTC/USDT:USDT", "NOPE/USDT:USDT"])
    assert "NOPE/USDT:USDT" not in out


def test_empty_request_makes_no_call():
    c = _BatchClient()
    assert FuturesExchange(c, keyless=True).mark_prices([]) == {}
    assert c.batch_calls == 0 and c.single_calls == 0


def test_monitor_prices_the_whole_book_in_one_call():
    """The gate's pricing burst is monitor.position_marks. It must make ONE call for N positions."""
    from futures_fund.monitor import position_marks

    class _Pos:
        def __init__(self, sym):
            self.symbol = sym

    class _Ex:
        def __init__(self):
            self.calls = 0
            self.singles = 0

        def unified_for_raw(self, raw):
            return raw.replace("USDT", "/USDT:USDT")

        def mark_prices(self, syms):
            self.calls += 1
            return dict.fromkeys(syms, 42.0)

        def mark_price(self, sym):
            self.singles += 1
            return 42.0

    ex = _Ex()
    marks, unpriced = position_marks(ex, [_Pos("BTCUSDT"), _Pos("ETHUSDT"), _Pos("SOLUSDT")])
    assert marks == {"BTCUSDT": 42.0, "ETHUSDT": 42.0, "SOLUSDT": 42.0}
    assert unpriced == []
    assert ex.calls == 1, f"expected 1 batch call for 3 positions, got {ex.calls}"
    assert ex.singles == 0, "must not fall back to per-position calls"
