from __future__ import annotations

import json

import pandas as pd

from futures_fund.config import Settings
from futures_fund.market_data import (
    FundingInfo,
    _filter_field,
    klines_to_ccxt_rows,
    parse_funding,
    parse_long_short_ratio,
    parse_ohlcv,
    parse_open_interest_history,
    parse_symbol_spec,
)
from futures_fund.models import MmrBracket, SymbolSpec

# NETWORK RESILIENCE. cy368 and cy370 both died on a mid-gate TimeoutError — two lost cycles out of
# three DUE ticks. The gate makes ~20 sequential UNPROXIED calls (mark_price -> fetch_funding_rate,
# one per position) and ccxt defaults to a 10s timeout with no retry, so a single slow response
# under the shared-IP load aborts the whole cycle. State stayed clean (the HOLD path works), but the
# cycle is still lost.
CLIENT_TIMEOUT_MS = 30_000
RETRYABLE_ATTEMPTS = 2
_RETRY_BACKOFF_S = 1.5
# A rate-limit BAN must never be retried: each call during one re-extends it ~22 minutes,
# the single worst thing this desk can do to itself. Only transient transport faults qualify.
_BAN_MARKERS = ("418", "-1003", "429", "too many requests", "banned")


def is_retryable(exc: BaseException) -> bool:
    """True only for a transient TRANSPORT fault — never for a ban, never for a bug."""
    msg = str(exc).lower()
    if any(m in msg for m in _BAN_MARKERS):
        return False
    if isinstance(exc, TimeoutError | ConnectionError):
        return True
    return any(w in msg for w in ("timed out", "timeout", "connection reset",
                                  "connection aborted", "temporarily unavailable"))


def with_retry(fn, *, attempts: int = RETRYABLE_ATTEMPTS, sleep=None):
    """Call `fn`, retrying ONLY transient transport faults, at most `attempts` times total."""
    import time as _time
    _sleep = sleep if sleep is not None else _time.sleep
    last: BaseException | None = None
    for i in range(max(1, attempts)):
        try:
            return fn()
        except BaseException as exc:  # noqa: BLE001 — re-raised below unless retryable
            last = exc
            if not is_retryable(exc) or i == max(1, attempts) - 1:
                raise
            _sleep(_RETRY_BACKOFF_S * (i + 1))
    raise last  # pragma: no cover - loop always returns or raises


def build_ccxt(settings: Settings):
    """Construct a ccxt binanceusdm client. Imported lazily so the test suite never needs
    ccxt's network stack.

    - Paper (settings.live is False, the default): a PUBLIC mainnet client — real market data,
      no API keys, no orders. Binance has deprecated ccxt's futures testnet/sandbox, so paper
      trading uses real mainnet data with in-process simulated execution (never sandbox).
    - Live (settings.live is True): an authenticated mainnet client for real orders.
    """
    import ccxt

    config: dict = {"enableRateLimit": True, "timeout": CLIENT_TIMEOUT_MS}
    if settings.live:
        if not settings.exchange.api_key or not settings.exchange.api_secret:
            raise ValueError(
                "live=True requires BINANCE_KEY/BINANCE_SECRET; refusing to build a live client "
                "without authenticated credentials (would also fail leverage-tier / order calls)."
            )
        config["apiKey"] = settings.exchange.api_key
        config["secret"] = settings.exchange.api_secret
    return ccxt.binanceusdm(config)


def default_symbol_spec(market: dict) -> SymbolSpec:
    """Build a SymbolSpec from PUBLIC exchangeInfo only (no leverage tiers). Used in paper/
    keyless mode, where the authenticated leverage-tiers endpoint is unavailable. Uses a single
    conservative MMR bracket (5% maintenance, 20x cap) so the risk gate computes a deliberately
    cautious liquidation price; real per-tier MMR is used whenever keys are present (live)."""
    filters = (market.get("info") or {}).get("filters") or []
    tick = _filter_field(filters, "PRICE_FILTER", "tickSize")
    step = _filter_field(filters, "LOT_SIZE", "stepSize")
    mn = _filter_field(filters, "MIN_NOTIONAL", "notional")
    if tick is None:
        tick = float(market["precision"]["price"])
    if step is None:
        step = float(market["precision"]["amount"])
    if mn is None:
        mn = float((market.get("limits", {}).get("cost", {}) or {}).get("min") or 5.0)
    return SymbolSpec(
        symbol=market["id"], tick_size=float(tick), step_size=float(step), min_notional=float(mn),
        mmr_brackets=[MmrBracket(notional_floor=0.0, notional_cap=1e12, mmr=0.05,
                                 maint_amount=0.0, max_leverage=20.0)],
    )


def _proxy_get_klines(url: str, params: dict | None = None, timeout: float | None = None):
    """GET the proxy's /fapi/v1/klines. Split out so tests can substitute it without a socket."""
    import urllib.parse
    import urllib.request

    q = urllib.parse.urlencode(params or {})
    with urllib.request.urlopen(f"{url}?{q}", timeout=timeout or 30) as r:  # noqa: S310
        return json.loads(r.read().decode())


class FuturesExchange:
    """Thin wrapper over a ccxt-like client. Inject a fake client in tests."""

    def __init__(self, client, keyless: bool = False, klines_proxy_url: str = ""):
        self.client = client
        # When set, ALL candle fetches go through the local binance-proxy instead of Binance.
        self.klines_proxy_url = (klines_proxy_url or "").rstrip("/")
        # keyless: leverage tiers (an authenticated endpoint) are unavailable, so symbol_spec
        # falls back to a conservative default bracket. True for paper; False for live.
        self.keyless = keyless

    @classmethod
    def from_settings(cls, settings: Settings) -> FuturesExchange:
        ex = build_ccxt(settings)
        ex.load_markets()
        return cls(ex, keyless=not settings.live,
                   klines_proxy_url=settings.exchange.klines_proxy_url)

    def _raw_id(self, symbol: str) -> str:
        return self.client.market(symbol)["id"]

    def unified_for_raw(self, raw_id: str) -> str | None:
        """Map a stored raw exchange id (e.g. 'BTCUSDT') back to its ccxt unified symbol
        (e.g. 'BTC/USDT:USDT'). Used to fold carried positions into the working universe so a
        held symbol outside this cycle's Watcher picks is never stranded unaudited. None if the
        market is unknown."""
        by_id = getattr(self.client, "markets_by_id", None)
        if by_id and raw_id in by_id:
            m = by_id[raw_id]
            return (m[0] if isinstance(m, list) else m)["symbol"]
        for sym, mk in getattr(self.client, "markets", {}).items():
            if mk.get("id") == raw_id:
                return sym
        return None

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        market = self.client.market(symbol)
        if self.keyless:
            return default_symbol_spec(market)  # paper: no auth for leverage tiers
        tiers = with_retry(lambda: self.client.fetch_leverage_tiers([symbol]))[symbol]
        return parse_symbol_spec(market, tiers)

    def ohlcv(self, symbol: str, timeframe: str = "4h", limit: int = 500) -> pd.DataFrame:
        """Candles, via the local binance-proxy when one is configured.

        NO SILENT FALLBACK. If the proxy is unreachable or its circuit breaker is open (503 +
        Retry-After), this RAISES. Quietly resuming direct Binance calls is precisely the behaviour
        that got the desk IP-banned, so it is forbidden; auto_cycle's HOLD-ON-DATA-OUTAGE path
        already turns a raised fetch into a safely-held book.
        """
        if self.klines_proxy_url:
            raw = _proxy_get_klines(
                f"{self.klines_proxy_url}/fapi/v1/klines",
                params={"symbol": self._raw_id(symbol), "interval": timeframe, "limit": limit},
            )
            return parse_ohlcv(klines_to_ccxt_rows(raw))
        return parse_ohlcv(self.client.fetch_ohlcv(symbol, timeframe, None, limit))

    def funding(self, symbol: str) -> FundingInfo:
        # UNPROXIED and called once per symbol — the burst that cost cy368/cy370. A transient
        # timeout here is retried once; a BAN is never retried (see is_retryable).
        fr = with_retry(lambda: self.client.fetch_funding_rate(symbol))
        try:
            interval = self.client.fetch_funding_interval(symbol)
        except Exception:
            interval = None  # symbol uses default 8h, or endpoint unavailable
        return parse_funding(fr, interval)

    def open_interest_history(
        self, symbol: str, period: str = "4h", limit: int = 200
    ) -> pd.DataFrame:
        return parse_open_interest_history(
            self.client.fetch_open_interest_history(symbol, period, None, limit)
        )

    def long_short_ratio(self, symbol: str, period: str = "4h", limit: int = 200) -> pd.DataFrame:
        # implicit fapiData endpoint takes the RAW binance id, not the unified symbol
        raw = self.client.fapiDataGetGlobalLongShortAccountRatio(
            {"symbol": self._raw_id(symbol), "period": period, "limit": limit}
        )
        return parse_long_short_ratio(raw)

    def mark_prices(self, symbols) -> dict[str, float]:
        """Marks for many symbols in ONE call.

        WHY THIS EXISTS. Gate timeouts clustered with total precision at Binance's 8-hourly funding
        settlement — 00:00/08:00/16:00 UTC failed (cy368, cy370, cy372, cy374), 04:00/12:00/20:00
        succeeded (cy366/367/369/371/373); 4-for-4 and 5-for-5. The book was priced with ~20
        SEQUENTIAL fetch_funding_rate calls, hitting the funding endpoint exactly when every perp on
        the venue settles and the neighbour fleet recalculates. One batch call collapses that window
        and stops this desk adding to the same spike.

        A symbol the venue omits is ABSENT from the result, never 0.0 — pricing a live position at
        nothing is far worse than reporting it unpriceable.
        """
        wanted = [s for s in (symbols or []) if s]
        if not wanted:
            return {}
        batch = getattr(self.client, "fetch_mark_prices", None)
        if callable(batch):
            raw = with_retry(lambda: batch(wanted)) or {}
            out = {}
            for sym in wanted:
                row = raw.get(sym) or {}
                px = row.get("markPrice") if isinstance(row, dict) else None
                if px is not None:
                    try:
                        out[sym] = float(px)
                    except (TypeError, ValueError):
                        continue
            return out
        return {s: self.mark_price(s) for s in wanted}

    def mark_price(self, symbol: str) -> float:
        return float(with_retry(lambda: self.client.fetch_funding_rate(symbol))["markPrice"])
