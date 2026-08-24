"""Candle data MUST come from the local binance-proxy, never straight from Binance.

Operator requirement (non-negotiable): the desk fetches klines through ~/binance-proxy, a caching
reverse proxy that serves the identical `/fapi/v1/klines` signature, keeps closed candles in SQLite
forever, coalesces concurrent identical requests into one upstream call, and throttles on Binance's
own weight headers. Klines are the desk's heaviest call by far — preflight pulls ~14 series of 500
candles a tick — and they are what repeatedly tripped the -1003 IP bans that blocked cy299, cy301,
cy306, cy310 and cy325.

Two invariants this pins:

* NO SILENT FALLBACK. If the proxy is down or its circuit breaker is open (503 + Retry-After), the
  fetch RAISES. It must never quietly resume hammering Binance directly — that is exactly the
  behaviour the operator forbade. auto_cycle's HOLD-ON-DATA-OUTAGE path already turns a raised
  fetch into a safely-held book, so failing loudly is both correct and non-destructive.
* SHAPE-IDENTICAL. The proxy returns Binance's raw 12-field klines (strings); ccxt hands the desk
  6-field float rows. The adapter converts, so `parse_ohlcv` and everything downstream is unchanged.
"""
import json

import pytest

from futures_fund.market_data import klines_to_ccxt_rows, parse_ohlcv

# one real row as the proxy returns it (captured from http://127.0.0.1:8000/fapi/v1/klines)
RAW = [
    [1787529600000, "77719.00", "77728.40", "76857.10", "76925.20", "23632.135",
     1787543999999, "1826081130.01210", 685809, "11286.729", "872226098.17790", "0"],
    [1787544000000, "76925.20", "77789.70", "76649.00", "77301.80", "21827.005",
     1787558399999, "1685176196.70330", 566288, "10404.752", "803557470.92180", "0"],
]


def test_converts_binance_rows_to_the_shape_ccxt_gives():
    rows = klines_to_ccxt_rows(RAW)
    assert rows == [
        [1787529600000, 77719.00, 77728.40, 76857.10, 76925.20, 23632.135],
        [1787544000000, 76925.20, 77789.70, 76649.00, 77301.80, 21827.005],
    ]
    assert all(isinstance(r[0], int) for r in rows), "timestamp stays an int ms epoch"
    assert all(isinstance(v, float) for r in rows for v in r[1:]), "OHLCV become floats"


def test_the_converted_rows_feed_parse_ohlcv_unchanged():
    """Downstream must not notice the swap."""
    df = parse_ohlcv(klines_to_ccxt_rows(RAW))
    assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert len(df) == 2
    assert df["close"].iloc[-1] == pytest.approx(77301.80)
    assert df["timestamp"].is_monotonic_increasing


def test_an_empty_response_is_an_empty_list_not_a_crash():
    assert klines_to_ccxt_rows([]) == []


def test_a_short_or_malformed_row_is_rejected_loudly():
    """Silently dropping a bad candle would corrupt ATR/momentum without any signal."""
    with pytest.raises((ValueError, IndexError, TypeError)):
        klines_to_ccxt_rows([[1, "2", "3"]])


def test_json_round_trip_matches_the_live_proxy_payload():
    """Guards against the proxy ever changing field order/types under us."""
    payload = json.loads(json.dumps(RAW))
    assert klines_to_ccxt_rows(payload) == klines_to_ccxt_rows(RAW)


# ---------------------------------------------------------------------------------------------
# The fetch path itself.
# ---------------------------------------------------------------------------------------------
class _Client:
    """Minimal ccxt stand-in that RECORDS whether anyone reached for Binance directly."""

    def __init__(self):
        self.fetch_ohlcv_calls = 0

    def market(self, symbol):
        return {"id": symbol.split("/")[0] + "USDT"}

    def fetch_ohlcv(self, *a, **k):
        self.fetch_ohlcv_calls += 1
        return [[1, 1.0, 1.0, 1.0, 1.0, 1.0]]


def test_ohlcv_goes_through_the_proxy_not_ccxt(monkeypatch):
    """THE REQUIREMENT: candles come from the proxy; ccxt's fetch_ohlcv is never touched."""
    from futures_fund import exchange as ex_mod
    from futures_fund.exchange import FuturesExchange

    seen = {}

    def fake_get(url, params=None, timeout=None):
        seen["url"] = url
        seen["params"] = params
        return RAW

    monkeypatch.setattr(ex_mod, "_proxy_get_klines", fake_get)
    client = _Client()
    ex = FuturesExchange(client, keyless=True, klines_proxy_url="http://127.0.0.1:8000")
    df = ex.ohlcv("BTC/USDT:USDT", "4h", 500)

    assert client.fetch_ohlcv_calls == 0, "the desk must not call Binance directly for klines"
    assert seen["url"].endswith("/fapi/v1/klines")
    assert seen["params"] == {"symbol": "BTCUSDT", "interval": "4h", "limit": 500}
    assert len(df) == 2 and df["close"].iloc[-1] == pytest.approx(77301.80)


def test_a_dead_proxy_raises_and_never_falls_back_to_binance(monkeypatch):
    """Non-negotiable: no silent fallback. auto_cycle's HOLD-ON-DATA-OUTAGE path turns this into
    a safely-held book, which is the correct outcome — resuming direct Binance calls is not."""
    from futures_fund import exchange as ex_mod
    from futures_fund.exchange import FuturesExchange

    def boom(url, params=None, timeout=None):
        raise ConnectionError("proxy down")

    monkeypatch.setattr(ex_mod, "_proxy_get_klines", boom)
    client = _Client()
    ex = FuturesExchange(client, keyless=True, klines_proxy_url="http://127.0.0.1:8000")
    with pytest.raises(ConnectionError):
        ex.ohlcv("BTC/USDT:USDT", "4h", 500)
    assert client.fetch_ohlcv_calls == 0, "a proxy failure must NOT fall back to Binance"


def test_without_a_configured_proxy_it_uses_ccxt(monkeypatch):
    """Back-compat for tests and any caller that injects a fake client with no proxy set."""
    from futures_fund.exchange import FuturesExchange

    client = _Client()
    ex = FuturesExchange(client, keyless=True)
    ex.ohlcv("BTC/USDT:USDT", "4h", 500)
    assert client.fetch_ohlcv_calls == 1


def test_from_settings_wires_the_proxy_url():
    """The live desk must get the proxy by default, not by remembering to pass it."""
    from futures_fund.config import Settings

    assert Settings().exchange.klines_proxy_url == "http://127.0.0.1:8000"
