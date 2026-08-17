"""Both models of the gate's sizing must agree with the gate.

Review finding (MEDIUM): my first attempt at this fix was tested only by calling
`presize_and_balance` with a pre-multiplied ptr — stashing the orchestration change left all four
tests passing, so nothing pinned the wiring. These tests pin the derivation itself, which both
callers now share.

Review finding (HIGH): `blended_book_cli` hardcoded ptr at the HEALTHY tier and ignored the breaker,
so at dd>=5% + caution tier it was 4x high — enough to make `deployment_resizes` flag most of the
book for close+reopen every cycle, forever, while in drawdown (HARD RULE 2).
"""
import pytest

from futures_fund.effective_risk import (
    breaker_multiplier,
    effective_per_trade_risk,
    tier_factor,
)

LIVE_DD = 0.0552          # cy294
BASE = 0.010              # high_vol_trend / low_vol_range quadrant


def test_breaker_multiplier_matches_the_ladder():
    assert breaker_multiplier(0.00) == 1.0
    assert breaker_multiplier(0.049) == 1.0
    assert breaker_multiplier(0.05) == 0.5
    assert breaker_multiplier(LIVE_DD) == 0.5
    assert breaker_multiplier(0.10) == 0.25
    assert breaker_multiplier(0.20) == 0.25          # force_flatten, multiplier still bounded


def test_tier_factor_matches_caps_for():
    assert tier_factor("healthy") == 1.0
    assert tier_factor("caution") == 0.5
    assert tier_factor("stressed") == 0.0
    assert tier_factor(None) == 1.0
    assert tier_factor("UNKNOWN") == 1.0             # unknown tier must not silently zero the book


def test_the_live_cy294_case_is_four_times_off_untreated():
    """caution tier (0.5) x breaker (0.5) = 0.25 — the planner's hardcoded value was 4x high."""
    eff = effective_per_trade_risk(BASE, drawdown_from_peak=LIVE_DD, health_tier="caution")
    assert eff == pytest.approx(BASE * 0.25)
    assert BASE / eff == pytest.approx(4.0)


def test_caps_derived_ptr_must_not_double_apply_the_tier():
    """caps_for has ALREADY halved for the tier; applying it again would under-size 2x."""
    from_caps = BASE * 0.5                            # what caps_for(caution) returns
    eff = effective_per_trade_risk(from_caps, drawdown_from_peak=LIVE_DD,
                                   health_tier="caution", tier_already_applied=True)
    assert eff == pytest.approx(BASE * 0.25), "must equal the raw-base result, not half of it"


def test_healthy_desk_is_unchanged():
    assert effective_per_trade_risk(BASE, drawdown_from_peak=0.0,
                                    health_tier="healthy") == pytest.approx(BASE)


def test_period_returns_are_passed_through_not_assumed():
    """policy is PROTECTED and may change; the derivation must not hardcode zeros. Today the
    period returns do not alter risk_multiplier, so this pins the plumbing, not the value."""
    a = effective_per_trade_risk(BASE, drawdown_from_peak=0.0, daily_pnl_pct=-0.04)
    b = effective_per_trade_risk(BASE, drawdown_from_peak=0.0, daily_pnl_pct=0.0)
    assert a == pytest.approx(b * breaker_multiplier(0.0, daily_pnl_pct=-0.04)
                              / breaker_multiplier(0.0))


def test_negative_and_degenerate_inputs_are_safe():
    assert effective_per_trade_risk(-1.0, drawdown_from_peak=0.0) == 0.0
    assert effective_per_trade_risk(BASE, drawdown_from_peak=-0.5) == pytest.approx(BASE)
    assert effective_per_trade_risk(0.0, drawdown_from_peak=LIVE_DD) == 0.0


def test_stressed_tier_zeroes_risk_like_caps_for():
    assert effective_per_trade_risk(BASE, drawdown_from_peak=0.0, health_tier="stressed") == 0.0
