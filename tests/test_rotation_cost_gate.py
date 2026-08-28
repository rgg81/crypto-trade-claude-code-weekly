"""A rotation must repay its own round-trip cost.

Measured over the desk's own 353-cycle record, the price IC of every traded signal at the 4h
rebalance horizon is ~0 (|t| < 0.1). With no price edge, a rotation's only expected income is the
funding differential between challenger and incumbent — so a rotation that cannot cover two taker
fills out of carry is a guaranteed loss. These tests pin that rule AND the invariant that vetoing
must never unbalance the dollar-neutral book.
"""
from __future__ import annotations

import pytest

from futures_fund.blended_score import apply_rotation_cost_gate
from futures_fund.costs import TAKER_RATE


def _brief(sym, funding, interval=8.0):
    return {"symbol": sym, "funding_rate": funding, "funding_interval_hours": interval}


def _plan(keep_long=(), keep_short=(), open_long=(), open_short=(), close=()):
    return {"keep_long": list(keep_long), "keep_short": list(keep_short),
            "open_long": list(open_long), "open_short": list(open_short),
            "close": list(close)}


def _sides(plan):
    return (len(plan["keep_long"]) + len(plan["open_long"]),
            len(plan["keep_short"]) + len(plan["open_short"]))


def test_rotation_with_no_carry_advantage_is_vetoed():
    """Identical funding on both names => zero edge, two fills of cost => must not rotate."""
    briefs = {"OLD": _brief("OLD", 0.0001), "NEW": _brief("NEW", 0.0001)}
    plan = _plan(keep_long=["A", "B"], open_long=["NEW"], close=["OLD"],
                 keep_short=["X", "Y", "Z"])
    out = apply_rotation_cost_gate(plan, briefs, {"OLD": "long"}, equity=10_000.0, n_per_side=3)
    assert out["close"] == []
    assert out["open_long"] == []
    assert "OLD" in out["keep_long"]


def test_rotation_with_large_carry_advantage_survives():
    """Challenger pays far more carry than the incumbent => the swap earns its cost."""
    briefs = {"OLD": _brief("OLD", 0.0), "NEW": _brief("NEW", -0.02, interval=1.0)}
    plan = _plan(keep_long=["A", "B"], open_long=["NEW"], close=["OLD"],
                 keep_short=["X", "Y", "Z"])
    out = apply_rotation_cost_gate(plan, briefs, {"OLD": "long"}, equity=10_000.0, n_per_side=3)
    assert out["close"] == ["OLD"]
    assert out["open_long"] == ["NEW"]
    assert "OLD" not in out["keep_long"]


@pytest.mark.parametrize("n_per_side", [2, 3, 4])
def test_side_counts_are_invariant_under_vetoing(n_per_side):
    """THE critical invariant: gating may never unbalance the book."""
    keep_l = [f"L{i}" for i in range(n_per_side - 1)]
    keep_s = [f"S{i}" for i in range(n_per_side)]
    briefs = {"OLD": _brief("OLD", 0.0001), "NEW": _brief("NEW", 0.0001)}
    plan = _plan(keep_long=keep_l, open_long=["NEW"], close=["OLD"], keep_short=keep_s)
    before = _sides(plan)
    out = apply_rotation_cost_gate(plan, briefs, {"OLD": "long"}, equity=10_000.0,
                                   n_per_side=n_per_side)
    assert _sides(out) == before


def test_gate_never_invents_a_new_leg():
    briefs = {"OLD": _brief("OLD", 0.0001), "NEW": _brief("NEW", 0.0001)}
    plan = _plan(keep_long=["A", "B"], open_long=["NEW"], close=["OLD"],
                 keep_short=["X", "Y", "Z"])
    out = apply_rotation_cost_gate(plan, briefs, {"OLD": "long"}, equity=10_000.0, n_per_side=3)
    everything = set(out["keep_long"] + out["keep_short"] + out["open_long"] + out["open_short"])
    assert everything <= {"A", "B", "X", "Y", "Z", "OLD", "NEW"}


def test_unpaired_close_is_left_alone():
    """A leg that crossed to the other sleeve MUST still close (it cannot same-cycle flip);
    there is no paired open to cancel, so the gate must not resurrect it."""
    briefs = {"OLD": _brief("OLD", 0.0001)}
    plan = _plan(keep_long=["A", "B"], close=["OLD"], keep_short=["X", "Y", "Z"])
    out = apply_rotation_cost_gate(plan, briefs, {"OLD": "long"}, equity=10_000.0, n_per_side=3)
    assert out["close"] == ["OLD"]
    assert "OLD" not in out["keep_long"]


def test_empty_plan_is_unchanged():
    out = apply_rotation_cost_gate(_plan(), {}, {}, equity=10_000.0, n_per_side=3)
    assert out == _plan()


def test_assumed_price_edge_can_justify_a_rotation():
    """price_edge_bps is the caller's assumed price alpha. It defaults to 0 because the desk has
    NONE at 4h; a large enough assumption must still be able to pay for the swap."""
    briefs = {"OLD": _brief("OLD", 0.0001), "NEW": _brief("NEW", 0.0001)}
    plan = _plan(keep_long=["A", "B"], open_long=["NEW"], close=["OLD"],
                 keep_short=["X", "Y", "Z"])
    out = apply_rotation_cost_gate(plan, briefs, {"OLD": "long"}, equity=10_000.0,
                                   n_per_side=3, price_edge_bps=500.0)
    assert out["close"] == ["OLD"]


def test_cost_scales_with_leg_notional_not_hardcoded():
    """A bigger book has proportionally bigger cost AND carry, so the verdict is scale-free."""
    briefs = {"OLD": _brief("OLD", 0.0), "NEW": _brief("NEW", -0.02, interval=1.0)}
    plan_a = _plan(keep_long=["A", "B"], open_long=["NEW"], close=["OLD"],
                   keep_short=["X", "Y", "Z"])
    plan_b = _plan(keep_long=["A", "B"], open_long=["NEW"], close=["OLD"],
                   keep_short=["X", "Y", "Z"])
    small = apply_rotation_cost_gate(plan_a, briefs, {"OLD": "long"}, equity=1_000.0, n_per_side=3)
    big = apply_rotation_cost_gate(plan_b, briefs, {"OLD": "long"}, equity=1_000_000.0,
                                   n_per_side=3)
    assert small["close"] == big["close"] == ["OLD"]


def test_taker_rate_is_sourced_from_costs_module():
    """Single source of truth: the gate must price fills off costs.TAKER_RATE, so a fee-schedule
    change flows through instead of silently diverging."""
    briefs = {"OLD": _brief("OLD", 0.0), "NEW": _brief("NEW", -0.0006)}
    plan = _plan(keep_long=["A", "B"], open_long=["NEW"], close=["OLD"],
                 keep_short=["X", "Y", "Z"])
    base = apply_rotation_cost_gate(plan, briefs, {"OLD": "long"}, equity=10_000.0, n_per_side=3,
                                    slippage_bps=0.0)
    # carry over the default 4-cycle horizon must beat exactly 2 x TAKER_RATE to survive
    leg = 10_000.0 / 6.0
    carry = 0.0006 * (4 * 4.0 / 8.0) * leg
    assert (base["close"] == ["OLD"]) is (carry > 2 * leg * TAKER_RATE)


def test_incumbent_without_a_brief_is_never_resurrected():
    """REGRESSION. A held symbol that has dropped out of the briefs (cy205 stale ex-holding) cannot
    be priced and has no downstream structure — vetoing its rotation would crash the planner at
    `by_sym[sym]`. The rotation must proceed untouched."""
    briefs = {"NEW": _brief("NEW", 0.0001)}          # OLD deliberately absent
    plan = _plan(keep_long=["A", "B"], open_long=["NEW"], close=["OLD"],
                 keep_short=["X", "Y", "Z"])
    out = apply_rotation_cost_gate(plan, briefs, {"OLD": "long"}, equity=10_000.0, n_per_side=3)
    assert out["close"] == ["OLD"]
    assert out["open_long"] == ["NEW"]
    assert "OLD" not in out["keep_long"]


def test_challenger_without_a_brief_is_not_priced_off_zero_carry():
    briefs = {"OLD": _brief("OLD", 0.0001)}          # NEW deliberately absent
    plan = _plan(keep_long=["A", "B"], open_long=["NEW"], close=["OLD"],
                 keep_short=["X", "Y", "Z"])
    out = apply_rotation_cost_gate(plan, briefs, {"OLD": "long"}, equity=10_000.0, n_per_side=3)
    assert out["open_long"] == ["NEW"]
    assert out["close"] == ["OLD"]


def test_each_challenger_is_allocated_to_exactly_one_incumbent():
    """ADVERSARIAL. Two simultaneous rotations on one side: the first pays (challenger has strong
    carry), the second does not. A challenger consumed by a surviving pair must not be re-priced
    against the next incumbent — otherwise the gate cancels the WRONG open."""
    briefs = {
        "OLD1": _brief("OLD1", 0.0), "OLD2": _brief("OLD2", 0.0),
        "RICH": _brief("RICH", -0.02, interval=1.0),   # huge carry -> its swap pays
        "POOR": _brief("POOR", 0.0),                   # no carry -> its swap cannot pay
    }
    # Order matters: challengers are consumed from the END, so RICH (the paying swap) is priced
    # FIRST. With the allocation bug, RICH is then re-priced against OLD2 as well and BOTH
    # rotations survive — POOR's unpayable swap slips through untouched.
    plan = _plan(keep_long=["A"], open_long=["POOR", "RICH"], close=["OLD1", "OLD2"],
                 keep_short=["X", "Y", "Z"])
    out = apply_rotation_cost_gate(plan, briefs, {"OLD1": "long", "OLD2": "long"},
                                   equity=10_000.0, n_per_side=3)
    # POOR's rotation must be the one cancelled; RICH must survive.
    assert "RICH" in out["open_long"], "the paying rotation was cancelled"
    assert "POOR" not in out["open_long"], "the non-paying rotation survived"
    assert len(out["keep_long"]) + len(out["open_long"]) == 3


def test_unscored_incumbent_is_never_resurrected():
    """REGRESSION (cy340 TRUMP). A held name that has turned untradeable still HAS a brief but is
    dropped by composite_scores. Retaining it feeds a scoreless symbol to the deployment top-up,
    which reopens it and crashes on score_of[sym]. It must be allowed to leave."""
    briefs = {"OLD": _brief("OLD", 0.0001), "NEW": _brief("NEW", 0.0001)}
    plan = _plan(keep_long=["A", "B"], open_long=["NEW"], close=["OLD"],
                 keep_short=["X", "Y", "Z"])
    out = apply_rotation_cost_gate(plan, briefs, {"OLD": "long"}, equity=10_000.0, n_per_side=3,
                                   keepable={"A", "B", "NEW", "X", "Y", "Z"})  # OLD absent
    assert out["close"] == ["OLD"]
    assert out["open_long"] == ["NEW"]
    assert "OLD" not in out["keep_long"]


def test_scored_incumbent_is_still_vetoable():
    """The keepable guard must not disable the gate for legitimate incumbents."""
    briefs = {"OLD": _brief("OLD", 0.0001), "NEW": _brief("NEW", 0.0001)}
    plan = _plan(keep_long=["A", "B"], open_long=["NEW"], close=["OLD"],
                 keep_short=["X", "Y", "Z"])
    out = apply_rotation_cost_gate(plan, briefs, {"OLD": "long"}, equity=10_000.0, n_per_side=3,
                                   keepable={"OLD", "A", "B", "NEW", "X", "Y", "Z"})
    assert out["close"] == []
    assert "OLD" in out["keep_long"]
