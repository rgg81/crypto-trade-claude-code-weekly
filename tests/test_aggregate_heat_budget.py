"""The pre-sizer must fit the BATCH gross-heat cap, not just per-symbol headroom.

policy.py is explicit: "position_risk/consolidate sum gross stop-risk and do NOT credit the
long/short offset, so max_heat is the binding deployment ceiling for a balanced book."

The pre-sizer only ever checked PER-SYMBOL headroom, which says nothing about the SUM — and the sum
is exactly what consolidate acts on:

    factor = max_heat / total ; trades = [_scale(t, factor) for t in trades]
    return [t for t in trades if risk(t) >= min_risk_frac]

so an over-budget batch is scaled behind the desk's back and any leg pushed under the dust floor
vanishes with no record.

NOT PROVEN to be the cy292 cause. There, 6 legs were submitted, 6 approved (heat_dropped []), 4
opened, and BNBUSDT/XRPUSDT disappeared — but six legs each capped at per_trade_risk sum to 0.06,
under the 0.08 max_heat, so the batch total alone does not explain it (held positions consuming
headroom, or another path inside consolidate, could). This closes a real and separately verifiable
gap; report["silent_dropped"] (387575b) is what will identify the actual trigger next time.

Fitting the aggregate budget up front means consolidate has nothing left to scale, so nothing is
dusted behind the desk's back — and anything that genuinely cannot clear the dust floor is reported
in `heat_dropped` instead. The pre-sizer still only ever SHRINKS.
"""
import pytest

from futures_fund.models import TradeProposal
from futures_fund.neutral_book import presize_and_balance

EQ = 10000.0


def _p(sym, direction, stop_frac=0.06, entry=100.0):
    stop = entry * (1 - stop_frac) if direction == "long" else entry * (1 + stop_frac)
    tp = entry * (1 + 2.2 * stop_frac) if direction == "long" else entry * (1 - 2.2 * stop_frac)
    return TradeProposal(symbol=sym, direction=direction, entry=entry, stop=stop,
                         take_profits=[tp], atr=entry * stop_frac / 2, funding_rate=0.0,
                         confidence=0.6, horizon_hours=8, rationale="x",
                         falsifiable_prediction="y")


def _six():
    return [_p("L1", "long"), _p("L2", "long"), _p("L3", "long"),
            _p("S1", "short"), _p("S2", "short"), _p("S3", "short")]


def notional_risk(p, equity=EQ):
    """Stop-risk fraction this leg contributes — the quantity consolidate sums."""
    from futures_fund.notional_sizing import notional_to_risk_pct
    return notional_to_risk_pct(_notional(p, equity), p.entry, p.stop, equity)


def _total_risk(kept, equity=EQ):
    return sum(notional_risk(p, equity) for p in kept)


def _notional(p, equity):
    """Leg notional the pre-sizer stamped: risk_mult * ptr * equity * entry/dist."""
    dist = abs(p.entry - p.stop)
    return p.risk_mult * 0.010 * equity * abs(p.entry) / dist


def test_batch_fits_the_aggregate_heat_budget():
    """Six legs must be sized so their SUMMED stop-risk clears max_heat, leaving consolidate
    nothing to scale (and therefore nothing to dust)."""
    heat = {p.symbol: 0.08 for p in _six()}
    kept, summ = presize_and_balance(
        _six(), equity=EQ, per_trade_risk_pct=0.010, heat_headroom_by_symbol=heat,
        aggregate_heat_headroom=0.08)
    assert len(kept) == 6, "no leg should be lost"
    assert _total_risk(kept) <= 0.08 + 1e-6, "summed stop-risk must fit the batch cap"


def test_per_symbol_headroom_does_not_bound_the_batch_sum():
    """The gap this closes. Each leg is individually within its 0.02 headroom, yet the SUM is 0.06
    — three times over. Per-symbol headroom says nothing about the total, which is exactly what
    consolidate scales on. (Six legs each capped at per_trade_risk only exceed max_heat once HELD
    positions have already consumed part of the budget, which is why the headroom passed in is the
    residual `max_heat - used_heat`, not max_heat itself.)"""
    heat = {p.symbol: 0.02 for p in _six()}
    kept, _ = presize_and_balance(_six(), equity=EQ, per_trade_risk_pct=0.010,
                                  heat_headroom_by_symbol=heat)
    per_leg = [notional_risk(p, EQ) for p in kept]
    assert all(r <= 0.02 + 1e-9 for r in per_leg), "each leg is within its own headroom"
    assert sum(per_leg) > 0.02, "yet the batch sum blows past it — unbounded before this fix"


def test_scaling_is_proportional_so_the_book_stays_balanced():
    heat = {p.symbol: 0.08 for p in _six()}
    kept, summ = presize_and_balance(
        _six(), equity=EQ, per_trade_risk_pct=0.010, heat_headroom_by_symbol=heat,
        aggregate_heat_headroom=0.04)
    longs = [p for p in kept if p.direction == "long"]
    shorts = [p for p in kept if p.direction == "short"]
    assert len(longs) == len(shorts) == 3, "count symmetry preserved"
    assert sum(_notional(p, EQ) for p in longs) == pytest.approx(
        sum(_notional(p, EQ) for p in shorts), rel=1e-6), "dollar neutrality preserved"


def test_a_generous_budget_does_not_shrink_anything():
    heat = {p.symbol: 0.08 for p in _six()}
    base, _ = presize_and_balance(_six(), equity=EQ, per_trade_risk_pct=0.010,
                                  heat_headroom_by_symbol=heat)
    wide, _ = presize_and_balance(_six(), equity=EQ, per_trade_risk_pct=0.010,
                                  heat_headroom_by_symbol=heat, aggregate_heat_headroom=99.0)
    assert [round(p.risk_mult, 9) for p in wide] == [round(p.risk_mult, 9) for p in base]


def test_budget_never_grows_a_leg():
    """The pre-sizer's contract: it may only ever SHRINK."""
    heat = {p.symbol: 0.08 for p in _six()}
    base, _ = presize_and_balance(_six(), equity=EQ, per_trade_risk_pct=0.010,
                                  heat_headroom_by_symbol=heat)
    tight, _ = presize_and_balance(_six(), equity=EQ, per_trade_risk_pct=0.010,
                                   heat_headroom_by_symbol=heat, aggregate_heat_headroom=0.02)
    by = {p.symbol: p.risk_mult for p in base}
    for p in tight:
        assert p.risk_mult <= by[p.symbol] + 1e-9


def test_none_budget_is_backwards_compatible():
    heat = {p.symbol: 0.08 for p in _six()}
    a, _ = presize_and_balance(_six(), equity=EQ, per_trade_risk_pct=0.010,
                               heat_headroom_by_symbol=heat)
    b, _ = presize_and_balance(_six(), equity=EQ, per_trade_risk_pct=0.010,
                               heat_headroom_by_symbol=heat, aggregate_heat_headroom=None)
    assert [p.risk_mult for p in a] == [p.risk_mult for p in b]


def test_zero_or_negative_budget_is_ignored_not_zeroing_the_book():
    """A bad/absent budget must not silently flatten the desk."""
    heat = {p.symbol: 0.08 for p in _six()}
    kept, _ = presize_and_balance(_six(), equity=EQ, per_trade_risk_pct=0.010,
                                  heat_headroom_by_symbol=heat, aggregate_heat_headroom=0.0)
    assert len(kept) == 6 and all(p.risk_mult > 0 for p in kept)


def test_budget_is_respected_even_when_the_dust_loop_refills():
    """Review finding: the aggregate scale was applied ONCE before the dust/trim loop, but every
    re-water-fill inside the loop restored the unconstrained side budgets — so a book that lost a
    leg to dust came back OVER the budget, and consolidate batch-scaled (and could dust) after all.

    Reproduced with the live cy293 stop shape at a 0.02 budget: realised 0.02162 > 0.02.
    """
    props = [_p("LINK", "long", 0.0305), _p("BTW", "long", 0.2207),
             _p("BTC", "short", 0.0093), _p("ETH", "short", 0.0122),
             _p("HYPE", "short", 0.0268)]
    heat = {p.symbol: 0.08 for p in props}
    kept, _ = presize_and_balance(props, equity=EQ, per_trade_risk_pct=0.010,
                                  heat_headroom_by_symbol=heat, aggregate_heat_headroom=0.02)
    assert _total_risk(kept) <= 0.02 + 1e-6, "aggregate fit must survive the dust/trim loop"
