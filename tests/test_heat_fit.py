"""The book must FIT the gate's heat cap, not discover it by being vetoed.

THE ARITHMETIC. The gate sizes by risk: qty = equity*ptr*rm / |entry-stop|. Feed that into
`portfolio_risk.position_risk` = qty*|entry-stop| / equity and the distance cancels:

    per-leg heat == ptr * rm            book heat == ptr * sum(rm)

So the cap `sum(ptr*rm) <= max_heat` is a constraint on the rm VECTOR. `safe_n_per_side` only ever
sized the leg COUNT, and off the dust floor at that — a lower bound on how SMALL a leg may be, which
is a different constraint from how LARGE the book may be in total. Nothing checked the actual rm
vector against the cap, so whether a rebalance fit was luck of the z-score distribution.

cy414 is what that costs:

    target L5/S5 -> gate: opened 4 closed 5 | VETOED USELESSUSDT: no heat headroom
                          (used 0.021 >= cap 0.020)
    -> L4/S5, tilt 0.056 -> NEUTRALITY GUARD trims the SHORT sleeve by 0.106 to re-balance

The desk lost a leg AND shrank the opposite sleeve to match it. Scaling the whole rm vector to fit
instead keeps all 10 legs and the symmetry, and only ever SHRINKS risk — which is exactly what a
non-protected pre-sizer is allowed to do. The gate's cap is untouched and still has the last word.

The floor matters as much as the ceiling: consolidate() silently deletes any leg under `dust_frac`
of equity, so scaling must never push a leg under it. Both bounds are satisfiable together whenever
safe_n_per_side's own feasibility test passes (2*n*dust_frac <= max_heat).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from xsection_book_cli import DUST_FRAC, fit_heat  # noqa: E402


def _heat(rm, ptr):
    return ptr * sum(rm.values())


def test_a_book_that_already_fits_is_left_alone():
    """Never shrink a book that the gate would have accepted as-is."""
    rm = {"A": 1.0, "B": 0.5}
    out = fit_heat(rm, ptr=0.001, max_heat=0.020)
    assert out == rm


def test_the_cy414_book_is_scaled_to_fit_instead_of_losing_a_leg():
    """THE regression: 10 legs at ptr*sum(rm)=0.021 against a 0.020 cap."""
    rm = {f"L{i}": v for i, v in enumerate([1.0, 0.9, 0.8, 0.7, 0.6])}
    rm |= {f"S{i}": v for i, v in enumerate([1.0, 0.85, 0.75, 0.65, 0.55])}
    ptr = 0.0027                      # -> sum(rm)=7.8, heat = 0.02106 > 0.020

    out = fit_heat(rm, ptr=ptr, max_heat=0.020)

    assert len(out) == len(rm), "every leg must survive — losing one is what we are fixing"
    assert _heat(out, ptr) <= 0.020 + 1e-12, "the scaled book must fit the cap"


def test_scaling_preserves_the_relative_sizing():
    """The factor's inverse-vol/z weighting must survive — this is a scale, not a reshape."""
    rm = {"A": 1.0, "B": 0.5, "C": 0.25}
    out = fit_heat(rm, ptr=0.01, max_heat=0.010)

    assert out["A"] / out["B"] == pytest.approx(rm["A"] / rm["B"])
    assert out["B"] / out["C"] == pytest.approx(rm["B"] / rm["C"])


def test_no_leg_is_scaled_under_the_dust_floor():
    """consolidate() deletes a sub-dust leg SILENTLY. Fitting the ceiling must not breach the
    floor — that would trade a veto (loud) for a dust drop (silent), which is strictly worse."""
    rm = {f"L{i}": 1.0 for i in range(5)} | {f"S{i}": 1.0 for i in range(5)}
    ptr = 0.0030

    out = fit_heat(rm, ptr=ptr, max_heat=0.020)

    for sym, v in out.items():
        assert ptr * v >= DUST_FRAC, f"{sym} scaled to {ptr * v:.5f}, under the {DUST_FRAC} floor"


def test_it_refuses_to_breach_the_floor_even_when_that_means_not_fitting():
    """If the cap and the floor genuinely cannot both hold, the pre-sizer must NOT silently
    produce a dust book. It leaves the legs at the floor and lets the gate's veto be the visible
    failure — a loud stop beats a quiet one."""
    rm = {f"L{i}": 1.0 for i in range(20)}
    ptr = 0.0030                      # floor alone needs 20*0.001 = 0.020 > cap 0.008

    out = fit_heat(rm, ptr=ptr, max_heat=0.008)

    for v in out.values():
        assert ptr * v >= DUST_FRAC, "must never scale a leg into the dust"


def test_rm_never_exceeds_one():
    """The gate clamps rm to (0,1]; emitting more would be silently truncated.

    The discriminating case is ptr < dust_frac, which puts the dust FLOOR itself above 1.0
    (floor_rm = dust_frac/ptr). Scaling alone can never raise rm — scale < 1 whenever we are over
    budget — so a fixture that merely fits exercises nothing: it returns early and a mutation
    removing the clamp survives. Reachable in practice once the breaker steps down far enough.
    """
    out = fit_heat({"A": 1.0, "B": 0.9}, ptr=0.0001, max_heat=0.50)
    assert all(v <= 1.0 for v in out.values())

    # floor_rm = 0.001/0.0005 = 2.0, and the book is over budget so the floor actually binds
    clamped = fit_heat({f"L{i}": 1.0 for i in range(10)}, ptr=0.0005, max_heat=0.002)
    assert clamped, "fixture check: must produce legs"
    assert all(v <= 1.0 for v in clamped.values()), (
        "rm above 1.0 is silently truncated by the gate — the clamp must hold")


def test_an_empty_or_zero_book_is_handled():
    assert fit_heat({}, ptr=0.001, max_heat=0.02) == {}
    assert fit_heat({"A": 1.0}, ptr=0.0, max_heat=0.02) == {"A": 1.0}


def test_the_scaled_book_is_what_the_gate_would_actually_charge():
    """Tie the model to the gate's own accounting, not to a restatement of it.

    position_risk(qty, entry, stop, equity) with the gate's own sizing must reproduce ptr*rm; if
    that identity ever breaks, this whole fit is computing the wrong quantity.
    """
    from futures_fund.portfolio_risk import position_risk

    equity, ptr, entry, stop = 10_000.0, 0.0027, 100.0, 70.0
    rm = {"A": 0.8}
    out = fit_heat(rm, ptr=ptr, max_heat=0.020)

    qty = equity * ptr * out["A"] / abs(entry - stop)
    charged = position_risk(qty, entry, stop, equity, "long")

    assert charged == pytest.approx(ptr * out["A"]), "heat must equal ptr*rm — the fit's premise"


def test_the_shipped_path_applies_the_fit():
    """`risk_mults` alone is what walked into the cy414 veto. The composed helper the CLI actually
    calls must fit the cap — otherwise fit_heat is dead code and the bug is still shipped."""
    from xsection_book_cli import book_risk_mults, risk_mults

    weights = {f"L{i}": 1.0 - i * 0.05 for i in range(5)}
    weights |= {f"S{i}": -(1.0 - i * 0.05) for i in range(5)}
    stop_frac = dict.fromkeys(weights, 0.30)
    ptr, cap = 0.00375, 0.020          # the live cy414 numbers

    raw = risk_mults(weights, stop_frac)
    fitted = book_risk_mults(weights, stop_frac, ptr=ptr, max_heat=cap)

    assert ptr * sum(raw.values()) > cap, "the fixture must reproduce the cy414 overshoot"
    assert ptr * sum(fitted.values()) <= cap + 1e-12, "the shipped path must fit the cap"
    assert len(fitted) == len(raw), "and must not do it by dropping legs"


def test_without_caps_the_shipped_path_is_unchanged():
    """No context / caps read failure must not silently reshape the book."""
    from xsection_book_cli import book_risk_mults, risk_mults

    weights = {"A": 1.0, "B": -1.0}
    stop_frac = {"A": 0.3, "B": 0.3}
    assert book_risk_mults(weights, stop_frac, ptr=None, max_heat=None) == \
        risk_mults(weights, stop_frac)


def test_effective_ptr_includes_the_DRAWDOWN_BREAKER_not_just_the_caps():
    """The gate charges `caps.per_trade_risk_pct * breaker.risk_multiplier * rm`.

    Reading caps alone understates every leg by 2x past the -5% step-down — which is precisely the
    live condition at cy414 (dd 7.04%) — and walks straight back into the veto this fixes. A
    mutation dropping the breaker survived every other test in this file.
    """
    from xsection_book_cli import effective_ptr

    from futures_fund.models import PortfolioHealth, RegimeState
    from futures_fund.policy import caps_for

    dd, eq, quad = 0.0704, 10_000.0, "low_vol_trend"          # the live cy414 drawdown
    h = PortfolioHealth(equity=eq, peak_equity=eq / (1 - dd), drawdown_from_peak=dd)

    caps_only = caps_for(RegimeState(quadrant=quad, trend="up", vol="low"), h).per_trade_risk_pct

    # Isolate the BREAKER at fixed health: the tier's own halving is already inside caps_only, so
    # anything below it is the step-down and nothing else.
    assert effective_ptr(h, dd, quad) == pytest.approx(caps_only * 0.5), (
        "past the -5% step-down the breaker halves per-trade risk; ignoring it doubles the "
        "book the fit thinks it can afford")


def test_effective_ptr_matches_the_gate_arithmetic_exactly():
    """Pin it to policy's own numbers so a caps change flows through instead of drifting."""
    from xsection_book_cli import effective_ptr

    from futures_fund.models import PortfolioHealth, RegimeState
    from futures_fund.policy import caps_for, circuit_breaker

    dd = 0.0704
    eq = 10_000.0
    h = PortfolioHealth(equity=eq, peak_equity=eq / (1 - dd), drawdown_from_peak=dd)
    quad = "low_vol_trend"

    expected = (caps_for(RegimeState(quadrant=quad, trend="up", vol="low"), h).per_trade_risk_pct
                * circuit_breaker(0.0, 0.0, 0.0, dd).risk_multiplier)

    assert effective_ptr(h, dd, quad) == pytest.approx(expected)


def test_an_unknown_quadrant_assumes_the_LARGEST_ptr_not_the_smallest():
    """This is the LIVE path — the desk's context carries `regime_state.regime`, not `quadrant`.

    For max_heat the strictest guess is the smallest (fallback_max_heat takes a min). For ptr it is
    the opposite: a LARGER ptr means each leg eats more of the budget, so fewer fit — guessing low
    would understate the book's heat and hand back the cy414 veto. Mirrors, not copies,
    fallback_max_heat's direction.
    """
    from xsection_book_cli import effective_ptr

    from futures_fund.models import PortfolioHealth, RegimeState
    from futures_fund.policy import _BASE_CAPS, caps_for

    dd, eq = 0.0704, 10_000.0
    h = PortfolioHealth(equity=eq, peak_equity=eq / (1 - dd), drawdown_from_peak=dd)

    per_quadrant = [effective_ptr(h, dd, q) for q in _BASE_CAPS]
    assert effective_ptr(h, dd, None) == pytest.approx(max(per_quadrant)), (
        "unknown quadrant must assume the WORST case for the budget")
    assert max(per_quadrant) > min(per_quadrant), (
        "fixture check: the quadrants must actually differ, or this test proves nothing")
    # and it is genuinely derived from policy, not a hardcoded constant
    assert max(per_quadrant) == pytest.approx(
        max(caps_for(RegimeState(quadrant=q, trend="up", vol="low"), h).per_trade_risk_pct
            for q in _BASE_CAPS) * 0.5)
