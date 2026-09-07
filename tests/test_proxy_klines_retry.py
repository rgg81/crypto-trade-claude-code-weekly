"""The kline fetch is the LAST un-retried network call, and it is made ~100 times a tick.

Found by the outage forensics (30360c6), not by guessing — the captured cy417 traceback names it
outright, where the console's 200-char tail had only ever shown the shared socket frames:

    futures_fund/cycle.py:52   frames = {s: exchange.ohlcv(s, ...) for s in settings.symbols}
    futures_fund/exchange.py:176   raw = _proxy_get_klines(
    futures_fund/exchange.py:111   urllib.request.urlopen(f"{url}?{q}", timeout=timeout or 30)
    TimeoutError: timed out

So the stall is the LOCAL proxy, not Binance, and not the two things previously fixed (the funding
burst, the unretried load_markets). Protected `cycle.fetch_context` loops one GET per symbol over a
~100-name universe, `_proxy_get_klines` had no retry, and a single stall past 30s loses the tick.

Retrying is safe here in a way it would not be against the venue: these are idempotent GETs to a
LOCAL coalescing/caching proxy, so a retry costs one localhost round-trip and cannot contribute to
an IP ban. `with_retry` still refuses to retry anything carrying a ban marker, so a 418 relayed
THROUGH the proxy is passed straight up as before.
"""
from __future__ import annotations

import pytest

from futures_fund import exchange as ex_mod
from futures_fund.exchange import RETRYABLE_ATTEMPTS, FuturesExchange


class _Client:
    def market(self, symbol):
        return {"id": symbol.replace("/", "").replace(":USDT", "")}


_ROWS = [[1, "1", "2", "0.5", "1.5", "10", 2, "0", 1, "0", "0", "0"]] * 30


def _exchange():
    return FuturesExchange(_Client(), keyless=True, klines_proxy_url="http://127.0.0.1:8000")


def test_a_transient_proxy_stall_costs_a_retry_not_the_tick(monkeypatch):
    """THE regression: cy417 lost the whole cycle to one stalled localhost GET."""
    calls = []

    def flaky(url, params=None, timeout=None):
        calls.append(url)
        if len(calls) == 1:
            raise TimeoutError("timed out")
        return _ROWS

    monkeypatch.setattr(ex_mod, "_proxy_get_klines", flaky)

    df = _exchange().ohlcv("BTC/USDT:USDT", "4h")

    assert len(calls) == 2, "a transient stall must be retried"
    assert not df.empty


def test_a_ban_relayed_through_the_proxy_is_NEVER_retried(monkeypatch):
    """The proxy can relay a 418 from Binance. Retrying it re-extends the ban ~22 minutes."""
    calls = []

    def banned(url, params=None, timeout=None):
        calls.append(url)
        raise Exception('418 {"code":-1003,"msg":"Way too many requests; IP banned until 123"}')

    monkeypatch.setattr(ex_mod, "_proxy_get_klines", banned)

    with pytest.raises(Exception, match="1003"):
        _exchange().ohlcv("BTC/USDT:USDT", "4h")

    assert len(calls) == 1, "a ban must cost exactly ONE call"


def test_a_persistent_outage_still_RAISES_so_the_book_is_held(monkeypatch):
    """NO SILENT FALLBACK: a dead proxy must not quietly resume direct Binance calls — that is what
    got this desk IP-banned. The raise is what arms HOLD-ON-DATA-OUTAGE."""
    def dead(url, params=None, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(ex_mod, "_proxy_get_klines", dead)

    with pytest.raises(TimeoutError):
        _exchange().ohlcv("BTC/USDT:USDT", "4h")


def test_the_retry_is_bounded(monkeypatch):
    """100 symbols x unbounded retries would turn a slow proxy into a hung cycle."""
    calls = []

    def dead(url, params=None, timeout=None):
        calls.append(1)
        raise TimeoutError("timed out")

    monkeypatch.setattr(ex_mod, "_proxy_get_klines", dead)

    with pytest.raises(TimeoutError):
        _exchange().ohlcv("BTC/USDT:USDT", "4h")

    assert len(calls) <= RETRYABLE_ATTEMPTS


def test_the_happy_path_makes_exactly_one_call(monkeypatch):
    """Retry must not become a second request on every healthy fetch — that would double the load
    on the very proxy that is stalling."""
    calls = []

    def ok(url, params=None, timeout=None):
        calls.append(1)
        return _ROWS

    monkeypatch.setattr(ex_mod, "_proxy_get_klines", ok)

    _exchange().ohlcv("BTC/USDT:USDT", "4h")

    assert len(calls) == 1


def test_the_proxy_request_is_unchanged(monkeypatch):
    """Wrapping must not alter the URL, params or the symbol->raw-id mapping."""
    seen = {}

    def capture(url, params=None, timeout=None):
        seen["url"] = url
        seen["params"] = dict(params or {})
        return _ROWS

    monkeypatch.setattr(ex_mod, "_proxy_get_klines", capture)

    _exchange().ohlcv("BTC/USDT:USDT", "4h", limit=123)

    assert seen["url"] == "http://127.0.0.1:8000/fapi/v1/klines"
    assert seen["params"] == {"symbol": "BTCUSDT", "interval": "4h", "limit": 123}
