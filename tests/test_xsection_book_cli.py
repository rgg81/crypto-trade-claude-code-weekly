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
    # healthy low_vol_trend: heat 0.10 -> 100 legs -> 20/side requested is fine
    assert xb.safe_n_per_side(20, max_heat=0.10, dust_frac=0.001) == 20
    # caution low_vol_trend: heat 0.05 -> 50 legs -> 20/side (40) still fits
    assert xb.safe_n_per_side(20, max_heat=0.05, dust_frac=0.001) == 20
    # caution high_vol_range: heat 0.02 -> 20 legs total -> only 10/side
    assert xb.safe_n_per_side(20, max_heat=0.02, dust_frac=0.001) == 10
    # healthy high_vol_range: heat 0.04 -> 40 legs -> exactly 20/side
    assert xb.safe_n_per_side(20, max_heat=0.04, dust_frac=0.001) == 20


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
    # the heat budget still binds when it is the tighter of the two
    assert xb.fit_n_per_side(20, priced=100, max_heat=0.02) == 10


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
