"""The pre-sizer must size against the risk the GATE will actually use, breaker included.

risk_gate.py:88 sizes with

    risk_pct = caps.per_trade_risk_pct * breaker.risk_multiplier * rm

but orchestration passed the pre-sizer the RAW per_trade_risk_pct — `circuit_breaker` was never
even referenced there. Two consequences, both observed live once the -5% step-down engaged at
cy291 (drawdown 5.59%, risk_multiplier 0.5):

1. SYSTEMATIC HALF-SIZING. The pre-sizer stamps rm = intended_risk / ptr, so the gate realises
   ptr * 0.5 * rm = HALF the intended risk. The book deploys at half the pre-sizer's target — a
   large part of why deployment sat at 0.29x.

2. SILENT DUST DROPS. The pre-sizer's dust check passes a leg at its full-size risk, then the gate
   halves it under consolidate's 0.001 floor, where it is discarded with no record. The arithmetic
   predicts the live drops exactly:

       leg    presize   x0.5      outcome          actual
       LINK   0.00674   0.00337   survives         opened
       SOL    0.00368   0.00184   survives         opened
       BTW    0.01000   0.00500   survives         opened
       BTC    0.00172   0.00086   DUST             SILENT-DROPPED
       XRP    0.00235   0.00118   survives         opened
       HYPE   0.00448   0.00224   survives         opened

   6/6 at cy294, and it also explains cy293 dropping BTC (0.00086) and ETH (0.00090).

Passing the EFFECTIVE risk (ptr * risk_multiplier) makes the pre-sizer's model match reality: rm is
computed against the halved budget, so the gate realises exactly the intended risk, and the dust
check then tests the real post-breaker size.
"""
import pytest

from futures_fund.models import TradeProposal
from futures_fund.neutral_book import presize_and_balance
from futures_fund.notional_sizing import notional_to_risk_pct

EQ = 10077.55
BASE_PTR = 0.010
FLOOR = 0.001


def _p(sym, direction, stop_frac, entry=100.0):
    stop = entry * (1 - stop_frac) if direction == "long" else entry * (1 + stop_frac)
    tp = entry * (1 + 2.2 * stop_frac) if direction == "long" else entry * (1 - 2.2 * stop_frac)
    return TradeProposal(symbol=sym, direction=direction, entry=entry, stop=stop,
                         take_profits=[tp], atr=entry * stop_frac / 2, funding_rate=0.0,
                         confidence=0.6, horizon_hours=8, rationale="x",
                         falsifiable_prediction="y")


def _cy294():
    """The live cy294 submission, by stop fraction."""
    return [_p("LINK", "long", 0.0298), _p("SOL", "long", 0.0162), _p("BTW", "long", 0.2142),
            _p("BTC", "short", 0.0103), _p("XRP", "short", 0.0141), _p("HYPE", "short", 0.0269)]


def _gate_realised_risk(p, base_ptr, breaker_mult):
    """What risk_gate.py:88 will actually realise for this stamped proposal."""
    return base_ptr * breaker_mult * p.risk_mult


def _intended_risk(p, effective_ptr):
    """The risk the pre-sizer intended when it stamped risk_mult against `effective_ptr`."""
    return effective_ptr * p.risk_mult


def test_gate_realises_the_risk_the_presizer_intended():
    """The invariant. With the breaker at 0.5, passing the EFFECTIVE ptr makes the gate's realised
    risk equal the intended risk; passing the raw ptr realises half of it."""
    mult = 0.5
    eff = BASE_PTR * mult
    heat = {p.symbol: 0.08 for p in _cy294()}

    kept_eff, _ = presize_and_balance(_cy294(), equity=EQ, per_trade_risk_pct=eff,
                                      heat_headroom_by_symbol=heat)
    for p in kept_eff:
        assert _gate_realised_risk(p, BASE_PTR, mult) == pytest.approx(
            _intended_risk(p, eff), rel=1e-9)

    kept_raw, _ = presize_and_balance(_cy294(), equity=EQ, per_trade_risk_pct=BASE_PTR,
                                      heat_headroom_by_symbol=heat)
    halved = [p for p in kept_raw
              if _gate_realised_risk(p, BASE_PTR, mult) < _intended_risk(p, BASE_PTR) * 0.99]
    assert halved, "raw ptr must demonstrate the systematic half-sizing this fixes"


def test_no_kept_leg_is_dust_once_the_breaker_is_applied():
    """The live failure: BTC survived the pre-sizer's dust check and was then halved under the
    floor. With the effective ptr the check tests the real post-breaker size."""
    eff = BASE_PTR * 0.5
    heat = {p.symbol: 0.08 for p in _cy294()}
    kept, summ = presize_and_balance(_cy294(), equity=EQ, per_trade_risk_pct=eff,
                                     heat_headroom_by_symbol=heat, dust_risk_frac=FLOOR)
    for p in kept:
        dist = abs(p.entry - p.stop)
        notional = p.risk_mult * eff * EQ * abs(p.entry) / dist
        assert notional_to_risk_pct(notional, p.entry, p.stop, EQ) >= FLOOR, (
            f"{p.symbol} is dust after the breaker and would be dropped silently")


def test_an_unbreakered_desk_is_unchanged():
    """risk_multiplier 1.0 must reproduce the pre-fix sizing exactly."""
    heat = {p.symbol: 0.08 for p in _cy294()}
    a, _ = presize_and_balance(_cy294(), equity=EQ, per_trade_risk_pct=BASE_PTR * 1.0,
                               heat_headroom_by_symbol=heat)
    b, _ = presize_and_balance(_cy294(), equity=EQ, per_trade_risk_pct=BASE_PTR,
                               heat_headroom_by_symbol=heat)
    assert [p.risk_mult for p in a] == [p.risk_mult for p in b]


def test_risk_mult_stays_within_bounds_under_the_breaker():
    """Halving the budget raises rm; it must still respect the (0,1] clamp the gate expects."""
    eff = BASE_PTR * 0.5
    heat = {p.symbol: 0.08 for p in _cy294()}
    kept, _ = presize_and_balance(_cy294(), equity=EQ, per_trade_risk_pct=eff,
                                  heat_headroom_by_symbol=heat)
    assert all(0.0 < p.risk_mult <= 1.0 for p in kept), [
        (p.symbol, p.risk_mult) for p in kept]
