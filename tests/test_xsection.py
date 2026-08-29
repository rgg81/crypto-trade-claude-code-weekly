"""Cross-sectional factor book — the maths the desk sizes from.

Measured on 230 perps x 2190 4h bars (research/README.md): the desk's losses were caused by
CONCENTRATION, not by absence of signal. Same momentum signal at 3 legs/side gives Sharpe 1.19 and a
35% drawdown; at 20/side it gives 1.95 with 13.6%. These tests pin the construction that produces
that: z-scored cross-sectional momentum, inverse-vol legs, iterated beta-neutralisation, per-name
cap, and an exactly dollar-neutral result.
"""
from __future__ import annotations

import math

import pytest

from futures_fund.xsection import (
    beta_to_market,
    cross_sectional_weights,
    market_returns,
    momentum,
    realized_vol,
    returns,
    size_sleeves,
    zscore_map,
)


def _ramp(n, start=100.0, step=1.0):
    return [start + i * step for i in range(n)]


def test_returns_and_momentum_are_simple_and_correct():
    assert returns([100.0, 110.0, 121.0]) == pytest.approx([0.10, 0.10])
    assert momentum([100.0] * 10 + [120.0], 10) == pytest.approx(0.20)
    assert momentum([1.0, 2.0], 10) is None, "not enough history must be None, never 0.0"


def test_momentum_uses_the_bar_exactly_lookback_back():
    closes = _ramp(200)
    assert momentum(closes, 150) == pytest.approx(closes[-1] / closes[-151] - 1.0)


def test_realized_vol_is_zero_free_and_none_on_short_history():
    assert realized_vol([100.0] * 40, 30) is None, "a flat series has no usable vol"
    v = realized_vol([100.0 * (1.02 ** i) if i % 2 else 100.0 * (0.98 ** i) for i in range(40)], 30)
    assert v and v > 0


def test_market_returns_is_the_equal_weight_cross_section():
    s = {"A": [100.0, 110.0], "B": [100.0, 90.0]}
    assert market_returns(s) == pytest.approx([0.0])


def test_beta_of_a_name_that_is_the_market_is_one():
    closes = [100.0, 101.0, 99.0, 103.0, 102.0, 105.0, 104.0, 108.0, 107.0, 111.0] * 4
    mkt = returns(closes)
    assert beta_to_market(closes, mkt, 30) == pytest.approx(1.0, abs=1e-6)


def test_beta_of_an_inverted_name_is_negative_one():
    closes = [100.0, 101.0, 99.0, 103.0, 102.0, 105.0, 104.0, 108.0, 107.0, 111.0] * 4
    inv = [100.0]
    for r in returns(closes):
        inv.append(inv[-1] * (1 - r))
    assert beta_to_market(inv, returns(closes), 30) == pytest.approx(-1.0, abs=0.05)


def test_zscore_map_clips_and_ignores_missing():
    z = zscore_map({"A": 0.0, "B": 100.0, "C": None, "D": 50.0}, clip=1.0)
    assert "C" not in z
    assert all(abs(v) <= 1.0 for v in z.values())


def _synthetic(n_names=40, bars=260):
    """Deterministic panel that behaves like a MARKET: a common factor every name loads on (betas
    spread around 1, as real assets do), plus an idiosyncratic wobble and a persistent per-name
    drift that creates the cross-sectional momentum spread. A panel without a common factor gives
    nonsense betas (spread -11..+11) and tests nothing real."""
    market = [math.sin(b / 11.0) * 0.010 + math.cos(b / 29.0) * 0.006 for b in range(bars)]
    out = {}
    for i in range(n_names):
        beta = 0.6 + 1.2 * (i % 7) / 6.0                     # betas in [0.6, 1.8]
        drift = (i - n_names / 2) / (n_names * 400.0)
        px = [100.0]
        for b in range(bars):
            idio = math.sin((b + i * 13) / 6.0) * 0.003
            px.append(px[-1] * (1.0 + drift + beta * market[b] + idio))
        out[f"S{i:02d}"] = px
    return out


def test_weights_are_dollar_neutral_and_normalised():
    w = cross_sectional_weights(_synthetic(), n_per_side=8)
    assert w, "expected a book"
    assert sum(w.values()) == pytest.approx(0.0, abs=1e-9), "book must be dollar-neutral"
    assert sum(abs(v) for v in w.values()) == pytest.approx(1.0, abs=1e-9)


def test_book_holds_exactly_n_per_side():
    w = cross_sectional_weights(_synthetic(), n_per_side=8)
    assert sum(1 for v in w.values() if v > 0) == 8
    assert sum(1 for v in w.values() if v < 0) == 8


def test_longs_are_the_strong_momentum_names_and_shorts_the_weak():
    panel = _synthetic()
    w = cross_sectional_weights(panel, n_per_side=5)
    moms = {s: momentum(px, 150) for s, px in panel.items()}
    longs = [s for s, v in w.items() if v > 0]
    shorts = [s for s, v in w.items() if v < 0]
    assert min(moms[s] for s in longs) > max(moms[s] for s in shorts)


def test_per_name_cap_is_respected():
    w = cross_sectional_weights(_synthetic(), n_per_side=8, max_name_frac=0.10)
    assert max(abs(v) for v in w.values()) <= 0.10 + 1e-9


def test_beta_neutralisation_reduces_net_book_beta():
    panel = _synthetic()
    mkt = market_returns(panel)
    betas = {s: beta_to_market(px, mkt, 180) for s, px in panel.items()}
    raw = cross_sectional_weights(panel, n_per_side=8, beta_iters=0)
    neu = cross_sectional_weights(panel, n_per_side=8, beta_iters=5)
    def net(w):
        return abs(sum(v * betas[s] for s, v in w.items()))

    assert net(neu) <= net(raw) + 1e-12


def test_too_few_names_yields_no_book_rather_than_a_lopsided_one():
    panel = {k: v for k, v in list(_synthetic().items())[:6]}
    assert cross_sectional_weights(panel, n_per_side=8) == {}


def test_names_without_enough_history_are_excluded_not_defaulted():
    panel = _synthetic(n_names=30)
    panel["SHORT_HIST"] = [100.0, 101.0, 102.0]
    w = cross_sectional_weights(panel, n_per_side=5)
    assert "SHORT_HIST" not in w


def test_narrow_cross_section_still_builds_a_book():
    """REGRESSION. Requiring n strictly-positive and n strictly-negative weights returned an EMPTY
    book on a narrow universe (12 names at 6/side), so the desk held forever. Select by rank."""
    panel = _synthetic(n_names=12, bars=260)
    w = cross_sectional_weights(panel, n_per_side=6)
    assert w, "a 12-name universe at 6/side must still produce a book"
    assert sum(1 for v in w.values() if v > 0) == 6
    assert sum(1 for v in w.values() if v < 0) == 6
    assert sum(w.values()) == pytest.approx(0.0, abs=1e-9)


def test_longs_still_outrank_shorts_on_RISK_ADJUSTED_momentum():
    """On a NARROW cross-section the sleeves can swap a name at the boundary versus raw momentum,
    and that is correct: the weight is momentum divided by volatility and then beta-neutralised, so
    a low-vol low-beta name legitimately outranks a slightly-higher-momentum volatile one. What must
    hold is that the long sleeve is stronger ON AVERAGE, not a perfect raw-momentum split."""
    panel = _synthetic(n_names=12, bars=260)
    w = cross_sectional_weights(panel, n_per_side=6)
    moms = {s: momentum(px, 150) for s, px in panel.items()}
    longs = [s for s, v in w.items() if v > 0]
    shorts = [s for s, v in w.items() if v < 0]
    def mean(xs):
        return sum(xs) / len(xs)

    assert mean([moms[s] for s in longs]) > mean([moms[s] for s in shorts])


def test_near_zero_vol_names_are_excluded():
    """Gold-backed tokens (PAXG/XAUT, ~1.3% ATR) are 'COIN' by Binance's taxonomy so is_crypto_perp
    passes them, but they are commodities. Inverse-vol weighting hands a near-zero-vol name one of
    the LARGEST notionals in the book, so the live book came out heavily short gold. A volatility
    FLOOR removes them (and any stablecoin-like perp) without naming instruments."""
    panel = _synthetic(n_names=30, bars=260)
    flat = [100.0 * (1 + 0.00002 * i) for i in range(261)]     # ~zero vol, like a gold token
    panel["GOLDLIKE"] = flat
    w = cross_sectional_weights(panel, n_per_side=8, vol_floor=0.001)
    assert "GOLDLIKE" not in w


def test_parabolic_pumps_are_excluded():
    """A name up >50% over 20 bars is a blow-off, not momentum the desk can size into — the old
    book refused these via PUMP_MOM_HARD and the factor book must too."""
    panel = _synthetic(n_names=30, bars=260)
    pump = list(panel["S00"])
    pump = pump[:-20] + [pump[-21] * (1 + 0.9 * i / 20) for i in range(1, 21)]
    panel["PUMPED"] = pump
    w = cross_sectional_weights(panel, n_per_side=8, pump_cap=0.50)
    assert "PUMPED" not in w


def test_filters_are_off_by_default_so_callers_opt_in():
    panel = _synthetic(n_names=30, bars=260)
    panel["GOLDLIKE"] = [100.0 * (1 + 0.00002 * i) for i in range(261)]
    w = cross_sectional_weights(panel, n_per_side=8)
    assert isinstance(w, dict)


def test_sleeve_sizing_is_never_inverted_when_a_sleeve_does_not_straddle_zero():
    """ADVERSARIAL, and my first attempt at this test was VACUOUS — it passed with the bug
    reintroduced because the synthetic panel never reached the skewed regime. This exercises it
    directly.

    Sizing a sleeve by |weight| assumes the sleeve's sign is consistent. Momentum is RIGHT-SKEWED
    (a few sharp losers, many mild winners), so after de-meaning the bottom-n can all be POSITIVE.
    |w| then makes the WEAKEST short the LARGEST short — exactly backwards. Here every weight is
    positive: A(0.10) is the most short-favourable and MUST get the biggest short."""
    book = {"A": 0.10, "B": 0.20, "C": 0.30, "D": 0.40}
    # max_name_frac must not BIND here, or the water-fill equalises the sleeve and the
    # test cannot observe the ordering at all (my first version failed for exactly that).
    w = size_sleeves(book, long_syms={"C", "D"}, max_name_frac=0.5)
    assert abs(w["A"]) > abs(w["B"]), (
        f"short sleeve inverted: A={w['A']:.4f} B={w['B']:.4f} (A has the lower score)")
    assert abs(w["D"]) > abs(w["C"]), (
        f"long sleeve inverted: D={w['D']:.4f} C={w['C']:.4f} (D has the higher score)")
    assert w["A"] < 0 and w["B"] < 0 and w["C"] > 0 and w["D"] > 0
    assert sum(w.values()) == pytest.approx(0.0, abs=1e-9)


def test_sleeve_sizing_reduces_to_absolute_weight_when_the_book_straddles_zero():
    """The normal case must be unchanged by the boundary-relative fix."""
    book = {"S1": -0.30, "S2": -0.10, "L1": 0.10, "L2": 0.30}
    w = size_sleeves(book, long_syms={"L1", "L2"}, max_name_frac=0.5)
    assert abs(w["L2"]) / abs(w["L1"]) == pytest.approx(3.0, rel=1e-6)
    assert abs(w["S1"]) / abs(w["S2"]) == pytest.approx(3.0, rel=1e-6)


def test_size_sleeves_refuses_a_one_sided_book():
    assert size_sleeves({"A": 0.1, "B": 0.2}, long_syms={"A", "B"}) == {}
    assert size_sleeves({"A": 0.1, "B": 0.2}, long_syms=set()) == {}
