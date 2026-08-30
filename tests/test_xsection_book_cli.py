"""The weight -> gate translation. This is where a factor book most easily goes wrong silently."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "xsection_book_cli", Path(__file__).resolve().parents[1] / "scripts" / "xsection_book_cli.py")
xb = importlib.util.module_from_spec(_spec)
sys.modules["xsection_book_cli"] = xb
_spec.loader.exec_module(xb)


def _rows(n=60, base=100.0):
    """Klines: [openTime, open, high, low, close, ...]."""
    out = []
    for i in range(n):
        c = base + i
        out.append([i * 1000, c - 0.5, c + 1.0, c - 1.0, c, 0, 0, 0])
    return out


def test_atr_is_positive_and_none_on_short_history():
    assert xb.atr(_rows(60)) > 0
    assert xb.atr(_rows(3)) is None


def test_long_structure_places_stop_below_and_tps_above():
    st = xb.structure(100.0, 2.0, "long")
    assert st["stop"] < 100.0 < st["take_profits"][0] < st["take_profits"][1]


def test_short_structure_places_stop_above_and_tps_below():
    st = xb.structure(100.0, 2.0, "short")
    assert st["stop"] > 100.0 > st["take_profits"][0] > st["take_profits"][1]


@pytest.mark.parametrize("direction", ["long", "short"])
def test_take_profits_clear_the_gate_minimum_rr(direction):
    """RR >= 2 is a hard gate veto. A factor leg's TP is deliberately FAR (6R) so it rarely fires,
    but it must still exist and must still clear the minimum."""
    from futures_fund.risk_gate import MIN_RR
    st = xb.structure(100.0, 2.0, direction)
    risk = abs(st["entry"] - st["stop"])
    nearest = min(st["take_profits"], key=lambda tp: abs(tp - st["entry"]))
    assert abs(nearest - st["entry"]) / risk >= MIN_RR


def test_risk_mult_is_proportional_to_weight_times_stop_frac():
    """The gate sizes by RISK: notional = equity*ptr*rm/stop_frac. For notional to track the target
    WEIGHT, rm must scale with weight*stop_frac. Getting this backwards silently inverts the book's
    risk profile — big weights on tight stops would dominate."""
    w = {"A": 0.10, "B": 0.05}
    sf = {"A": 0.05, "B": 0.05}
    rm = xb.risk_mults(w, sf)
    assert rm["A"] == pytest.approx(1.0)
    assert rm["B"] == pytest.approx(0.5)


def test_risk_mult_accounts_for_differing_stop_widths():
    """Equal weights but one leg has a 2x wider stop -> it needs 2x the risk to reach equal $."""
    rm = xb.risk_mults({"WIDE": 0.10, "TIGHT": 0.10}, {"WIDE": 0.10, "TIGHT": 0.05})
    assert rm["WIDE"] == pytest.approx(1.0)
    assert rm["TIGHT"] == pytest.approx(0.5)


def test_risk_mult_never_exceeds_one():
    """The gate clamps rm to (0,1]; emitting >1 would be silently truncated and skew the book."""
    rm = xb.risk_mults({"A": 5.0, "B": 1.0}, {"A": 0.5, "B": 0.1})
    assert all(0 < v <= 1.0 for v in rm.values())


def test_risk_mult_is_scale_free_in_weights():
    a = xb.risk_mults({"A": 0.10, "B": 0.05}, {"A": 0.05, "B": 0.05})
    b = xb.risk_mults({"A": 0.20, "B": 0.10}, {"A": 0.05, "B": 0.05})
    assert a == pytest.approx(b)


def test_empty_or_degenerate_inputs_yield_no_book():
    assert xb.risk_mults({}, {}) == {}
    assert xb.risk_mults({"A": 0.0}, {"A": 0.0}) == {}


def test_leg_count_adapts_to_the_gates_heat_budget():
    """A factor book must never propose more legs than the gate's heat budget can carry ABOVE the
    dust floor. consolidate() drops any leg whose risk < 0.001 of equity SILENTLY, so proposing 40
    legs into a 0.02 heat cap (high_vol_range at the caution tier) would quietly delete half the
    book and leave it lopsided — the dust-drop failure family. Cap legs at heat/dust/2 per side."""
    # Budgets carry DUST_HEADROOM (see test_leg_budget_leaves_headroom_above_the_dust_floor):
    # affordable legs = max_heat / (dust * headroom), halved per side.
    # healthy low_vol_trend: heat 0.10 -> 50 legs -> 25/side, so 20 asked is fine
    assert xb.safe_n_per_side(20, max_heat=0.10, dust_frac=0.001) == 20
    # caution low_vol_trend: heat 0.05 -> 25 legs -> 12/side
    assert xb.safe_n_per_side(20, max_heat=0.05, dust_frac=0.001) == 12
    # caution high_vol_range: heat 0.02 -> 10 legs -> 5/side
    assert xb.safe_n_per_side(20, max_heat=0.02, dust_frac=0.001) == 5
    # healthy high_vol_range: heat 0.04 -> 20 legs -> 10/side
    assert xb.safe_n_per_side(20, max_heat=0.04, dust_frac=0.001) == 10


def test_leg_count_never_exceeds_the_request():
    assert xb.safe_n_per_side(20, max_heat=1.0, dust_frac=0.001) == 20


def test_leg_count_has_a_floor_so_the_desk_is_never_flat():
    """Even a crushed heat budget must leave a real book — the desk is NEVER flat by mandate."""
    assert xb.safe_n_per_side(20, max_heat=0.0, dust_frac=0.001) >= 3
    assert xb.safe_n_per_side(20, max_heat=0.001, dust_frac=0.001) >= 3


def test_leg_count_also_adapts_to_a_THIN_universe():
    """cross_sectional_weights refuses to build a lopsided book, so asking for 20/side out of a
    12-name universe returns NOTHING and the desk holds forever. The book must instead shrink to
    the widest one the universe can actually fill."""
    assert xb.fit_n_per_side(20, priced=100, max_heat=0.10) == 20
    assert xb.fit_n_per_side(20, priced=30, max_heat=0.10) == 15
    assert xb.fit_n_per_side(20, priced=12, max_heat=0.10) == 6
    # the heat budget still binds when it is the tighter of the two (0.02 -> 5/side with headroom)
    assert xb.fit_n_per_side(20, priced=100, max_heat=0.02) == 5


def test_thin_universe_still_yields_a_book_not_silence():
    import math

    from futures_fund.xsection import cross_sectional_weights
    panel = {}
    for i in range(14):
        beta = 0.7 + 0.1 * (i % 5)
        px = [100.0]
        for b in range(260):
            px.append(px[-1] * (1 + (i - 7) / 6000.0 + beta * math.sin(b / 11.0) * 0.008))
        panel[f"S{i}"] = px
    n = xb.fit_n_per_side(20, priced=len(panel), max_heat=0.10)
    w = cross_sectional_weights(panel, n_per_side=n)
    assert w, "a thin universe must still produce a (smaller) book"
    assert sum(1 for v in w.values() if v > 0) == n


def test_names_with_an_inadmissible_stop_are_dropped_before_the_book_is_built():
    """REGRESSION. A leg whose ATR stop exceeds the gate's liq-distance ceiling (40% long /
    36.19% short) is VETOED every time, so the sleeve executes a leg short and the book comes out
    lopsided — the same defect the blended CLI's _too_wide filter existed to prevent. The live book
    proposed ONG with a 54% stop. Filter it out at selection, not after the veto."""
    from futures_fund.blended_score import admissible_stop_frac
    ceiling = admissible_stop_frac(None)
    assert xb.stop_ok(0.10, ceiling)
    assert not xb.stop_ok(0.54, ceiling)
    assert not xb.stop_ok(ceiling + 1e-6, ceiling)
    assert xb.stop_ok(ceiling - 1e-6, ceiling)


def test_leg_budget_leaves_headroom_above_the_dust_floor():
    """cy360 REGRESSION. Sizing to exactly max_heat/dust legs leaves ZERO margin: consolidate()
    scales the batch to the heat cap first, so any scaling pushes marginal legs under the floor and
    they are deleted silently. cy360 asked for 40 legs into a 40-leg budget and got 31. Require
    each leg to clear the floor with a safety multiple."""
    # max_heat 0.04 / dust 0.001 = 40 legs raw, but with 2x headroom only 20 total = 10/side
    assert xb.safe_n_per_side(20, max_heat=0.04, dust_frac=0.001) == 10
    assert xb.safe_n_per_side(20, max_heat=0.10, dust_frac=0.001) == 20   # healthy: 50 -> 20 asked
    assert xb.safe_n_per_side(20, max_heat=0.02, dust_frac=0.001) == 5


def test_risk_mult_is_normalised_PER_SLEEVE():
    """cy360 REGRESSION. Normalising rm across the WHOLE book couples the two sleeves: momentum is
    right-skewed, so the long sleeve carries larger |z| than the short, every short leg gets a
    smaller rm, and the shorts are the ones dust-dropped (7 of 9 lost legs at cy360). Each sleeve
    must get its own budget so neither side is starved by the other's signal strength."""
    w = {"L1": 0.20, "L2": 0.05, "S1": -0.02, "S2": -0.01}
    sf = dict.fromkeys(w, 0.05)
    rm = xb.risk_mults(w, sf)
    assert max(rm["L1"], rm["L2"]) == pytest.approx(1.0), "long sleeve must reach 1.0"
    assert max(rm["S1"], rm["S2"]) == pytest.approx(1.0), "short sleeve must reach 1.0 too"
    # and within a sleeve the ordering is preserved
    assert rm["L1"] > rm["L2"] and rm["S1"] > rm["S2"]


def test_per_sleeve_normalisation_keeps_relative_weights_inside_a_sleeve():
    w = {"L1": 0.10, "L2": 0.05, "S1": -0.10, "S2": -0.05}
    sf = dict.fromkeys(w, 0.05)
    rm = xb.risk_mults(w, sf)
    assert rm["L2"] / rm["L1"] == pytest.approx(0.5)
    assert rm["S2"] / rm["S1"] == pytest.approx(0.5)


def test_stops_are_wide_enough_that_noise_does_not_close_a_factor_leg():
    """cy360-364 REGRESSION, and the most damaging bug so far. The validated backtest held every leg
    until the SIGNAL changed; the shipped desk stopped legs out at 2xATR. Replayed over 100 names x
    2190 bars that single difference INVERTS the strategy:

        no stops  sharpe +2.33 (+6.29%/mo)   0 stop-outs
        8xATR     sharpe +1.42 (+3.62%/mo)  51 stop-outs
        4xATR     sharpe +0.49 (+0.91%/mo) 205 stop-outs
        2xATR     sharpe -1.63 (-4.83%/mo) 734 stop-outs   <-- what shipped

    Monotone in stop-outs: momentum legs get stopped on ordinary noise, then sit out until the
    next rebalance, so the desk sells the dip and misses the recovery. Refilling faster does not
    rescue it (2xATR at every=1 is still only +0.15). The gate REQUIRES a stop, so make it wide
    enough that the signal, not noise, closes the leg."""
    assert xb.ATR_MULT >= 8.0, "a 2xATR stop inverts this strategy"


def test_stop_is_capped_below_the_gate_ceiling_instead_of_excluding_the_name():
    """A pure multiple over-filters: 8xATR on a 5%-ATR name is 40%, past the gate's admissible stop,
    so stop_ok would reject it and bias the book toward low-vol names. Cap the stop instead."""
    from futures_fund.blended_score import admissible_stop_frac
    ceiling = admissible_stop_frac(None)
    assert xb.STOP_CAP < ceiling, "the cap must sit inside the gate's liq-distance ceiling"
    # a very high-ATR name is capped, not dropped
    st = xb.structure(100.0, 20.0, "long")          # 20% ATR -> 8x would be 160%
    assert abs(st["entry"] - st["stop"]) / st["entry"] == pytest.approx(xb.STOP_CAP, abs=1e-9)


def test_widening_the_stop_narrows_the_long_short_stop_gap():
    """The asymmetric attrition (3 shorts lost vs 1 long over cy360-364) came from the factor
    longing high-ATR microcaps and shorting low-ATR majors: at 2xATR the short stops were ~45%
    tighter, so shorts were picked off first and the guard had to shrink the long sleeve to restore
    neutrality. Capping the wide side compresses that gap."""
    lo_vol, hi_vol = 3.0, 12.0                       # ATR as % of a 100 price
    s_short = abs(100.0 - xb.structure(100.0, lo_vol, "short")["stop"]) / 100.0
    s_long = abs(100.0 - xb.structure(100.0, hi_vol, "long")["stop"]) / 100.0
    assert s_long / s_short < 2.0, "stop widths must not differ by more than 2x across sleeves"


def test_a_leg_with_no_computed_risk_mult_is_not_given_the_LARGEST_size():
    """ADVERSARIAL. The CLI did `rm.get(sym, 1.0)`. risk_mults() only emits symbols with a non-zero
    weight on a side, so any leg it skips would silently be handed rm=1.0 — the MAXIMUM position —
    rather than a small one. A missing weight must never become the biggest bet on the book."""
    rm = xb.risk_mults({"A": 0.10, "B": 0.00, "S": -0.10}, {"A": 0.05, "B": 0.05, "S": 0.05})
    assert "B" not in rm, "a zero-weight leg has no sleeve and must not be sized"
    assert xb.rm_for("B", rm) < 1.0, "a leg with no computed weight must not get the max size"
    assert xb.rm_for("A", rm) == pytest.approx(1.0)


def test_held_legs_with_a_STALE_tight_stop_are_re_struck():
    """cy369 REGRESSION. Widening the stop only affects NEWLY OPENED legs — a held leg keeps
    whatever stop it was booked with. After the 2xATR -> 8xATR fix the live book was bimodal: 11 new
    legs at a 30.0% median stop and 8 legacy legs at 8.7%, and it was a LEGACY leg (LAB) that was
    stopped out first, costing a leg and forcing a neutrality trim that shrank the book.

    A leg whose stop is materially tighter than current policy carries the exact exposure the policy
    change removed, so it must be re-struck rather than left to bleed out. The whole migration is 8
    legs x 2 fills x ~$110 x 0.07% ~ $1.23 — far cheaper than one avoidable stop-out."""
    live = {"OLD1": 0.054, "OLD2": 0.087, "NEW1": 0.300, "NEW2": 0.138}
    want = {"OLD1": 0.30, "OLD2": 0.30, "NEW1": 0.30, "NEW2": 0.15}
    out = xb.stale_stop_restrikes(live, want, tolerance=0.5)
    assert "OLD1" in out and "OLD2" in out, "legacy tight stops must be re-struck"
    assert "NEW1" not in out, "a leg already at policy must not churn"
    assert "NEW2" not in out, "a leg inside tolerance must not churn"


def test_restrike_never_fires_on_a_leg_whose_stop_is_WIDER_than_policy():
    """Only a TIGHTER-than-policy stop carries the harmful exposure. A wider one is harmless and
    re-striking it would be pure fee."""
    out = xb.stale_stop_restrikes({"WIDE": 0.30}, {"WIDE": 0.10}, tolerance=0.5)
    assert out == set()


def test_restrike_is_silent_without_the_data_it_needs():
    assert xb.stale_stop_restrikes({}, {}, tolerance=0.5) == set()
    assert xb.stale_stop_restrikes({"A": 0.05}, {}, tolerance=0.5) == set()


def test_risk_mult_uses_the_stop_ACTUALLY_placed_not_the_uncapped_one():
    """The cap introduced a mismatch: rm was computed from the raw ATR_MULT*atr/price while
    structure() places min(that, STOP_CAP). Since notional = equity*ptr*rm/stop_frac, sizing a leg
    off a 40% stop while placing a 30% one over-sizes it by 33%. Both must use the placed stop."""
    entry, atr_v = 100.0, 10.0                    # 8x ATR = 80% -> capped to STOP_CAP
    st = xb.structure(entry, atr_v, "long")
    placed = abs(st["entry"] - st["stop"]) / st["entry"]
    assert placed == pytest.approx(xb.STOP_CAP)
    assert xb.placed_stop_frac(entry, atr_v) == pytest.approx(placed), (
        "the stop_frac fed to risk_mults must equal the stop structure() actually places")
    # and an uncapped leg is unchanged
    assert xb.placed_stop_frac(100.0, 1.0) == pytest.approx(0.08)
