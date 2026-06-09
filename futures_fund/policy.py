from __future__ import annotations

import numpy as np
from pydantic import BaseModel

from futures_fund.models import PortfolioHealth, RegimeQuadrant, RegimeState, RiskCaps

# Healthy-tier base caps per regime quadrant: (max_leverage, per_trade_risk_pct, max_heat).
# AGGRESSIVE WEEKLY envelope (Operation TEMPEST-WEEKLY): the desk targets 5%/WEEK and tolerates
# ~50% drawdown, so caps are far wider than the survival-first monthly desk. Leverage is still an
# OUTPUT of geometry (sizing.choose_leverage searches DOWN from these caps to satisfy the
# liq-distance floor); these are ceilings, never inputs. The deterministic gate remains the sole,
# non-overridable risk authority — only the numbers move, not the mechanism.
_BASE_CAPS: dict[RegimeQuadrant, tuple[float, float, float]] = {
    "low_vol_trend":  (10.0, 0.030, 0.40),
    "high_vol_trend": (10.0, 0.025, 0.35),
    "low_vol_range":  ( 8.0, 0.025, 0.35),
    "high_vol_range": ( 6.0, 0.020, 0.30),
    "transition":     ( 5.0, 0.015, 0.25),
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
    """Hard circuit breakers — AGGRESSIVE WEEKLY posture. Thresholds are fractions (-0.20 = -20%).

    The desk is drawdown-tolerant (accepts ~50%), so the breakers sit far wider than the monthly
    desk: a single -20% drawdown step-down (was -5%) and a -50% drawdown HARD STOP (force-flatten +
    halt-new — the survival floor). Daily/weekly halts are loosened for a 10x intraday book but kept
    as soft brakes; a -40% month is a secondary halt well inside the -50% flatten.
    """
    allow_new = True
    force_flatten = False
    mult = 1.0
    reasons: list[str] = []

    if dd_from_peak >= 0.20:           # step-down: halve risk past -20% from peak (was -5%)
        mult = 0.5
        reasons.append("dd>=20% step-down")
    if dd_from_peak >= 0.50:           # HARD STOP: -50% drawdown force-flatten + halt-new
        allow_new = False
        force_flatten = True
        reasons.append("dd>=50% force-flatten")
    if daily_pnl_pct <= -0.10:         # loosened daily soft brake (was -3%)
        allow_new = False
        reasons.append("daily<=-10% halt-new")
    if weekly_pnl_pct <= -0.20:        # weekly soft brake (was -7%)
        allow_new = False
        reasons.append("weekly<=-20% halt-new")
    if monthly_pnl_pct <= -0.40:       # secondary monthly halt inside the -50% flatten
        allow_new = False
        reasons.append("monthly<=-40% halt-new")
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
