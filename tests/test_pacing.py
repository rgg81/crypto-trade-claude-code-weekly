"""Monthly risk-pacing engine (Pillar 1 DEPLOY): start soft -> press when behind pace (and NOT in
drawdown) -> throttle once the ~3%/month target is hit. Anti-martingale: drawdown ALWAYS suppresses
press (the desk presses with UNUSED budget, never into losses).

Calendar note: the month anchor is the 1st at 00:00 UTC; June 2026 has 30 days."""
from datetime import UTC, datetime

from futures_fund.pacing import (
    CAUTION_DD,
    PRESS_GAP,
    compute_pacing,
    pacing_state,
)


def _c(mtd, day, dd=0.0, heat=0.0, dim=30, target=0.03):
    # day = days elapsed in the month (1.0 = end of the 1st)
    return compute_pacing(mtd_return=mtd, days_elapsed=day, days_in_month=dim,
                          drawdown=dd, open_heat=heat, monthly_target=target)


def test_throttle_when_target_hit():
    s = _c(mtd=0.03, day=10)
    assert s.mode == "throttle"
    assert s.suggested_risk_mult <= 0.6
    s2 = _c(mtd=0.04, day=2)  # hit early -> still throttle
    assert s2.mode == "throttle"


def test_soft_early_month():
    s = _c(mtd=0.0, day=1.5)  # day 1.5 < SOFT_DAYS, behind but early -> soft, NOT press
    assert s.mode == "soft"


def test_press_when_behind_and_underdeployed_and_no_drawdown():
    # day 10 of 30, pace = 1.0%, mtd 0% -> gap -1.0% (> PRESS_GAP behind), no dd, low heat -> press
    s = _c(mtd=0.0, day=10, dd=0.0, heat=0.0)
    assert s.mode == "press"
    assert s.suggested_risk_mult >= 0.95
    assert s.appetite >= 0.8


def test_anti_martingale_drawdown_never_presses():
    # same behind-pace setup BUT in drawdown -> must NOT press (breakers own the loss path)
    s = _c(mtd=-0.02, day=10, dd=CAUTION_DD + 0.001, heat=0.0)
    assert s.mode != "press"
    assert s.mode == "soft"
    assert s.in_drawdown is True
    assert s.suggested_risk_mult <= 0.6


def test_no_press_when_already_deployed():
    # behind pace but heat already high (deployed) -> not under-deployed -> normal, not press
    s = _c(mtd=0.0, day=10, dd=0.0, heat=0.10)
    assert s.mode == "normal"


def test_normal_when_on_pace():
    # day 15, pace 1.5%, mtd 1.5% -> on pace -> normal
    s = _c(mtd=0.015, day=15, dd=0.0, heat=0.0)
    assert s.mode == "normal"


def test_pace_gap_sign_and_fields():
    s = _c(mtd=0.0, day=15, dim=30, target=0.03)
    assert abs(s.pace - 0.015) < 1e-9      # 0.03 * 15/30
    assert abs(s.pace_gap - (-0.015)) < 1e-9
    assert s.mtd_return == 0.0
    assert isinstance(s.directive, str) and len(s.directive) > 0


def test_press_requires_gap_beyond_threshold():
    # only slightly behind (< PRESS_GAP) -> normal, not press
    s = _c(mtd=0.015 - (PRESS_GAP * 0.5), day=15)
    assert s.mode == "normal"


def test_pacing_state_reads_equity_log(tmp_path):
    from futures_fund.equity_log import record_equity
    state = tmp_path / "s"
    # anchor 10000 on Jun 1, latest 10000 on Jun 11 -> mtd 0%, day 10, behind pace -> press
    record_equity(state, datetime(2026, 6, 1, tzinfo=UTC), 10000.0, cycle=1)
    record_equity(state, datetime(2026, 6, 11, tzinfo=UTC), 10000.0, cycle=2)

    class _H:
        drawdown_from_peak = 0.0
        open_heat = 0.0
    s = pacing_state(state, datetime(2026, 6, 11, tzinfo=UTC), _H(), monthly_target=0.03)
    assert s.mode == "press"
    assert abs(s.mtd_return - 0.0) < 1e-9


def test_pacing_state_empty_log_is_soft(tmp_path):
    class _H:
        drawdown_from_peak = 0.0
        open_heat = 0.0
    s = pacing_state(tmp_path / "s", datetime(2026, 6, 16, tzinfo=UTC), _H())
    assert s.mode == "soft"  # no data -> conservative default


def test_pacing_state_midmonth_start_does_not_fabricate_press(tmp_path):
    from futures_fund.equity_log import record_equity
    state = tmp_path / "s"
    # desk started MID-MONTH: first point Jun 11, latest Jun 12, flat, NO 1st-of-month anchor point.
    # Pace must prorate from Jun 11 (1 day), not Jun 1 (11 days) — a flat desk is NOT pressed
    # to 'catch up' on ~10 days it never traded.
    record_equity(state, datetime(2026, 6, 11, tzinfo=UTC), 10000.0, cycle=1)
    record_equity(state, datetime(2026, 6, 12, tzinfo=UTC), 10000.0, cycle=2)

    class _H:
        drawdown_from_peak = 0.0
        open_heat = 0.0
    s = pacing_state(state, datetime(2026, 6, 12, tzinfo=UTC), _H(), monthly_target=0.03)
    assert s.mode != "press"          # would have been 'press' under 1st-anchored proration
    assert abs(s.pace - 0.03 / 30) < 1e-6  # pace prorated over ~1 actual day, not ~11


def test_pacing_state_mtd_from_month_start_anchor(tmp_path):
    from futures_fund.equity_log import record_equity
    state = tmp_path / "s"
    record_equity(state, datetime(2026, 5, 28, tzinfo=UTC), 9000.0, cycle=1)    # prior month
    record_equity(state, datetime(2026, 6, 1, tzinfo=UTC), 10000.0, cycle=2)    # month-start anchor
    record_equity(state, datetime(2026, 6, 3, tzinfo=UTC), 10300.0, cycle=3)    # +3% MTD

    class _H:
        drawdown_from_peak = 0.0
        open_heat = 0.0
    s = pacing_state(state, datetime(2026, 6, 3, tzinfo=UTC), _H(), monthly_target=0.03)
    assert abs(s.mtd_return - 0.03) < 1e-6   # vs the Jun-1 anchor, not the May point


def test_pacing_state_uses_live_equity_not_stale_log(tmp_path):
    # The equity LOG is appended only when the gate runs (per STRATEGIC cycle); at preflight the
    # last logged point is the PRIOR cycle — up to a full cycle stale. If the book moved against us
    # intra-cycle, MTD must use the LIVE M2M equity (health.equity), NOT the stale logged point.
    from futures_fund.equity_log import record_equity
    state = tmp_path / "s"
    record_equity(state, datetime(2026, 6, 1, tzinfo=UTC), 10000.0, cycle=1)    # anchor
    record_equity(state, datetime(2026, 6, 3, tzinfo=UTC), 10300.0, cycle=2)  # last LOGGED (stale)

    class _H:
        drawdown_from_peak = 0.029
        open_heat = 0.0
        equity = 10000.0   # LIVE M2M now: the bounce erased the logged +3%
    s = pacing_state(state, datetime(2026, 6, 3, tzinfo=UTC), _H(), monthly_target=0.03)
    assert abs(s.mtd_return - 0.0) < 1e-9   # live 10000 vs anchor 10000 = 0%, not +3% off log


def test_pacing_state_falls_back_to_log_when_no_live_equity(tmp_path):
    # FAIL-SAFE: a health object without a finite equity field -> fall back to the logged point.
    from futures_fund.equity_log import record_equity
    state = tmp_path / "s"
    record_equity(state, datetime(2026, 6, 1, tzinfo=UTC), 10000.0, cycle=1)
    record_equity(state, datetime(2026, 6, 3, tzinfo=UTC), 10300.0, cycle=2)

    class _H:
        drawdown_from_peak = 0.0
        open_heat = 0.0
    s = pacing_state(state, datetime(2026, 6, 3, tzinfo=UTC), _H(), monthly_target=0.03)
    assert abs(s.mtd_return - 0.03) < 1e-6   # no live equity -> uses the logged 10300 -> +3%
