"""The two scalers BELOW the pre-sizer must never bind — pinned, not assumed.

Adversarial-review finding (MEDIUM). `neutral_book.presize_and_balance` predicts the size the gate
will land so it can pre-empt `consolidate`'s SILENT dust drop. But two things scale the book AFTER
the pre-sizer and BEFORE that dust filter, and the pre-sizer models neither:

    cycle.py:196   consolidate(approved, equity, max_heat - reserved, cvar_mult=cvar_mult)
    cycle.py:244   cluster_scale(to_open, held, equity, corr, cluster_cap)

That matters more than it used to. The sleeve-refill loop now converges on the LARGEST set of legs
that each clear the floor, and maximising leg count IS minimising margin — cy295's survivors landed
at 0.00100773 against a 0.001 floor, 0.8% of headroom. Any unmodelled scaler below that cliff dusts
them, and dusting a whole side is the naked-book violation again by another route.

Rather than model both (complexity in the layer that just caused an incident), this pins the reason
they are inert, so the assumption fails LOUDLY if the envelope ever moves:

* `cvar_risk_multiplier` drops to 0.5 only when the tail-mean of the last 30 trade returns is worse
  than -5% of equity. Measured live: CVaR -0.005946, worst single trade -0.69%, and per-trade risk
  is capped at 0.25% of equity at the stop. The trigger is ~8x beyond the desk's worst observed
  tail and unreachable without raising per-trade risk by an order of magnitude.
* `cluster_scale` binds only if one correlated same-direction cluster exceeds
  max(per_trade_risk_pct, 0.5 * max_heat). A full side is n_per_side legs at risk_mult <= 1, and
  every quadrant/tier leaves headroom — thinnest at low_vol_trend/healthy (0.045 vs 0.050).

If a caps change or a bigger `n_per_side` erases that headroom, these fail and the pre-sizer has to
start modelling the scalers for real.
"""
import pytest

from futures_fund.models import PortfolioHealth, RegimeState
from futures_fund.policy import caps_for

QUADRANTS = ["low_vol_trend", "high_vol_trend", "low_vol_range", "high_vol_range", "transition"]
N_PER_SIDE = 3                      # scripts/blended_book_cli.py --n-per-side default
EQ = 10000.0

# drawdown -> health tier (models.PortfolioHealth.tier): <5% healthy, >=5% caution, >=10% stressed
TIERS = {"healthy": 0.0, "caution": 0.06, "stressed": 0.12}


def _caps(quadrant, dd):
    return caps_for(RegimeState(quadrant=quadrant),
                    PortfolioHealth(equity=EQ * (1 - dd), peak_equity=EQ))


@pytest.mark.parametrize("quadrant", QUADRANTS)
@pytest.mark.parametrize("tier,dd", TIERS.items())
def test_a_full_side_never_reaches_the_cluster_cap(quadrant, tier, dd):
    """cluster_scale must not be able to trim a legitimately-sized sleeve.

    Worst case is every leg on one side correlated and at risk_mult = 1.0 (the gate's clamp), so
    the cluster's stop-risk is n_per_side * per_trade_risk_pct.
    """
    caps = _caps(quadrant, dd)
    cluster_cap = max(caps.per_trade_risk_pct, 0.5 * caps.max_heat)   # cycle.py:240
    full_side = N_PER_SIDE * caps.per_trade_risk_pct
    assert full_side <= cluster_cap, (
        f"{quadrant}/{tier}: a full side ({full_side:.4f}) reaches the cluster cap "
        f"({cluster_cap:.4f}) — cluster_scale can now dust legs the pre-sizer sized")


@pytest.mark.parametrize("quadrant", QUADRANTS)
@pytest.mark.parametrize("tier,dd", TIERS.items())
def test_a_full_neutral_book_never_reaches_the_gross_heat_cap(quadrant, tier, dd):
    """The same headroom for `consolidate`'s batch scaling, which the pre-sizer's
    `aggregate_heat_headroom` mirrors: position_risk sums GROSS stop-risk with no long/short
    offset, so a balanced book costs 2 * n_per_side legs against max_heat."""
    caps = _caps(quadrant, dd)
    full_book = 2 * N_PER_SIDE * caps.per_trade_risk_pct
    assert full_book <= caps.max_heat, (
        f"{quadrant}/{tier}: a full neutral book ({full_book:.4f}) exceeds max_heat "
        f"({caps.max_heat:.4f}) — consolidate will batch-scale and silently dust the remainder")


def test_the_thinnest_headroom_is_where_we_think_it_is():
    """Name the binding case so a caps edit that erodes it is obvious in the diff."""
    margins = {}
    for q in QUADRANTS:
        caps = _caps(q, 0.0)
        cap = max(caps.per_trade_risk_pct, 0.5 * caps.max_heat)
        margins[q] = (cap - N_PER_SIDE * caps.per_trade_risk_pct) / cap
    assert min(margins, key=margins.get) == "low_vol_trend"
    assert margins["low_vol_trend"] == pytest.approx(0.10, abs=0.005)


def test_the_cvar_derisk_cannot_fire_at_the_per_trade_risk_cap():
    """cvar_mult halves the whole book when the tail-mean loss breaches -5% of equity. A single
    trade cannot get there: per_trade_risk_pct tops out at 1.5% and risk_mult is clamped to 1."""
    worst_single_trade = max(_caps(q, 0.0).per_trade_risk_pct for q in QUADRANTS)
    assert worst_single_trade == 0.015
    assert worst_single_trade < 0.05, (
        "a single stop-out can now breach the CVaR threshold; the pre-sizer must model cvar_mult")
