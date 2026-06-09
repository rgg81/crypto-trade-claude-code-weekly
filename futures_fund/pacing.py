"""Weekly risk-pacing engine (Pillar 1 — DEPLOY: actively pursue 5%/WEEK).

Operation TEMPEST-WEEKLY targets 5% per WEEK, net of all costs. `policy.circuit_breaker` only ever
DE-risks; nothing scales deployment UP when the desk is BEHIND the weekly target. This module adds
the missing upward pressure, SAFELY:

- Rolling ISO-week pacing: week-to-date return vs the pro-rated 5%/7-day pace (week starts Monday
  00:00 UTC).
- START SOFT in the first day of the week; PRESS (deploy more) when BEHIND pace AND under-deployed
  AND NOT in drawdown; THROTTLE once the weekly target is hit.
- ANTI-MARTINGALE (hard invariant): being behind because of DRAWDOWN never presses — the protected
  drawdown breakers own the loss path; pacing only ever spends UNUSED budget, never doubles into
  losses. `drawdown >= CAUTION_DD` forces mode <= soft. (CAUTION_DD is wide here — 20% — because the
  aggressive desk is MEANT to press through small drawdowns; it only stops pressing once it is down
  20%, which is also where the policy step-down engages.)

It is ADVISORY/UTILIZATION-only: it raises how fully the desk uses its EXISTING gate-enforced caps
(more setups, fuller `risk_mult` toward 1.0, a lower take-it bar) — it NEVER raises a cap and NEVER
touches a protected module (risk_gate/policy/sizing). The risk gate still clamps `risk_mult` to
(0,1], so pacing can never increase risk above the survival cap.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

CAUTION_DD = 0.20      # drawdown at/above this -> NEVER press (mirrors policy caution / step-down)
SOFT_DAYS = 1.0        # first ~1 day of the week: start soft, preserve budget/optionality
PRESS_GAP = 0.01       # behind pro-rated pace by more than this (1% equity) -> press-eligible
PRESS_UTIL_HEAT = 0.15  # open heat below this (15% at risk) -> under-deployed, room to deploy
DAYS_IN_WEEK = 7

_MODE_RISK_MULT = {"throttle": 0.5, "soft": 0.5, "normal": 0.85, "press": 1.0}
_MODE_APPETITE = {"throttle": 0.25, "soft": 0.4, "normal": 0.7, "press": 0.95}


@dataclass
class PacingState:
    mode: str               # 'soft' | 'normal' | 'press' | 'throttle'
    appetite: float         # 0..1 deployment appetite
    suggested_risk_mult: float
    wtd_return: float       # week-to-date return vs the week-start anchor
    pace: float             # pro-rated target for days elapsed in the week
    pace_gap: float         # wtd_return - pace (negative = behind)
    drawdown: float
    open_heat: float
    in_drawdown: bool
    days_elapsed: float
    days_in_week: int
    directive: str          # human guidance injected into the team's prompts


def _directive(mode: str, pace_gap: float, wtd: float, target: float) -> str:
    if mode == "throttle":
        return (f"THROTTLE — week-to-date {wtd:+.1%} has reached the {target:.0%}/week target. Be "
                f"selective, bank winners, protect the week; only take A+ setups.")
    if mode == "press":
        return (f"PRESS — behind the weekly pace by {pace_gap:+.1%} with unused budget, no "
                f"drawdown. DEPLOY: take EVERY gate-clearing edge-aligned setup at full size "
                f"(risk_mult 1.0), lower the take-it bar, hunt edges across ALL desks (momentum/"
                f"breakout/squeeze, scalp, funding/basis carry, catalyst). Decline ONLY a failed "
                f"thesis, never for want of looking — standing flat has negative carry vs the "
                f"{target:.0%}/week goal. But the target is a goal, not a quota: never chase a "
                f"thin setup that fails the gate.")
    if mode == "soft":
        return ("SOFT — start the week conservatively (early week, or in drawdown deferring to the "
                "breakers). Take only clean high-conviction setups; preserve optionality.")
    return ("NORMAL — roughly on the weekly pace. Take clean edge-aligned setups at full standard "
            "size; keep the book working hard toward the 5%/week target.")


def compute_pacing(*, wtd_return: float, days_elapsed: float, days_in_week: int = DAYS_IN_WEEK,
                   drawdown: float, open_heat: float, weekly_target: float = 0.05) -> PacingState:
    """Pure pacing logic (no I/O). See module docstring for the safety invariants."""
    diw = max(1, int(days_in_week))
    pace = weekly_target * (max(0.0, days_elapsed) / diw)
    pace_gap = wtd_return - pace
    in_dd = drawdown >= CAUTION_DD

    if wtd_return >= weekly_target:
        mode = "throttle"
    elif in_dd:
        mode = "soft"                       # ANTI-MARTINGALE: drawdown never presses
    elif days_elapsed < SOFT_DAYS:
        mode = "soft"                       # start the week soft
    elif pace_gap <= -PRESS_GAP and open_heat < PRESS_UTIL_HEAT:
        mode = "press"                      # behind + under-deployed + healthy -> deploy
    else:
        mode = "normal"

    return PacingState(
        mode=mode, appetite=_MODE_APPETITE[mode], suggested_risk_mult=_MODE_RISK_MULT[mode],
        wtd_return=wtd_return, pace=pace, pace_gap=pace_gap, drawdown=drawdown, open_heat=open_heat,
        in_drawdown=in_dd, days_elapsed=days_elapsed, days_in_week=diw,
        directive=_directive(mode, pace_gap, wtd_return, weekly_target),
    )


def _week_start(now: datetime) -> datetime:
    """The Monday-00:00 anchor of the ISO week containing `now` (same tz as `now`)."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight - timedelta(days=now.weekday())


def pacing_state(state_dir, now: datetime, health, *, weekly_target: float = 0.05) -> PacingState:
    """Compute the rolling-week pacing state from the equity log + live portfolio health.

    `health` supplies `drawdown_from_peak` and `open_heat`. Week-to-date return is measured vs the
    last equity point at/before the Monday-00:00 anchor. If the desk started MID-WEEK (no pre-anchor
    point), both the return base AND the pace proration are anchored to the FIRST in-week point — so
    the weekly target is never prorated over days the desk had no chance to trade (which would
    fabricate a catch-up gap and a spurious PRESS). FAIL-SAFE: with <2 equity points TOTAL (no
    basis) -> SOFT (conservative default); early-week softness (within SOFT_DAYS of the Monday
    anchor) is enforced separately by the days_elapsed<SOFT_DAYS branch in compute_pacing."""
    from futures_fund.equity_log import equity_series
    dd = float(getattr(health, "drawdown_from_peak", 0.0) or 0.0)
    heat = float(getattr(health, "open_heat", 0.0) or 0.0)
    anchor_ts = _week_start(now)
    days_elapsed = (now - anchor_ts).total_seconds() / 86400.0

    series = []
    for ts, eq in equity_series(state_dir):
        try:
            series.append((datetime.fromisoformat(ts), float(eq)))
        except (ValueError, TypeError):
            continue
    if len(series) < 2:
        return compute_pacing(wtd_return=0.0, days_elapsed=min(days_elapsed, SOFT_DAYS - 0.01),
                              days_in_week=DAYS_IN_WEEK, drawdown=dd, open_heat=heat,
                              weekly_target=weekly_target)
    at_or_before = [eq for ts, eq in series if ts <= anchor_ts]
    if at_or_before:
        base = at_or_before[-1]
        elapsed = days_elapsed            # true week-to-date from the Monday anchor
    else:
        # mid-week start: anchor pace proration to the first in-week point, not Monday, so the
        # desk isn't measured against ~days it never traded (the spurious-PRESS footgun).
        base_ts, base = series[0]
        elapsed = max(0.0, (now - base_ts).total_seconds() / 86400.0)
    last = series[-1][1]
    wtd = (last / base - 1.0) if base > 0 else 0.0
    return compute_pacing(wtd_return=wtd, days_elapsed=elapsed, days_in_week=DAYS_IN_WEEK,
                          drawdown=dd, open_heat=heat, weekly_target=weekly_target)
