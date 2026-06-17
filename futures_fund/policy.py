from __future__ import annotations

import numpy as np
from pydantic import BaseModel

from futures_fund.models import PortfolioHealth, RegimeQuadrant, RegimeState, RiskCaps

# Healthy-tier base caps per regime quadrant: (max_leverage, per_trade_risk_pct, max_heat).
# CONSERVATIVE DOLLAR-NEUTRAL envelope (Operation TEMPEST-NEUTRAL): the desk targets ~3%/MONTH on a
# dollar-neutral long/short book at LITERAL 1x PER POSITION (full isolated margin, so liquidation is
# effectively unreachable — liq sits ~95% away, trivially clearing the 2.5x floor). max_leverage is
# pinned to 1.0 across every quadrant; the dollar-neutral pre-sizer (futures_fund/neutral_book.py)
# SHRINKS risk_mult toward a ~equity/2-per-side notional target, so gross stays ~1x of equity. These
# are ceilings, never inputs; the deterministic gate remains the sole, non-overridable risk
# authority. NOTE: position_risk/consolidate sum gross stop-risk and do NOT credit the long/short
# offset, so max_heat is the binding deployment ceiling for a balanced book.
_BASE_CAPS: dict[RegimeQuadrant, tuple[float, float, float]] = {
    "low_vol_trend":  (1.0, 0.015, 0.10),
    "high_vol_trend": (1.0, 0.010, 0.08),
    "low_vol_range":  (1.0, 0.010, 0.08),
    "high_vol_range": (1.0, 0.005, 0.04),
    "transition":     (1.0, 0.005, 0.04),
}


def caps_for(regime: RegimeState, health: PortfolioHealth) -> RiskCaps:
    """Adaptive caps from the regime × portfolio-health matrix (spec §7.1)."""
    lev, risk, heat = _BASE_CAPS[regime.quadrant]
    bias = "reduce" if regime.quadrant == "transition" else "normal"
    tier = health.tier

    if tier == "stressed":
        return RiskCaps(max_leverage=1.0, per_trade_risk_pct=0.0, max_heat=0.0, bias="flat")
    if tier == "caution":
        lev *= 0.5
        risk *= 0.5
        heat *= 0.5
        bias = "reduce"
    return RiskCaps(max_leverage=lev, per_trade_risk_pct=risk, max_heat=heat, bias=bias)


class BreakerState(BaseModel):
    allow_new_entries: bool
    force_flatten: bool
    risk_multiplier: float
    reason: str = ""


def circuit_breaker(
    daily_pnl_pct: float, weekly_pnl_pct: float, monthly_pnl_pct: float, dd_from_peak: float
) -> BreakerState:
    """Hard circuit breakers — CONSERVATIVE DOLLAR-NEUTRAL posture. Thresholds are fractions
    (-0.15 = -15%).

    Progressive de-risk for a ~1x dollar-neutral book targeting ~3%/MONTH: a -5% drawdown step-down
    (halve risk), a -10% reduce-only (stop new opens, hold/trim — a balanced book down 10% has a
    correlation/beta breakdown), and the -15% drawdown HARD STOP (force-flatten + halt-new — the
    user-set survival floor). Daily/weekly soft brakes are tight (-3%/-7%); parent -12% calendar-
    month force-flatten is kept as an additive secondary (both strengthen — neither weakens).
    """
    allow_new = True
    force_flatten = False
    mult = 1.0
    reasons: list[str] = []

    if dd_from_peak >= 0.05:           # step-down: halve risk past -5% from peak
        mult = 0.5
        reasons.append("dd>=5% step-down")
    if dd_from_peak >= 0.10:           # reduce-only: no new opens past -10% (hold/trim existing)
        allow_new = False
        mult = min(mult, 0.25)
        reasons.append("dd>=10% reduce-only")
    if dd_from_peak >= 0.15:           # HARD STOP: -15% drawdown force-flatten + halt-new
        allow_new = False
        force_flatten = True
        reasons.append("dd>=15% force-flatten")
    if daily_pnl_pct <= -0.03:         # daily soft brake
        allow_new = False
        reasons.append("daily<=-3% halt-new")
    if weekly_pnl_pct <= -0.07:        # weekly soft brake
        allow_new = False
        reasons.append("weekly<=-7% halt-new")
    if monthly_pnl_pct <= -0.12:       # additive secondary calendar-month force-flatten
        allow_new = False
        force_flatten = True
        reasons.append("monthly<=-12% force-flatten")
    return BreakerState(allow_new_entries=allow_new, force_flatten=force_flatten,
                        risk_multiplier=mult, reason="; ".join(reasons))


def cvar(returns: list[float], alpha: float = 0.05) -> float:
    """Conditional VaR (expected shortfall): mean of the worst `alpha` fraction of returns.

    Returns 0.0 if there are no observations. More negative = worse tail.
    """
    if not returns:
        return 0.0
    arr = np.sort(np.asarray(returns, dtype=float))
    k = max(1, int(np.ceil(alpha * len(arr))))
    return float(arr[:k].mean())
