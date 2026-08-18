"""A starved sleeve must open FEWER legs, never ZERO legs.

Live failure, cy295 (FLAT! VIOLATION — the mandate's one unbreakable invariant):

    submitted 3 shorts (BTC/ETH/XRP) against a held long sleeve of $1482.83
    -> symmetric trim caps the short side at held_long, split 3 ways = $494.28/leg
    -> every leg lands under ITS OWN consolidate dust floor (0.001 * equity / stop_frac):

           leg    dust floor   share@3   verdict
           BTC       947.93     494.28   dust
           ETH       773.18     494.28   dust
           XRP       698.28     494.28   dust

    (floors here and below are recomputed from THIS file's rounded EQ/stop-fraction constants, so
    they differ in the cents from the live cycle's; the mechanism and the verdicts are identical.)

    -> `_viable` drops all three IN ONE PASS, the re-water-fill has no survivors to spread
       the budget over, and the book executes L3/S0 — NAKED LONG.

The bug is the all-at-once drop. The budget was never the problem: $1482.83 on ONE leg clears
every floor above with room to spare. Dropping the most notional-hungry leg and re-spreading its
budget over the survivors converges to the largest set of legs that each clear the floor.

This matters more than under-deployment: a module documented as "only ever SHRINK risk" turned a
dollar-neutral book into a one-sided directional bet. Emptying a sleeve ADDS risk.
"""
import pytest

from futures_fund.models import TradeProposal
from futures_fund.neutral_book import presize_and_balance
from futures_fund.notional_sizing import notional_to_risk_pct

EQ = 10069.89
HELD_LONG = 1482.83
PTR = 0.0025                 # caution tier (0.5) x -5% step-down breaker (0.5) x 0.010
FLOOR = 0.001


def _short(sym, stop_frac, entry=100.0):
    stop = entry * (1 + stop_frac)
    return TradeProposal(symbol=sym, direction="short", entry=entry, stop=stop,
                         take_profits=[entry * (1 - 2.2 * stop_frac)],
                         atr=entry * stop_frac / 2, funding_rate=0.0, confidence=0.6,
                         horizon_hours=8, rationale="x", falsifiable_prediction="y")


def _cy295():
    """The live cy295 submission: three shorts against a held-only long sleeve."""
    return [_short("BTCUSDT", 0.010623), _short("ETHUSDT", 0.013024),
            _short("XRPUSDT", 0.014421)]


def _presize(props, **kw):
    return presize_and_balance(
        props, equity=EQ, per_trade_risk_pct=PTR,
        held_long=HELD_LONG, held_short=0.0,
        heat_headroom_by_symbol={p.symbol: 0.08 for p in props},
        dust_risk_frac=FLOOR, **kw)


def _notional(p):
    return p.risk_mult * PTR * EQ * abs(p.entry) / abs(p.entry - p.stop)


def test_the_starved_sleeve_is_not_emptied():
    """THE REGRESSION. cy295 executed L3/S0; the desk must never be handed a naked sleeve."""
    kept, summ = _presize(_cy295())
    assert kept, (
        f"whole short sleeve dropped -> FLAT violation (heat_dropped={summ['heat_dropped']})")
    assert summ["gross_short_target"] > 0.0


def test_the_survivor_clears_its_own_dust_floor():
    """Fewer legs is only correct if the survivors actually land; otherwise consolidate eats them
    silently and we are back to a naked sleeve one layer down."""
    kept, _ = _presize(_cy295())
    assert len(kept) == 2, "vacuous otherwise: an empty sleeve passes a for-loop over `kept`"
    for p in kept:
        assert notional_to_risk_pct(_notional(p), p.entry, p.stop, EQ) >= FLOOR, (
            f"{p.symbol} still lands as dust and would be dropped silently by consolidate")


def test_it_keeps_the_largest_viable_leg_count():
    """$1482.83 supports TWO of the three legs — assert we find that maximum, not an over-eager
    cull to one (or to zero).

    `_waterfill` is max-min fair — an equal share, capped at each leg's ceiling — NOT a
    proportional split. With three legs the budget is well under every ceiling, so all three DO get
    an equal $494.28 (the header table). With two legs the picture changes: ETH and XRP both pin at
    their rm=1 ceilings ($1932.95 / $1745.70, summing to $3678.65), so the symmetric trim scales
    that pinned pair by 1482.83/3678.65 = 0.4031 and lands ETH at $779.15 (floor $773.18) and XRP
    at $703.68 (floor $698.28) — both clearing by a hair. A two-way EQUAL split would have been
    $741.42 each and sunk ETH; it is the ceiling PINNING, not any proportionality rule, that makes
    the pair fit. (Believing the split was proportional in general is what produced the wrong drop
    key that `test_drops_the_highest_floor_not_the_largest_shortfall` now pins.)
    """
    kept, summ = _presize(_cy295())
    assert sorted(p.symbol for p in kept) == ["ETHUSDT", "XRPUSDT"], [p.symbol for p in kept]
    assert summ["heat_dropped"] == ["BTCUSDT"], "only the most notional-hungry leg should go"


def test_the_book_lands_dollar_neutral():
    """The mandate is DOLLAR neutrality, not leg-count symmetry. One short at the full budget is
    a neutral book; three dropped shorts is a 100% tilt."""
    kept, summ = _presize(_cy295())
    gl, gs = summ["gross_long_target"], summ["gross_short_target"]
    assert abs(gl - gs) / max(gl, gs) < 0.01, f"tilt not closed: long {gl:.2f} vs short {gs:.2f}"


def test_a_well_fed_sleeve_keeps_every_leg():
    """No over-culling: when the budget comfortably feeds all three, all three must survive."""
    kept, summ = presize_and_balance(
        _cy295(), equity=EQ, per_trade_risk_pct=PTR, held_long=EQ / 2, held_short=0.0,
        heat_headroom_by_symbol={p.symbol: 0.08 for p in _cy295()}, dust_risk_frac=FLOOR)
    assert len(kept) == 3, [p.symbol for p in kept]
    assert summ["heat_dropped"] == []


def test_a_genuinely_undeployable_sleeve_still_reports_it():
    """When even ONE leg cannot clear the floor the drop is real — it must stay surfaced in
    `heat_dropped` rather than being hidden by the new retry loop."""
    kept, summ = presize_and_balance(
        _cy295()[:1], equity=EQ, per_trade_risk_pct=PTR, held_long=10.0, held_short=0.0,
        heat_headroom_by_symbol={"BTCUSDT": 0.08}, dust_risk_frac=FLOOR)
    assert kept == []
    assert summ["heat_dropped"] == ["BTCUSDT"]


def test_risk_never_grows_above_the_intended_per_leg_budget():
    """Concentrating the budget must not push a survivor past rm=1.0 — the pre-sizer may only
    ever shrink risk, and the gate clamps rm to (0,1] anyway."""
    kept, _ = _presize(_cy295())
    assert len(kept) == 2, "vacuous otherwise: all() over an empty sleeve is True"
    assert all(0.0 < p.risk_mult <= 1.0 for p in kept), [(p.symbol, p.risk_mult) for p in kept]


# The cheapest leg to deploy is the widest stop: floor = FLOOR * EQ / stop_frac.
CHEAPEST_FLOOR = FLOOR * EQ / 0.014421          # XRP, $698.28


@pytest.mark.parametrize("held", [700.0, 800.0, 1482.83, 2500.0, 5034.94])
def test_every_feasible_held_size_still_deploys(held):
    """The ratchet: cy295's guard trimmed the long sleeve 35%, which shrinks the next cycle's
    short budget, dusts it again, and trims again — 1482 -> 964 -> 626. Every rung at or above
    the cheapest floor must still open a short, so the ratchet never gets started."""
    assert held >= CHEAPEST_FLOOR, "test rung must be feasible by construction"
    props = _cy295()
    kept, summ = presize_and_balance(
        props, equity=EQ, per_trade_risk_pct=PTR, held_long=held, held_short=0.0,
        heat_headroom_by_symbol={p.symbol: 0.08 for p in props}, dust_risk_frac=FLOOR)
    assert kept, f"held_long={held} emptied the short sleeve (dropped {summ['heat_dropped']})"


def test_below_the_cheapest_floor_the_drop_is_real_and_surfaced():
    """Honest boundary. Under ~$698 of held long, NO short clears consolidate's dust floor, so a
    neutral short genuinely cannot be opened — every leg is correctly dropped.

    The pre-sizer must NOT "fix" this by opening at the floor anyway: that would GROW gross to
    close a tilt, and this module may only ever shrink. Neutralising a book this starved is the
    post-gate guard's job, which trims the oversized LONG sleeve down toward the short — the same
    invariant approached from the side that reduces exposure instead of adding it.
    """
    props = _cy295()
    kept, summ = presize_and_balance(
        props, equity=EQ, per_trade_risk_pct=PTR, held_long=400.0, held_short=0.0,
        heat_headroom_by_symbol={p.symbol: 0.08 for p in props}, dust_risk_frac=FLOOR)
    assert kept == []
    assert sorted(summ["heat_dropped"]) == ["BTCUSDT", "ETHUSDT", "XRPUSDT"], (
        "an infeasible sleeve must still name every leg it dropped — never a silent flat book")


# ---------------------------------------------------------------------------------------------
# Adversarial-review regressions. My first fix dropped the leg with the largest SHORTFALL (floor
# minus allocation). That ranks by a quantity that depends on the allocation, and allocations are
# not uniform once a ceiling binds — so it sacrificed cheap-but-capped legs and kept expensive
# ones. All three cases below were found by the review, reproduced here first, and fail on the
# shortfall key. None of my original tests passed `aggregate_heat_headroom`, which orchestration
# ALWAYS passes (orchestration.py:982) — two of these cover that path.
# ---------------------------------------------------------------------------------------------
def _leg(sym, direction, stop_frac, entry=100.0):
    stop = entry * (1 - stop_frac) if direction == "long" else entry * (1 + stop_frac)
    tp = entry * (1 + 2.2 * stop_frac) if direction == "long" else entry * (1 - 2.2 * stop_frac)
    return TradeProposal(symbol=sym, direction=direction, entry=entry, stop=stop,
                         take_profits=[tp], atr=entry * stop_frac / 2, funding_rate=0.0,
                         confidence=0.6, horizon_hours=8, rationale="x",
                         falsifiable_prediction="y")


def test_drops_the_highest_floor_not_the_largest_shortfall():
    """Greedy must sacrifice the most EXPENSIVE leg, judged by floor alone.

    AAA has a $500.00 floor but a $1000.00 ceiling; BBB/CCC have $100.00 floors and $111.11
    ceilings. Trimmed to a $600.00 budget the shares are AAA $490.90, BBB/CCC $54.50 each — so the
    largest SHORTFALL is BBB/CCC ($45.50) even though they are by far the cheapest to keep. The
    shortfall key kills them both and keeps one oversized AAA; ranking by floor drops AAA and keeps
    the pair, which is the true optimum here and also honours the N4 concentration intent (two
    names rather than one dominating a scarce sleeve).
    """
    props = [_leg("AAAUSDT", "short", 0.02), _leg("BBBUSDT", "short", 0.10),
             _leg("CCCUSDT", "short", 0.10)]
    kept, summ = presize_and_balance(
        props, equity=10000.0, per_trade_risk_pct=0.01, held_long=600.0, held_short=0.0,
        heat_headroom_by_symbol={"AAAUSDT": 0.002, "BBBUSDT": 0.0011111,
                                 "CCCUSDT": 0.0011111}, dust_risk_frac=FLOOR)
    assert sorted(p.symbol for p in kept) == ["BBBUSDT", "CCCUSDT"], [p.symbol for p in kept]
    assert summ["heat_dropped"] == ["AAAUSDT"]


def test_shortfall_ranking_could_still_empty_a_sleeve():
    """The headline failure, one layer down: ranking by shortfall re-created the naked book.

    Floors $367.49 / $531.45 / $681.33 against a $490.90 budget — exactly one leg is affordable.
    Mid-loop the affordable leg (X0, small ceiling) shows a $256.60 shortfall while X1 (large
    ceiling) shows $151.50, so the shortfall key drops X0 and then everything else dies too. Floor
    ranking sheds the two unaffordable legs and keeps X0.
    """
    props = [_leg("X0USDT", "short", 0.027402), _leg("X1USDT", "short", 0.018948),
             _leg("X2USDT", "short", 0.014780)]
    kept, summ = presize_and_balance(
        props, equity=EQ, per_trade_risk_pct=0.01, held_long=490.9, held_short=0.0,
        heat_headroom_by_symbol={"X0USDT": 0.002, "X1USDT": 0.08, "X2USDT": 0.02},
        aggregate_heat_headroom=0.02, dust_risk_frac=FLOOR)
    assert [p.symbol for p in kept] == ["X0USDT"], (
        f"sleeve emptied again (dropped {summ['heat_dropped']})")


def test_trim_zeroed_legs_must_not_hold_the_aggregate_heat_budget():
    """A leg the symmetric trim zeroed must go at once, not one per pass.

    held_long ($3020.97) is already past the balance point, so the long side is scaled to zero and
    L0/L1 can never open. Dropping them one at a time keeps them alive for extra passes, and each
    pass re-water-fills them to their FULL ceilings before `_fit_aggregate` runs — so they eat the
    0.008 aggregate budget and scale the live short legs down. S0 then lands at $253.30 against its
    $274.39 floor and is culled: strictly WORSE than the all-at-once cull this replaced, which had
    both longs gone before that point and left S0 its full $411.59.

    This is why "drop one per pass" cannot be unconditional — the one-at-a-time rule exists to give
    survivors a bigger share, and a zeroed leg has no share to give.
    """
    props = [_leg("L0USDT", "long", 0.064970), _leg("L1USDT", "long", 0.011324),
             _leg("S0USDT", "short", 0.036699), _leg("S1USDT", "short", 0.100529)]
    kept, summ = presize_and_balance(
        props, equity=EQ, per_trade_risk_pct=0.01, held_long=3020.967, held_short=400.0,
        heat_headroom_by_symbol={"L0USDT": 0.08, "L1USDT": 0.08,
                                 "S0USDT": 0.0015, "S1USDT": 0.0015},
        aggregate_heat_headroom=0.008, dust_risk_frac=FLOOR)
    assert sorted(p.symbol for p in kept) == ["S0USDT", "S1USDT"], [p.symbol for p in kept]
    assert sorted(summ["heat_dropped"]) == ["L0USDT", "L1USDT"]


def test_the_aggregate_budget_still_binds_on_the_returned_book():
    """Guard the fix above: clearing zeroed legs must not let the survivors breach the budget."""
    props = [_leg("L0USDT", "long", 0.064970), _leg("L1USDT", "long", 0.011324),
             _leg("S0USDT", "short", 0.036699), _leg("S1USDT", "short", 0.100529)]
    budget = 0.008
    kept, _ = presize_and_balance(
        props, equity=EQ, per_trade_risk_pct=0.01, held_long=3020.967, held_short=400.0,
        heat_headroom_by_symbol={p.symbol: 0.0015 for p in props},
        aggregate_heat_headroom=budget, dust_risk_frac=FLOOR)
    total = sum(notional_to_risk_pct(
        p.risk_mult * 0.01 * EQ * abs(p.entry) / abs(p.entry - p.stop), p.entry, p.stop, EQ)
        for p in kept)
    assert total <= budget + 1e-9, f"summed stop-risk {total:.6f} breaches budget {budget}"
