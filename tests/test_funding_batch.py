"""Price the WHOLE UNIVERSE's funding in one call, not two per symbol.

The mark-price batch (test_mark_price_batch.py) fixed the GATE, which prices ~20 held positions.
It did not fix PREFLIGHT, which prices the whole ~100-name universe: `cycle.fetch_context` does

    fundings = {s: exchange.funding(s) for s in settings.symbols}

and `funding()` makes TWO unproxied calls per symbol (fetch_funding_rate + fetch_funding_interval).
That is ~200 sequential calls into the funding endpoint. The failures moved with the burst:

    cy409 08:00Z  HOLD-ON-DATA-OUTAGE  TimeoutError in preflight
    cy410 12:00Z  OK                   (not a settlement hour)
    cy411 16:00Z  HOLD-ON-DATA-OUTAGE  TimeoutError in preflight

Both failures land on an 8-hourly settlement hour and both are AFTER `warm:` succeeded — the klines
are proxied and cached, so the only unbatched work left is this funding loop.

`cycle` is PROTECTED and must not be edited, so the batching lives behind `funding()` in the
exchange: the first call warms a per-instance cache from ONE batch request and every later symbol
is served from it. Each CLI step is its own process, so the cache cannot go stale across cycles.
"""
from __future__ import annotations

from futures_fund.exchange import RETRYABLE_ATTEMPTS, FuturesExchange

_TS = 1_756_000_000_000


def _row(sym, rate, mark, index):
    return {"symbol": sym, "fundingRate": rate, "fundingTimestamp": _TS,
            "markPrice": mark, "indexPrice": index}


class _BatchClient:
    """A venue exposing the plural endpoints."""

    def __init__(self):
        self.batch_rate_calls = 0
        self.batch_interval_calls = 0
        self.single_rate_calls = 0
        self.single_interval_calls = 0

    def fetch_funding_rates(self, symbols=None):
        self.batch_rate_calls += 1
        return {"BTC/USDT:USDT": _row("BTC/USDT:USDT", "0.0001", "100.5", "100.4"),
                "ETH/USDT:USDT": _row("ETH/USDT:USDT", "-0.0002", "50.25", "50.20")}

    def fetch_funding_intervals(self, symbols=None):
        self.batch_interval_calls += 1
        return {"ETH/USDT:USDT": {"info": {"fundingIntervalHours": 4}}}

    def fetch_funding_rate(self, symbol):
        self.single_rate_calls += 1
        return _row(symbol, "0.009", "7.0", "6.9")

    def fetch_funding_interval(self, symbol):
        self.single_interval_calls += 1
        return {"info": {"fundingIntervalHours": 1}}


class _NoBatchClient(_BatchClient):
    fetch_funding_rates = None
    fetch_funding_intervals = None


def test_the_whole_universe_costs_ONE_batch_call_not_two_per_symbol():
    """THE regression. 100 symbols must not mean 200 requests into the settlement window."""
    c = _BatchClient()
    ex = FuturesExchange(c, keyless=True)

    universe = ["BTC/USDT:USDT", "ETH/USDT:USDT"] * 50  # re-asking must not re-fetch either
    for sym in universe:
        ex.funding(sym)

    assert c.batch_rate_calls == 1, "the funding rates must be fetched once for the whole universe"
    assert c.single_rate_calls == 0, "must not fall back to per-symbol when a batch call exists"
    assert c.single_interval_calls == 0


def test_the_batched_values_are_the_ones_the_venue_returned():
    """Batching must not quietly change what a leg is priced at."""
    ex = FuturesExchange(_BatchClient(), keyless=True)

    btc = ex.funding("BTC/USDT:USDT")
    eth = ex.funding("ETH/USDT:USDT")

    assert btc.current_rate == 0.0001
    assert btc.mark_price == 100.5
    assert btc.index_price == 100.4
    assert eth.current_rate == -0.0002
    assert eth.mark_price == 50.25


def test_the_funding_interval_comes_from_the_batch_and_defaults_to_8h():
    """interval_hours drives count_funding_events — a wrong one mis-signs accrued funding."""
    ex = FuturesExchange(_BatchClient(), keyless=True)

    assert ex.funding("ETH/USDT:USDT").interval_hours == 4.0, "must use the venue's interval"
    assert ex.funding("BTC/USDT:USDT").interval_hours == 8.0, "absent -> the venue default, not 0"


def test_a_symbol_the_batch_omits_falls_back_to_the_per_symbol_call():
    """A missing row must still price correctly — never crash, never a zero rate."""
    c = _BatchClient()
    ex = FuturesExchange(c, keyless=True)

    info = ex.funding("NOPE/USDT:USDT")

    assert info.current_rate == 0.009 and info.mark_price == 7.0
    assert c.single_rate_calls == 1


def test_falls_back_entirely_when_the_venue_has_no_batch_endpoint():
    c = _NoBatchClient()
    ex = FuturesExchange(c, keyless=True)

    info = ex.funding("BTC/USDT:USDT")

    assert info.current_rate == 0.009
    assert c.single_rate_calls == 1 and c.single_interval_calls == 1


def test_a_failing_batch_degrades_to_per_symbol_rather_than_losing_the_cycle():
    """The batch is an optimisation. If it fails, the cycle must still run the old way."""
    class _BrokenBatch(_BatchClient):
        def fetch_funding_rates(self, symbols=None):
            self.batch_rate_calls += 1
            raise TimeoutError("timed out")

    c = _BrokenBatch()
    ex = FuturesExchange(c, keyless=True)

    info = ex.funding("BTC/USDT:USDT")

    assert info.current_rate == 0.009, "must still return a usable rate"
    assert c.single_rate_calls == 1


def test_a_failed_batch_is_not_retried_on_every_symbol():
    """A universe-wide loop must not turn one failing batch into 100 failing batches."""
    class _BrokenBatch(_BatchClient):
        def fetch_funding_rates(self, symbols=None):
            self.batch_rate_calls += 1
            raise TimeoutError("timed out")

    c = _BrokenBatch()
    ex = FuturesExchange(c, keyless=True)

    for _ in range(20):
        ex.funding("BTC/USDT:USDT")

    # with_retry spends its own RETRYABLE_ATTEMPTS on the ONE batch; what must not happen is the
    # whole batch being re-attempted for every leg (20 legs => 40 requests, worse than no batch).
    assert c.batch_rate_calls <= RETRYABLE_ATTEMPTS, (
        "one failed batch must be remembered, not re-attempted per leg")
    assert c.single_rate_calls == 20


def test_preflight_prices_the_universe_without_a_per_symbol_burst():
    """The real call path: cycle.fetch_context loops funding() over the whole universe.

    `cycle` is protected, so this asserts the protected caller gets the batched behaviour for free.
    """
    import pandas as pd

    from futures_fund.cycle import fetch_context

    class _Ex(_BatchClient):
        def ohlcv(self, symbol, timeframe, limit=500):
            return pd.DataFrame({"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0],
                                 "volume": [1.0]})

        def symbol_spec(self, symbol):
            from futures_fund.models import SymbolSpec
            return SymbolSpec(symbol=symbol.split("/")[0] + "USDT", tick_size=0.01,
                              step_size=0.001, min_notional=5.0, mmr_brackets=[])

    class _Settings:
        symbols = ["BTC/USDT:USDT", "ETH/USDT:USDT"]
        timeframe = "4h"

    c = _Ex()
    ex = FuturesExchange(c, keyless=True)
    ex.ohlcv = c.ohlcv
    ex.symbol_spec = c.symbol_spec

    fetch_context(ex, _Settings())

    assert c.batch_rate_calls == 1
    assert c.single_rate_calls == 0, "preflight must not make a per-symbol funding burst"
