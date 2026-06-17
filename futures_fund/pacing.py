"""Monthly risk-pacing engine (Pillar 1 — DEPLOY: pursue ~3%/MONTH, conservatively).

Operation TEMPEST-NEUTRAL targets ~3% per MONTH net of costs on a ~1x-gross dollar-neutral book.
`policy.circuit_breaker` only ever DE-risks; nothing scales deployment UP when the desk is BEHIND
the monthly target. This module adds the missing upward pressure, SAFELY:

- Rolling calendar-month pacing: month-to-date return vs the pro-rated 3%/month pace (month starts
  on the 1st, 00:00 UTC).
- START SOFT in the first few days of the month; PRESS (deploy more of the BALANCED book) when
  BEHIND pace AND under-deployed AND NOT in drawdown; THROTTLE once the monthly target is hit.
- ANTI-MARTINGALE (hard invariant): being behind because of DRAWDOWN never presses — the protected
  drawdown breakers own the loss path; pacing only spends UNUSED budget. `drawdown >= CAUTION_DD`
  (5%, mirroring the policy step-down) forces mode <= soft.
- 3%/month is a CEILING the dollar-neutral edge must clear, NOT a floor to force: PRESS only fills
  the EXISTING gate-enforced caps more fully on a balanced book; a cost-aware rebalance HOLD verdict
  (rebalance_cost.py) OVERRIDES a PRESS directive (neutrality + cost discipline win over
  tempo — pressing a thin neutral book just pays fees faster).

It is ADVISORY/UTILIZATION-only: it raises how fully the desk uses its EXISTING gate-enforced caps
(more setups, fuller `risk_mult` toward 1.0) — it NEVER raises a cap and NEVER touches a protected
module (risk_gate/policy/sizing). The risk gate still clamps `risk_mult` to (0,1], so pacing can
never increase risk above the survival cap.
"""
from __future__ import annotations

import calendar
import math
from dataclasses import dataclass
from datetime import datetime

CAUTION_DD = 0.05      # drawdown at/above this -> NEVER press (mirrors policy caution / step-down)
SOFT_DAYS = 3.0        # first ~3 days of the month: start soft, preserve budget/optionality
PRESS_GAP = 0.005      # behind pro-rated pace by more than this (0.5% equity) -> press-eligible
PRESS_UTIL_HEAT = 0.06  # open heat below this (6% at risk) -> under-deployed, room to deploy
DAYS_IN_MONTH = 30     # fallback only; pacing_state uses calendar.monthrange for the real count

_MODE_RISK_MULT = {"throttle": 0.5, "soft": 0.5, "normal": 0.85, "press": 1.0}
_MODE_APPETITE = {"throttle": 0.25, "soft": 0.4, "normal": 0.7, "press": 0.95}


@dataclass
class PacingState:
    mode: str               # 'soft' | 'normal' | 'press' | 'throttle'
    appetite: float         # 0..1 deployment appetite
    suggested_risk_mult: float
    mtd_return: float       # month-to-date return vs the month-start anchor
    pace: float             # pro-rated target for days elapsed in the month
    pace_gap: float         # mtd_return - pace (negative = behind)
    drawdown: float
    open_heat: float
    in_drawdown: bool
    days_elapsed: float
    days_in_month: int
    directive: str          # human guidance injected into the team's prompts


def _directive(mode: str, pace_gap: float, mtd: float, target: float) -> str:
    if mode == "throttle":
        return (f"THROTTLE — MTD {mtd:+.1%} has reached the {target:.0%}/month target. Be "
                f"selective, protect the month; only rebalance for a clear, cost-positive edge.")
    if mode == "press":
        return (f"PRESS — behind the monthly pace by {pace_gap:+.1%} with unused budget, no "
                f"drawdown. DEPLOY MORE OF THE BALANCED BOOK: fill the dollar-neutral long/short "
                f"sleeves toward their gross target (risk_mult up toward 1.0) on edges that clear "
                f"the net-of-cost bar. NEVER tilt one-sided to chase pace, and a cost-aware "
                f"rebalance HOLD OVERRIDES this press — {target:.0%}/month is a CEILING the "
                f"edge must clear, not a quota; pressing a thin book only pays fees faster.")
    if mode == "soft":
        return ("SOFT — start the month conservatively (early month, or in drawdown, defer to the "
                "breakers). Take only clean, balanced, high-conviction pairs.")
    return ("NORMAL — roughly on the monthly pace. Hold the balanced book; rebalance only for "
            "cost-positive edge improvements toward the ~3%/month target.")


def compute_pacing(*, mtd_return: float, days_elapsed: float, days_in_month: int = DAYS_IN_MONTH,
                   drawdown: float, open_heat: float, monthly_target: float = 0.03) -> PacingState:
    """Pure pacing logic (no I/O). See module docstring for the safety invariants."""
    dim = max(1, int(days_in_month))
    pace = monthly_target * (max(0.0, days_elapsed) / dim)
    pace_gap = mtd_return - pace
    in_dd = drawdown >= CAUTION_DD

    if mtd_return >= monthly_target:
        mode = "throttle"
    elif in_dd:
        mode = "soft"                       # ANTI-MARTINGALE: drawdown never presses
    elif days_elapsed < SOFT_DAYS:
        mode = "soft"                       # start the month soft
    elif pace_gap <= -PRESS_GAP and open_heat < PRESS_UTIL_HEAT:
        mode = "press"                      # behind + under-deployed + healthy -> deploy
    else:
        mode = "normal"

    return PacingState(
        mode=mode, appetite=_MODE_APPETITE[mode], suggested_risk_mult=_MODE_RISK_MULT[mode],
        mtd_return=mtd_return, pace=pace, pace_gap=pace_gap, drawdown=drawdown, open_heat=open_heat,
        in_drawdown=in_dd, days_elapsed=days_elapsed, days_in_month=dim,
        directive=_directive(mode, pace_gap, mtd_return, monthly_target),
    )


def _month_start(now: datetime) -> datetime:
    """The 1st-of-month 00:00 anchor of the calendar month containing `now` (same tz as `now`)."""
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def pacing_state(state_dir, now: datetime, health, *, monthly_target: float = 0.03) -> PacingState:
    """Compute the rolling calendar-month pacing state from the equity log + live portfolio health.

    `health` supplies `drawdown_from_peak` and `open_heat`. Month-to-date return is measured vs the
    last equity point at/before the 1st-of-month-00:00 anchor. If the desk started MID-MONTH (no
    pre-anchor point), the return base AND pace proration both anchor to the FIRST in-month
    point — so the monthly target is never prorated over days the desk had no chance to trade (which
    would fabricate a catch-up gap and a spurious PRESS). FAIL-SAFE: with <2 equity points TOTAL (no
    basis) -> SOFT (conservative default); early-month softness is enforced separately by the
    days_elapsed<SOFT_DAYS branch in compute_pacing."""
    from futures_fund.equity_log import equity_series
    dd = float(getattr(health, "drawdown_from_peak", 0.0) or 0.0)
    heat = float(getattr(health, "open_heat", 0.0) or 0.0)
    anchor_ts = _month_start(now)
    dim = calendar.monthrange(now.year, now.month)[1]
    days_elapsed = (now - anchor_ts).total_seconds() / 86400.0

    series = []
    for ts, eq in equity_series(state_dir):
        try:
            series.append((datetime.fromisoformat(ts), float(eq)))
        except (ValueError, TypeError):
            continue
    if len(series) < 2:
        return compute_pacing(mtd_return=0.0, days_elapsed=min(days_elapsed, SOFT_DAYS - 0.01),
                              days_in_month=dim, drawdown=dd, open_heat=heat,
                              monthly_target=monthly_target)
    at_or_before = [eq for ts, eq in series if ts <= anchor_ts]
    if at_or_before:
        base = at_or_before[-1]
        elapsed = days_elapsed            # true month-to-date from the 1st-of-month anchor
    else:
        # mid-month start: anchor pace proration to the first in-month point, not the 1st, so the
        # desk isn't measured against days it never traded (the spurious-PRESS footgun).
        base_ts, base = series[0]
        elapsed = max(0.0, (now - base_ts).total_seconds() / 86400.0)
    # MTD numerator = the LIVE M2M equity (fresh from portfolio_health), NOT the last LOGGED point.
    # The equity log is appended only when the gate runs (per strategic cycle), so at preflight
    # series[-1] is the PRIOR cycle — up to a full cycle stale. A sharp intra-cycle move against the
    # book would otherwise mis-state MTD and mis-shape the CIO/Trader. drawdown/heat in this same
    # call already come from the fresh `health`, so taking `last` from it too keeps inputs
    # consistent. FAIL-SAFE to the last logged point if `health` carries no finite positive equity.
    he = getattr(health, "equity", None)
    last = float(he) if (isinstance(he, (int, float)) and math.isfinite(he) and he > 0) \
        else series[-1][1]
    mtd = (last / base - 1.0) if base > 0 else 0.0
    return compute_pacing(mtd_return=mtd, days_elapsed=elapsed, days_in_month=dim,
                          drawdown=dd, open_heat=heat, monthly_target=monthly_target)
