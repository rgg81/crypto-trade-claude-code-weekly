import pytest

from futures_fund.models import PortfolioHealth, RegimeState
from futures_fund.policy import caps_for, circuit_breaker, cvar


def _health(equity, peak):
    return PortfolioHealth(equity=equity, peak_equity=peak)


def test_healthy_low_vol_trend_is_full_caps():
    caps = caps_for(RegimeState(quadrant="low_vol_trend"), _health(10_000, 10_000))
    assert caps.max_leverage == 1.0          # literal 1x per position (full margin, no liq risk)
    assert caps.per_trade_risk_pct == pytest.approx(0.015)
    assert caps.max_heat == pytest.approx(0.10)
    assert caps.bias == "normal"


def test_high_vol_range_is_reduced():
    caps = caps_for(RegimeState(quadrant="high_vol_range"), _health(10_000, 10_000))
    assert caps.max_leverage == 1.0          # 1x across every quadrant
    assert caps.per_trade_risk_pct == pytest.approx(0.005)


def test_caution_halves_caps():
    # equity 9300/10000 -> dd 7% -> caution tier (>=5%, <10%); halve the healthy Q1 risk cap
    caps = caps_for(RegimeState(quadrant="low_vol_trend"), _health(9_300, 10_000))
    assert caps.per_trade_risk_pct == pytest.approx(0.0075)   # risk halved = the meaningful de-risk


def test_stressed_forces_flat_bias_and_zero_risk():
    caps = caps_for(RegimeState(quadrant="low_vol_trend"), _health(8_900, 10_000))  # dd 11% (>=10%)
    assert caps.bias == "flat"
    assert caps.per_trade_risk_pct == 0.0


def test_transition_regime_minimum_size():
    caps = caps_for(RegimeState(quadrant="transition"), _health(10_000, 10_000))
    assert caps.bias == "reduce"
    assert caps.max_leverage <= 2.0


def test_circuit_breaker_daily_loss_halts_new():
    state = circuit_breaker(daily_pnl_pct=-0.04, weekly_pnl_pct=-0.01, monthly_pnl_pct=-0.02,
                            dd_from_peak=0.02)
    assert state.allow_new_entries is False
    assert state.risk_multiplier <= 1.0


def test_circuit_breaker_step_down_halves_at_5pct_drawdown():
    state = circuit_breaker(daily_pnl_pct=-0.01, weekly_pnl_pct=-0.02, monthly_pnl_pct=-0.03,
                            dd_from_peak=0.07)
    assert state.risk_multiplier == pytest.approx(0.5)
    assert state.allow_new_entries is True  # step-down only; opens still allowed below -10%


def test_circuit_breaker_reduce_only_at_10pct_drawdown():
    state = circuit_breaker(daily_pnl_pct=-0.01, weekly_pnl_pct=-0.02, monthly_pnl_pct=-0.03,
                            dd_from_peak=0.11)
    assert state.allow_new_entries is False      # reduce-only: no new opens
    assert state.force_flatten is False          # but not yet flattening
    assert state.risk_multiplier == pytest.approx(0.25)


def test_circuit_breaker_force_flatten_boundary_at_15pct():
    # T4: force_flatten FALSE at dd 0.14, TRUE at dd 0.15
    just_under = circuit_breaker(daily_pnl_pct=-0.01, weekly_pnl_pct=-0.02, monthly_pnl_pct=-0.03,
                                 dd_from_peak=0.14)
    at_thresh = circuit_breaker(daily_pnl_pct=-0.01, weekly_pnl_pct=-0.02, monthly_pnl_pct=-0.03,
                                dd_from_peak=0.15)
    assert just_under.force_flatten is False
    assert at_thresh.force_flatten is True
    assert at_thresh.allow_new_entries is False


def test_circuit_breaker_monthly_loss_force_flattens():
    # additive secondary: a -12% calendar month force-flattens even at modest drawdown-from-peak
    state = circuit_breaker(daily_pnl_pct=-0.01, weekly_pnl_pct=-0.05, monthly_pnl_pct=-0.12,
                            dd_from_peak=0.04)
    assert state.force_flatten is True
    assert state.allow_new_entries is False


def test_cvar_is_mean_of_worst_tail():
    # returns; 5% tail of 20 obs = worst 1 obs = -0.10
    returns = [-0.10] + [0.01] * 19
    assert cvar(returns, alpha=0.05) == pytest.approx(-0.10)
