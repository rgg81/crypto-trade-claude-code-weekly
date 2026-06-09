"""Weekly risk-pacing engine (Pillar 1 DEPLOY): start soft -> press when behind pace (and NOT in
drawdown) -> throttle once the 5%/week target is hit. Anti-martingale: drawdown ALWAYS suppresses
press (the desk presses with UNUSED budget, never into losses).

Calendar note: 2026-06-01 / -08 / -15 are Mondays (the ISO-week anchors used below)."""
from datetime import UTC, datetime

from futures_fund.pacing import (
    CAUTION_DD,
    PRESS_GAP,
    compute_pacing,
    pacing_state,
)


def _c(wtd, day, dd=0.0, heat=0.0, diw=7, target=0.05):
    # day = days elapsed in the week (1.0 = end of Monday)
    return compute_pacing(wtd_return=wtd, days_elapsed=day, days_in_week=diw,
                          drawdown=dd, open_heat=heat, weekly_target=target)


def test_throttle_when_target_hit():
    s = _c(wtd=0.05, day=3)
    assert s.mode == "throttle"
    assert s.suggested_risk_mult <= 0.6
    s2 = _c(wtd=0.061, day=1)  # hit early -> still throttle
    assert s2.mode == "throttle"


def test_soft_early_week():
    s = _c(wtd=0.0, day=0.5)  # day 0.5 < SOFT_DAYS, behind but early -> soft, NOT press
    assert s.mode == "soft"


def test_press_when_behind_and_underdeployed_and_no_drawdown():
    # day 3 of 7, pace = 2.14%, wtd 0% -> gap -2.14% (> PRESS_GAP behind), no dd, low heat -> press
    s = _c(wtd=0.0, day=3, dd=0.0, heat=0.0)
    assert s.mode == "press"
    assert s.suggested_risk_mult >= 0.95
    assert s.appetite >= 0.8


def test_anti_martingale_drawdown_never_presses():
    # same behind-pace setup BUT in drawdown -> must NOT press (breakers own the loss path)
    s = _c(wtd=-0.04, day=3, dd=CAUTION_DD + 0.001, heat=0.0)
    assert s.mode != "press"
    assert s.mode == "soft"
    assert s.in_drawdown is True
    assert s.suggested_risk_mult <= 0.6


def test_no_press_when_already_deployed():
    # behind pace but heat already high (deployed) -> not under-deployed -> normal, not press
    s = _c(wtd=0.0, day=3, dd=0.0, heat=0.20)
    assert s.mode == "normal"


def test_normal_when_on_pace():
    # day 3.5, pace 2.5%, wtd 2.5% -> on pace -> normal
    s = _c(wtd=0.025, day=3.5, dd=0.0, heat=0.0)
    assert s.mode == "normal"


def test_pace_gap_sign_and_fields():
    s = _c(wtd=0.0, day=3.5, diw=7, target=0.05)
    assert abs(s.pace - 0.025) < 1e-9      # 0.05 * 3.5/7
    assert abs(s.pace_gap - (-0.025)) < 1e-9
    assert s.wtd_return == 0.0
    assert isinstance(s.directive, str) and len(s.directive) > 0


def test_press_requires_gap_beyond_threshold():
    # only slightly behind (< PRESS_GAP) -> normal, not press
    s = _c(wtd=0.025 - (PRESS_GAP * 0.5), day=3.5)
    assert s.mode == "normal"


def test_pacing_state_reads_equity_log(tmp_path):
    from futures_fund.equity_log import record_equity
    state = tmp_path / "s"
    # anchor 10000 on Mon Jun 1, latest 10000 on Thu Jun 4 -> wtd 0%, day 3, behind pace -> press
    record_equity(state, datetime(2026, 6, 1, tzinfo=UTC), 10000.0, cycle=1)
    record_equity(state, datetime(2026, 6, 4, tzinfo=UTC), 10000.0, cycle=2)

    class _H:
        drawdown_from_peak = 0.0
        open_heat = 0.0
    s = pacing_state(state, datetime(2026, 6, 4, tzinfo=UTC), _H(), weekly_target=0.05)
    assert s.mode == "press"
    assert abs(s.wtd_return - 0.0) < 1e-9


def test_pacing_state_empty_log_is_soft(tmp_path):
    class _H:
        drawdown_from_peak = 0.0
        open_heat = 0.0
    s = pacing_state(tmp_path / "s", datetime(2026, 6, 16, tzinfo=UTC), _H())
    assert s.mode == "soft"  # no data -> conservative default


def test_pacing_state_midweek_start_does_not_fabricate_press(tmp_path):
    from futures_fund.equity_log import record_equity
    state = tmp_path / "s"
    # desk started MID-WEEK: first point Thu Jun 4, latest Fri Jun 5, flat, NO Monday anchor point.
    # Pace must prorate from Thu (1 day), not from Monday (4 days) — so a flat desk is NOT pressed
    # to 'catch up' on ~3 days it never traded.
    record_equity(state, datetime(2026, 6, 4, tzinfo=UTC), 10000.0, cycle=1)
    record_equity(state, datetime(2026, 6, 5, tzinfo=UTC), 10000.0, cycle=2)

    class _H:
        drawdown_from_peak = 0.0
        open_heat = 0.0
    s = pacing_state(state, datetime(2026, 6, 5, tzinfo=UTC), _H(), weekly_target=0.05)
    assert s.mode != "press"          # would have been 'press' under Monday-anchored proration
    assert abs(s.pace - 0.05 / 7) < 1e-6  # pace prorated over ~1 actual day, not ~4


def test_pacing_state_wtd_from_week_start_anchor(tmp_path):
    from futures_fund.equity_log import record_equity
    state = tmp_path / "s"
    record_equity(state, datetime(2026, 5, 28, tzinfo=UTC), 9000.0, cycle=1)   # prior week
    record_equity(state, datetime(2026, 6, 1, tzinfo=UTC), 10000.0, cycle=2)   # week-start anchor
    record_equity(state, datetime(2026, 6, 3, tzinfo=UTC), 10300.0, cycle=3)   # +3% WTD

    class _H:
        drawdown_from_peak = 0.0
        open_heat = 0.0
    s = pacing_state(state, datetime(2026, 6, 3, tzinfo=UTC), _H(), weekly_target=0.05)
    assert abs(s.wtd_return - 0.03) < 1e-6   # vs the Jun-1 Monday anchor, not the May point
