"""The book guessed the WORST-CASE heat budget while the gate was enforcing twice that.

`fallback_max_heat` takes the minimum max_heat across every regime quadrant because the desk's
context carries `regime_state.regime` ("risk_off"), not a quadrant. That was the right call while
the quadrant was genuinely unknown — cy384 proved guessing HIGH gets legs vetoed.

But the quadrant is not unknown. The gate derives it itself, from the bellwether's own frame:

    futures_fund/cycle.py:150
        caps = caps_for(simple_regime(ctx.frames[ctx.settings.symbols[0]]), health)

`symbols[0]` is the scout universe's first entry (BTC by convention); `working_universe` only ever
APPENDS held symbols, so the first element is stable. The book CLI reads that same ordered
universe.json and already pulls that symbol's candles. Classifying the same way makes the book's
assumption EQUAL the gate's enforcement instead of a worst case:

    live cy424: quadrant=low_vol_range, tier=healthy
        gate enforces  max_heat 0.080  ->  gross 0.267x,  20 legs/side
        book assumed   max_heat 0.040  ->  gross 0.133x,  10 legs/side

Half the budget was going unused — not because a limit forbade it, but because the book could not
see which limit applied. This weakens nothing: the gate's table is untouched and still has the last
word; the book simply stops sandbagging.

FAIL-SAFE. If the bellwether cannot be identified or classified, it MUST fall back to the
worst-case budget. Over-asking is the failure that costs legs (cy384, cy360), so an unknown
quadrant must stay pessimistic.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from xsection_book_cli import bellwether_quadrant, gate_max_heat  # noqa: E402


class _Health:
    tier = "healthy"


def _trending_closes(n=300, start=100.0, step=0.5):
    return [start + i * step for i in range(n)]


def _flat_closes(n=300, start=100.0):
    # tiny alternating wobble: no trend, low volatility -> low_vol_range
    return [start + (0.01 if i % 2 else -0.01) for i in range(n)]


def test_it_classifies_the_same_way_the_GATE_does():
    """Tie this to `simple_regime` itself, not to a re-implementation of it — a copy would drift
    away from the gate silently, which is the whole failure being fixed."""
    import pandas as pd

    from futures_fund.baseline import simple_regime

    closes = _trending_closes()
    expected = simple_regime(pd.DataFrame({"close": closes})).quadrant

    assert bellwether_quadrant({"BTCUSDT": closes}, ["BTCUSDT", "ETHUSDT"]) == expected


def test_the_bellwether_is_the_FIRST_universe_symbol_not_a_hardcoded_BTC():
    """The gate uses settings.symbols[0]; if the scout ever reorders, this must follow it."""
    trend, flat = _trending_closes(), _flat_closes()

    as_first = bellwether_quadrant({"AAA": trend, "BBB": flat}, ["AAA", "BBB"])
    reordered = bellwether_quadrant({"AAA": trend, "BBB": flat}, ["BBB", "AAA"])

    assert as_first != reordered, "the classification must follow symbols[0], not a fixed symbol"


def test_a_real_quadrant_unlocks_the_budget_the_gate_actually_enforces():
    """THE regression: low_vol_range at healthy is 0.080, not the 0.040 worst case."""
    flat = _flat_closes()
    q = bellwether_quadrant({"BTCUSDT": flat}, ["BTCUSDT"])

    heat, src = gate_max_heat(_Health(), {"BTCUSDT": flat}, ["BTCUSDT"])

    from futures_fund.models import RegimeState
    from futures_fund.policy import caps_for
    expected = caps_for(RegimeState(quadrant=q, trend_direction="neutral"), _Health()).max_heat

    assert heat == pytest.approx(expected)
    assert q in src, "the note must name the quadrant it used, for the cycle log"


def test_an_UNIDENTIFIABLE_bellwether_falls_back_to_the_WORST_case():
    """Over-asking is what costs legs (cy384 vetoes, cy360 dust). Unknown stays pessimistic."""
    from xsection_book_cli import fallback_max_heat

    worst = fallback_max_heat(_Health())

    for series, syms in (({}, []),                        # nothing priced
                         ({"BTCUSDT": _flat_closes()}, []),   # no universe order
                         ({"ETHUSDT": _flat_closes()}, ["BTCUSDT"])):  # bellwether not priced
        heat, src = gate_max_heat(_Health(), series, syms)
        assert heat == pytest.approx(worst), f"must fall back for {syms}"
        assert "unknown" in src.lower() or "strict" in src.lower()


def test_a_classification_failure_falls_back_rather_than_raising():
    """A book that cannot classify must still be built — degraded, never absent."""
    heat, src = gate_max_heat(_Health(), {"BTCUSDT": [1.0, 2.0]}, ["BTCUSDT"])  # too short
    assert heat > 0
    assert isinstance(src, str) and src


def test_the_fallback_is_never_MORE_permissive_than_a_real_classification():
    """Sanity: the worst case must bound every quadrant's budget from below."""
    from xsection_book_cli import fallback_max_heat

    from futures_fund.models import RegimeState
    from futures_fund.policy import _BASE_CAPS, caps_for

    worst = fallback_max_heat(_Health())
    for q in _BASE_CAPS:
        assert caps_for(RegimeState(quadrant=q, trend_direction="up"),
                        _Health()).max_heat >= worst


def test_the_SHIPPED_path_uses_the_derived_quadrant():
    """A helper nothing calls is worse than none — it reads as fixed. Assert main() wires it."""
    src = (Path(__file__).resolve().parents[1] / "scripts" / "xsection_book_cli.py").read_text()
    assert "_heat, _src = gate_max_heat(health, series, symbols)" in src, \
        "the book must derive the gate's real budget, not fall straight to the worst case"
    # and the ptr must follow the SAME quadrant, or heat and per-leg risk disagree
    assert "quad = bellwether_quadrant(series, symbols)" in src
    assert "_ptr = effective_ptr(health, dd, quad)" in src
