"""A deployment top-up must pay for itself, exactly like a rotation.

`deployment_resizes` grows a frozen book toward ~1x by CLOSE+REOPEN, which costs two taker fills on
the WHOLE leg to add only the shortfall. Its payoff is whatever the added notional earns. Measured
over 353 live cycles and a 1-year 60-symbol panel, this desk has NO price alpha at the 4h horizon
(blend rank-IC +0.0010, t=+0.05), so the added notional's only expected income is carry — and carry
on a shortfall is an order of magnitude below two fills on the full leg. Growing a zero-edge book
buys more fees and more variance, not more edge.

The capability is NOT removed: `resize_price_edge_bps` re-enables top-ups for anyone who believes
there IS a price edge to deploy into. It defaults to 0 because that is the measurement.
"""
from __future__ import annotations

from futures_fund.blended_score import deployment_resizes


def _book(n=3):
    """A badly under-deployed 3v3 book: every leg far below its slot target."""
    holdings, notional, stops = {}, {}, {}
    for i in range(n):
        for d in ("long", "short"):
            s = f"{d[0].upper()}{i}"
            holdings[s] = d
            notional[s] = 50.0            # tiny vs a ~$800 slot
            stops[s] = 0.05
    return holdings, notional, stops


def test_starved_book_is_flagged_when_cost_is_not_considered():
    """Baseline: without funding data the legacy behaviour is unchanged (flags the starved legs)."""
    h, n, sf = _book()
    out = deployment_resizes(h, n, equity=10_000.0, n_per_side=3, per_trade_risk_pct=0.01,
                             stop_frac_by_sym=sf, planned_opens_by_side={"long": 0, "short": 0})
    assert out, "a starved book should be flagged when no cost model is supplied"


def test_top_up_is_vetoed_when_carry_cannot_repay_two_fills():
    h, n, sf = _book()
    funding = {s: (0.0001, 8.0) for s in h}      # ordinary carry: nowhere near two fills
    out = deployment_resizes(h, n, equity=10_000.0, n_per_side=3, per_trade_risk_pct=0.01,
                             stop_frac_by_sym=sf, planned_opens_by_side={"long": 0, "short": 0},
                             funding_by_sym=funding)
    assert out == set(), "an unpayable top-up must not be executed"


def test_enormous_carry_still_justifies_a_top_up():
    """The gate prices carry honestly — it does not simply disable the mechanism."""
    h, n, sf = _book()
    funding = {s: (-0.02, 1.0) if h[s] == "long" else (0.02, 1.0) for s in h}
    out = deployment_resizes(h, n, equity=10_000.0, n_per_side=3, per_trade_risk_pct=0.01,
                             stop_frac_by_sym=sf, planned_opens_by_side={"long": 0, "short": 0},
                             funding_by_sym=funding)
    assert out, "a top-up whose carry clearly beats its cost must still fire"


def test_assumed_price_edge_re_enables_top_ups():
    h, n, sf = _book()
    funding = {s: (0.0001, 8.0) for s in h}
    out = deployment_resizes(h, n, equity=10_000.0, n_per_side=3, per_trade_risk_pct=0.01,
                             stop_frac_by_sym=sf, planned_opens_by_side={"long": 0, "short": 0},
                             funding_by_sym=funding, resize_price_edge_bps=500.0)
    assert out, "a caller asserting a real price edge must be able to deploy into it"


def test_gate_only_ever_shrinks_the_flagged_set():
    """Shrink-only: the cost gate may remove candidates, never add one."""
    h, n, sf = _book()
    funding = {s: (0.0001, 8.0) for s in h}
    base = deployment_resizes(h, n, equity=10_000.0, n_per_side=3, per_trade_risk_pct=0.01,
                              stop_frac_by_sym=sf, planned_opens_by_side={"long": 0, "short": 0})
    gated = deployment_resizes(h, n, equity=10_000.0, n_per_side=3, per_trade_risk_pct=0.01,
                               stop_frac_by_sym=sf, planned_opens_by_side={"long": 0, "short": 0},
                               funding_by_sym=funding)
    assert gated <= base


def test_legacy_path_without_plan_is_also_gated():
    """The no-plan (conservative) branch must honour the same economics."""
    h, n, sf = _book()
    funding = {s: (0.0001, 8.0) for s in h}
    out = deployment_resizes(h, n, equity=10_000.0, n_per_side=3, per_trade_risk_pct=0.01,
                             stop_frac_by_sym=sf, funding_by_sym=funding)
    assert out == set()


def test_cost_gate_is_all_or_nothing_across_both_sides():
    """REGRESSION. `deployment_resizes` is book-level: legs must reopen on BOTH sides together, or
    the balancer pins each side to the smaller and the resized side just churns. Judging symbols
    individually let one side through and froze the other, producing a one-sided proposal set."""
    h, n, sf = _book()
    # Longs carry richly, shorts do not: a per-symbol gate would pass ONLY the longs.
    funding = {s: ((-0.02, 1.0) if h[s] == "long" else (0.0, 8.0)) for s in h}
    out = deployment_resizes(h, n, equity=10_000.0, n_per_side=3, per_trade_risk_pct=0.01,
                             stop_frac_by_sym=sf, planned_opens_by_side={"long": 0, "short": 0},
                             funding_by_sym=funding)
    sides = {h[s] for s in out}
    assert sides in ({"long", "short"}, set()), f"one-sided resize set {out} would churn the book"
