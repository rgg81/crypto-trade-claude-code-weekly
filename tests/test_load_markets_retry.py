"""The ONE venue call with no retry was the one losing whole cycles.

Every network call in the exchange goes through `with_retry` — except `load_markets()`, which
`from_settings` makes before anything else. It pulls `/fapi/v1/exchangeInfo`, the single largest
public payload on the venue (every market on binanceusdm), so it is the call most likely to time
out under shared-IP load. Both CLI entry points build the exchange this way, so one flaky
exchangeInfo takes out preflight OR the gate:

    cy413 preflight  ccxt.base.errors.NetworkError: binanceusdm GET .../fapi/v1/exchangeInfo
    cy412 gate       TimeoutError in http/client.py, gate produced no report
    cy413 gate       TimeoutError in http/client.py, gate produced no report

`with_retry` already encodes the rule that matters: a transient transport fault costs one retry, a
rate-limit BAN is never retried (retrying a 418/-1003 re-extends it ~22 minutes). Wrapping this call
just stops it being the exception.

The book was never at risk in any of the three — all were hold ticks and the HOLD-ON-DATA-OUTAGE
path held it — but a lost cycle is still a lost cycle, and on a REBALANCE tick a gate that dies
partway through is the one thing that can leave the book one-sided.
"""
from __future__ import annotations

import pytest

from futures_fund.config import Settings
from futures_fund.exchange import FuturesExchange


class _Client:
    def __init__(self, fail_times=0, exc=None):
        self.calls = 0
        self._fail_times = fail_times
        self._exc = exc or TimeoutError("timed out")

    def load_markets(self):
        self.calls += 1
        if self.calls <= self._fail_times:
            raise self._exc
        return {"BTC/USDT:USDT": {}}


def _patch(monkeypatch, client):
    monkeypatch.setattr("futures_fund.exchange.build_ccxt", lambda settings: client)


def test_a_transient_exchangeinfo_timeout_costs_a_retry_not_the_cycle(monkeypatch):
    """THE regression: cy412/cy413 both died here and lost the tick."""
    c = _Client(fail_times=1)
    _patch(monkeypatch, c)

    ex = FuturesExchange.from_settings(Settings(live=False))

    assert ex.client is c
    assert c.calls == 2, "a transient failure must be retried, not fatal"


def test_a_network_error_is_retried_too(monkeypatch):
    """cy413's preflight raised ccxt NetworkError, not a bare TimeoutError."""
    c = _Client(fail_times=1, exc=Exception("binanceusdm GET https://fapi.binance.com/fapi/v1/"
                                            "exchangeInfo connection timed out"))
    _patch(monkeypatch, c)

    FuturesExchange.from_settings(Settings(live=False))

    assert c.calls == 2


def test_a_rate_limit_BAN_is_never_retried(monkeypatch):
    """Retrying a ban re-extends it ~22 minutes — the worst thing this desk can do to itself."""
    ban = TimeoutError('418 {"code":-1003,"msg":"Way too many requests; IP banned until 123"}')
    c = _Client(fail_times=99, exc=ban)
    _patch(monkeypatch, c)

    with pytest.raises(Exception, match="1003"):
        FuturesExchange.from_settings(Settings(live=False))

    assert c.calls == 1, "a ban must cost exactly ONE call, never a retry"


def test_a_persistent_outage_still_raises_so_the_book_is_HELD(monkeypatch):
    """Retry must not paper over a real outage: a half-built exchange would be far worse than a
    held book. The raise is what triggers HOLD-ON-DATA-OUTAGE."""
    c = _Client(fail_times=99)
    _patch(monkeypatch, c)

    with pytest.raises(TimeoutError):
        FuturesExchange.from_settings(Settings(live=False))


def test_the_markets_are_actually_loaded(monkeypatch):
    """Guard against 'fixing' the retry by simply not calling load_markets."""
    c = _Client(fail_times=0)
    _patch(monkeypatch, c)

    FuturesExchange.from_settings(Settings(live=False))

    assert c.calls == 1, "markets must still be loaded exactly once on the happy path"
