"""One place to answer "what per-trade risk will the gate ACTUALLY size with?".

`risk_gate.py:88` sizes every trade as

    risk_pct = caps.per_trade_risk_pct * breaker.risk_multiplier * rm

and `caps_for` has already halved `per_trade_risk_pct` at the `caution` health tier (zeroed at
`stressed`). Any component that models the gate's sizing must apply BOTH factors, or it models a
book the gate will never build.

Two independent copies of that model had drifted, both only once a drawdown engaged the ladder:

* `orchestration` fed the pre-sizer the RAW caps ptr, so from cy291 (dd 5.59%) every leg realised
  HALF its intended risk and the dust check passed legs the gate then halved under consolidate's
  0.001 floor, where they vanished silently (BTC at cy294; BTC+ETH at cy293).
* `blended_book_cli` hardcoded ptr from the regime quadrant at the HEALTHY tier, ignoring both the
  tier halving and the breaker — up to 4x overstated. It feeds `deployment_resizes`, whose leg
  ceilings then look 4x too generous, so legs are flagged for close+reopen, reopen at their true
  (smaller) size, and are flagged again next cycle: a permanent 4h churn loop burning the 0.14%
  round-trip on most of the book, precisely while in drawdown (HARD RULE 2).

Keeping the derivation here means the next change to `policy` updates one model, not three.
"""
from __future__ import annotations

from futures_fund.policy import circuit_breaker

_TIER_FACTOR = {"healthy": 1.0, "caution": 0.5, "stressed": 0.0}


def breaker_multiplier(drawdown_from_peak: float, daily_pnl_pct: float = 0.0,
                       weekly_pnl_pct: float = 0.0, monthly_pnl_pct: float = 0.0) -> float:
    """The multiplier `risk_gate` applies on top of the caps.

    The period returns are passed through rather than assumed: `risk_multiplier` happens to depend
    only on drawdown today, but `policy` is PROTECTED and may legitimately change — hardcoding
    zeros here would silently re-open the very divergence this module exists to close.
    """
    return circuit_breaker(daily_pnl_pct, weekly_pnl_pct, monthly_pnl_pct,
                           max(0.0, drawdown_from_peak)).risk_multiplier


def tier_factor(health_tier: str | None) -> float:
    """How `caps_for` scales per-trade risk for the portfolio-health tier."""
    return _TIER_FACTOR.get((health_tier or "healthy").lower(), 1.0)


def effective_per_trade_risk(base_ptr: float, *, drawdown_from_peak: float = 0.0,
                             health_tier: str | None = None,
                             tier_already_applied: bool = False,
                             daily_pnl_pct: float = 0.0, weekly_pnl_pct: float = 0.0,
                             monthly_pnl_pct: float = 0.0) -> float:
    """The per-trade risk the gate will really size with.

    `tier_already_applied=True` when `base_ptr` came from `caps_for(...)`, which has already scaled
    for the tier — applying it twice would under-size the book by another 2x.
    """
    ptr = max(0.0, base_ptr)
    if not tier_already_applied:
        ptr *= tier_factor(health_tier)
    return ptr * breaker_multiplier(drawdown_from_peak, daily_pnl_pct,
                                    weekly_pnl_pct, monthly_pnl_pct)
