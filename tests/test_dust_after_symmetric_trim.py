"""The dust check must run AFTER the symmetric trim, not before it.

presize_and_balance ordered its steps:

    1. water-fill each side's budget across its legs
    2. _viable()          <- dust check
    3. SYMMETRIC TRIM     <- scales a side DOWN to match the other
    4. _stamp

so a leg could pass the dust check at its water-filled size and then be shrunk below the floor by
the trim. Nothing re-checked, `heat_dropped` stayed empty, and consolidate silently discarded it:

    return [t for t in trades if risk(t) >= min_risk_frac]

This is what ate BTCUSDT and ETHUSDT at cy293 (silent_dropped named them). It hits the tightest
stops first, because risk = notional * stop_frac / equity — BTC's stop was 0.93% and ETH's 1.22%
against LINK 3.05% and HYPE 2.68%, so the majors sit nearest the floor at any given notional.
"""
from futures_fund.models import TradeProposal
from futures_fund.neutral_book import presize_and_balance

EQ = 10000.0
DUST = 0.001


def _p(sym, direction, stop_frac, entry=100.0):
    stop = entry * (1 - stop_frac) if direction == "long" else entry * (1 + stop_frac)
    tp = entry * (1 + 2.2 * stop_frac) if direction == "long" else entry * (1 - 2.2 * stop_frac)
    return TradeProposal(symbol=sym, direction=direction, entry=entry, stop=stop,
                         take_profits=[tp], atr=entry * stop_frac / 2, funding_rate=0.0,
                         confidence=0.6, horizon_hours=8, rationale="x",
                         falsifiable_prediction="y")


def _risk(p, equity=EQ, ptr=0.010):
    """Stop-risk fraction the leg will contribute — what consolidate tests against the floor."""
    from futures_fund.notional_sizing import notional_to_risk_pct
    dist = abs(p.entry - p.stop)
    notional = p.risk_mult * ptr * equity * abs(p.entry) / dist
    return notional_to_risk_pct(notional, p.entry, p.stop, equity)


def test_no_kept_leg_is_left_below_the_dust_floor():
    """The cy293 shape: a lopsided book (2 long vs 3 short) forces a symmetric trim, and the
    tight-stop majors are the ones it pushes under."""
    props = [_p("LINK", "long", 0.0305), _p("BTW", "long", 0.2207),
             _p("BTC", "short", 0.0093), _p("ETH", "short", 0.0122),
             _p("HYPE", "short", 0.0268)]
    heat = {p.symbol: 0.08 for p in props}
    kept, summ = presize_and_balance(props, equity=EQ, per_trade_risk_pct=0.010,
                                     heat_headroom_by_symbol=heat, dust_risk_frac=DUST)
    below = [p.symbol for p in kept if _risk(p) < DUST]
    assert below == [], f"kept legs below the dust floor would be silently dropped: {below}"


def test_a_leg_trimmed_under_the_floor_is_reported_not_kept():
    """If the trim makes a leg unviable it must appear in heat_dropped — never vanish downstream."""
    props = [_p("LONG1", "long", 0.20),
             _p("TIGHT", "short", 0.002), _p("WIDE", "short", 0.10)]
    heat = {p.symbol: 0.08 for p in props}
    kept, summ = presize_and_balance(props, equity=EQ, per_trade_risk_pct=0.010,
                                     heat_headroom_by_symbol=heat, dust_risk_frac=DUST)
    kept_syms = {p.symbol for p in kept}
    for p in kept:
        assert _risk(p) >= DUST
    # anything excluded is accounted for, not silently missing
    assert set(summ["heat_dropped"]) == {"LONG1", "TIGHT", "WIDE"} - kept_syms


def test_summary_counts_stay_consistent_with_what_is_kept():
    props = [_p("LINK", "long", 0.0305), _p("BTW", "long", 0.2207),
             _p("BTC", "short", 0.0093), _p("ETH", "short", 0.0122),
             _p("HYPE", "short", 0.0268)]
    heat = {p.symbol: 0.08 for p in props}
    kept, summ = presize_and_balance(props, equity=EQ, per_trade_risk_pct=0.010,
                                     heat_headroom_by_symbol=heat, dust_risk_frac=DUST)
    assert summ["n_kept"] == len(kept)
    assert summ["n_dropped"] == len(props) - len(kept)


def test_a_healthy_balanced_book_is_untouched():
    """No false positives: equal sides with comfortable stops keep every leg."""
    props = [_p(f"L{i}", "long", 0.05) for i in range(3)] + \
            [_p(f"S{i}", "short", 0.05) for i in range(3)]
    heat = {p.symbol: 0.08 for p in props}
    kept, summ = presize_and_balance(props, equity=EQ, per_trade_risk_pct=0.010,
                                     heat_headroom_by_symbol=heat, dust_risk_frac=DUST)
    assert len(kept) == 6 and summ["heat_dropped"] == []


def test_heat_blind_callers_are_unaffected():
    """Without heat headroom the dust machinery stays off, exactly as before."""
    props = [_p("A", "long", 0.05), _p("B", "short", 0.002)]
    kept, summ = presize_and_balance(props, equity=EQ, per_trade_risk_pct=0.010)
    assert len(kept) == 2 and summ["heat_dropped"] == []
