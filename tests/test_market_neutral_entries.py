"""Market-neutral entry gating: on a DOLLAR-NEUTRAL desk a short is the short SLEEVE of a hedged
spread, not a counter-regime directional knife, so both sleeves must open at MARKET together. The
directional desk's counter-regime confirmation (rewrite a counter-regime market short into a
confirmation stop_entry) would otherwise strip one sleeve into triggers and open the book one-sided
in any trending regime — breaking neutrality by construction. The `market_neutral` flag bypasses it.
"""
from futures_fund.orchestration import _apply_counter_regime_confirmation

_REGIME = {"regime": "risk_on", "drivers": {"quorum_met": True}}


def _short(sym="BNBUSDT"):
    return {"symbol": sym, "direction": "short", "entry": 600.0, "stop": 612.0,
            "take_profits": [576.0], "atr": 8.0, "confidence": 0.6, "horizon_hours": 16}


def test_directional_desk_arms_counter_regime_short_in_risk_on():
    # legacy directional behavior preserved: a short while risk_on -> a confirmation stop_entry
    market, armed = _apply_counter_regime_confirmation([_short()], _REGIME, 1, market_neutral=False)
    assert len(armed) == 1 and len(market) == 0


def test_neutral_desk_opens_both_sleeves_at_market():
    # neutral desk: the short sleeve of a hedged spread opens at MARKET, never deferred to a trigger
    market, armed = _apply_counter_regime_confirmation([_short()], _REGIME, 1, market_neutral=True)
    assert len(market) == 1 and len(armed) == 0


def test_neutral_flag_does_not_disturb_with_regime_longs():
    # a with-regime long opens at market under both modes (the flag only frees the counter side)
    longp = {"symbol": "UNIUSDT", "direction": "long", "entry": 3.3, "stop": 3.1,
             "take_profits": [3.9], "atr": 0.2, "confidence": 0.7, "horizon_hours": 16}
    for mn in (True, False):
        market, armed = _apply_counter_regime_confirmation([longp], _REGIME, 1, market_neutral=mn)
        assert len(market) == 1 and len(armed) == 0
