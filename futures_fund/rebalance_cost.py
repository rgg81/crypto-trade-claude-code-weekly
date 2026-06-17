"""Cost-aware rebalance gate (NON-protected, ADVISORY). Every 4h the desk decides a target
dollar-neutral book; a FULL re-strike is expensive, so this gate answers per leg: is realigning
current->target worth the turnover cost? Calibrated from the REAL modeled cost (costs.TAKER_RATE
+ slippage) plus a no-trade DRIFT BAND so the book is not churned for noise. Funding is priced
with the CORRECT signed sign via costs.project_funding (carry collected = a CREDIT that RAISES the
realignment edge — never a positive cost that suppresses exactly the carry trades that earn).

It NEVER sizes or vetoes (the gate owns per-trade safety; neutral_book owns the dollar-neutral
targets). The CIO reads the verdict; a HOLD here OVERRIDES a pacing PRESS (neutrality + cost
discipline win over tempo — pressing a thin book only pays fees faster).

Turnover budget (RC1): with ~180 4h cycles/month and a 0.14% round-trip per full re-strike, a budget
of <= ~4 full re-strikes/month keeps fee drag <= ~0.6%/month (<= ~20% of 3%). The drift band
(default 15%) is the per-leg throttle that enforces this without a global counter.
"""
from __future__ import annotations

from futures_fund.costs import TAKER_RATE, project_funding

_DEFAULT_SLIPPAGE_BPS = 2.0   # matches cycle._SLIPPAGE_BPS; one taker fill to move a leg
_DEFAULT_DRIFT_BAND = 0.15    # don't rebalance a leg whose target-vs-current notional drift < 15%


def realignment_edge_usd(target_notional: float, *, price_edge_bps: float, funding_rate: float,
                         direction: str, funding_events: int = 1) -> float:
    """Expected $ the realignment captures over the hold: the price/dispersion edge PLUS the SIGNED
    funding carry (a CREDIT when we collect, a cost when we pay). funding_rate is the per-leg signed
    rate (Binance: positive => longs pay shorts). project_funding returns positive=pay, so
    the PnL credit is its negation."""
    price = abs(target_notional) * price_edge_bps / 10_000.0
    funding_pnl = -project_funding(abs(target_notional), funding_rate, direction, funding_events)
    return price + funding_pnl


def should_rebalance(current_notional: float, target_notional: float, expected_edge_usd: float, *,
                     slippage_bps: float = _DEFAULT_SLIPPAGE_BPS,
                     drift_band: float = _DEFAULT_DRIFT_BAND) -> dict:
    """Advisory verdict for realigning a leg's notional current->target.

    HOLD when the target-vs-current drift is below the no-trade band (don't churn for noise) OR when
    the turnover cost (the traded delta x one taker fill incl. slippage) exceeds the expected edge.
    REBALANCE only when drift clears the band AND the edge beats the cost. Returns the arithmetic
    so the CIO can show its work.
    """
    traded = abs(target_notional - current_notional)
    denom = max(abs(target_notional), abs(current_notional), 1e-9)
    drift = traded / denom
    cost_per_fill = TAKER_RATE + slippage_bps / 10_000.0
    cost = traded * cost_per_fill
    net = expected_edge_usd - cost
    if drift < drift_band:
        action = "hold"
    elif net > 0:
        action = "rebalance"
    else:
        action = "hold"
    return {"action": action, "traded": traded, "drift": drift, "cost": cost,
            "expected_edge_usd": expected_edge_usd, "net": net}
