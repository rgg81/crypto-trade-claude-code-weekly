"""Transient network failure must cost a retry, not a whole 4h cycle.

cy368 and cy370 both died on `TimeoutError` mid-gate — two lost cycles out of three DUE ticks. The
gate makes ~20 sequential UNPROXIED calls (mark_price -> fetch_funding_rate per position) and ccxt
defaults to a 10s timeout with no retry, so one slow response under the shared-IP load aborts
everything. State was clean both times (the HOLD path works), but the cycle is still lost.

CRITICAL: a retry must NEVER fire on a rate-limit ban. Retrying a 418/-1003 re-extends it ~22min,
which is the single worst thing this desk can do to itself.
"""
from __future__ import annotations

import pytest

from futures_fund.exchange import RETRYABLE_ATTEMPTS, is_retryable, with_retry


def test_a_timeout_is_retryable():
    assert is_retryable(TimeoutError("timed out"))


def test_a_rate_limit_ban_is_NEVER_retryable():
    """Retrying a ban re-extends it ~22 minutes. This must never happen.

    My first version of this test was VACUOUS: a plain '418 ... IP banned' message contains no
    transport keyword, so the fallback rejects it anyway and the ban check was never exercised
    (removing the check left the test green). The discriminating case is an exception that WOULD
    otherwise qualify — a TimeoutError, or a message carrying a transport word — while also being a
    ban. That is realistic: a request can time out against an already-banned IP, and ccxt wraps
    transport errors around HTTP bodies.
    """
    ban = '418 {"code":-1003,"msg":"Way too many requests; IP banned"}'
    assert not is_retryable(Exception(ban))
    assert not is_retryable(Exception("HTTP 418"))
    assert not is_retryable(Exception("-1003 too many requests"))
    assert not is_retryable(Exception("429 Too Many Requests"))
    # THE discriminating cases — retryable by type/keyword, but banned:
    assert not is_retryable(TimeoutError('418 {"code":-1003,"msg":"IP banned until 123"}')), (
        "a TimeoutError carrying a ban must NOT be retried")
    assert not is_retryable(ConnectionError("429 too many requests")), (
        "a ConnectionError carrying a ban must NOT be retried")
    assert not is_retryable(Exception("connection timed out; IP banned until 123")), (
        "a transport-worded message carrying a ban must NOT be retried")


def test_a_programming_error_is_not_retried():
    assert not is_retryable(KeyError("symbol"))
    assert not is_retryable(ValueError("bad"))


def test_with_retry_returns_on_first_success():
    calls = []

    def ok():
        calls.append(1)
        return "fine"

    assert with_retry(ok, sleep=lambda _: None) == "fine"
    assert len(calls) == 1


def test_with_retry_recovers_from_a_transient_timeout():
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("timed out")
        return "recovered"

    assert with_retry(flaky, sleep=lambda _: None) == "recovered"
    assert len(calls) == 2


def test_with_retry_gives_up_rather_than_hammering():
    calls = []

    def always():
        calls.append(1)
        raise TimeoutError("timed out")

    with pytest.raises(TimeoutError):
        with_retry(always, sleep=lambda _: None)
    assert len(calls) == RETRYABLE_ATTEMPTS, "must be bounded, never unbounded"


def test_with_retry_does_NOT_retry_a_ban_even_once():
    calls = []

    def banned():
        calls.append(1)
        raise Exception('418 {"code":-1003,"msg":"IP banned until 123"}')

    with pytest.raises(Exception, match="418"):
        with_retry(banned, sleep=lambda _: None)
    assert len(calls) == 1, "a ban must fail immediately — retrying re-extends it ~22min"


def test_the_unproxied_per_symbol_calls_are_wrapped():
    """The specific burst that cost cy368/cy370: one fetch_funding_rate per position during the
    gate. Assert the call sites route through with_retry rather than calling the client raw —
    a source check, but the behaviour is already pinned by the with_retry tests above."""
    import ast
    import inspect

    import futures_fund.exchange as ex
    src = inspect.getsource(ex)
    tree = ast.parse(src)
    for fname in ("funding", "mark_price", "symbol_spec"):
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == fname)
        body = ast.dump(fn)
        assert "with_retry" in body, f"{fname} makes an unguarded network call"


def test_client_timeout_is_raised_above_the_ccxt_default():
    """ccxt defaults to 10s; the gate's ~20 sequential calls under shared-IP load need more."""
    from futures_fund.exchange import CLIENT_TIMEOUT_MS
    assert CLIENT_TIMEOUT_MS >= 30_000
