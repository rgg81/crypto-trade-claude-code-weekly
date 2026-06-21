"""Blended cross-sectional score for the all-weather dollar-neutral desk.

A regime-weighted composite of three market-neutral edges per name:
  - momentum:    relative strength (z of momentum_20)        -> high = LONG
  - carry:       funding harvest (z of -annualized funding)  -> neg funding = LONG
  - mean_revert: range fade (z of (50-rsi))                  -> oversold = LONG
High score -> LONG candidate; low score -> SHORT candidate. Top-N long / bottom-N short = neutral,
ALWAYS deployed (never flat). Regime shifts the weights; a HYSTERESIS band keeps turnover low.
"""

from futures_fund import blended_score as bs


def _brief(sym, *, mom=0.0, funding=0.0, interval=8.0, rsi=50.0, adx=20.0,
           oi=500e6, last=100.0, atr=2.0):
    return {"symbol": sym, "momentum_20": mom, "funding_rate": funding,
            "funding_interval_hours": interval, "rsi": rsi, "adx": adx,
            "oi_value": oi, "last_close": last, "atr": atr,
            "swing_high": last * 1.1, "swing_low": last * 0.9}


# ---- annualized funding -------------------------------------------------------
def test_annualized_funding_scales_by_interval():
    # +0.00005 per 4h = 6 events/day * 365 ~ +10.95% annualized
    assert bs.annualized_funding(5e-5, 4.0) == \
        __import__("pytest").approx(5e-5 * 6 * 365, rel=1e-9)
    # an 8h major at the same rate annualizes to HALF (3 events/day)
    assert bs.annualized_funding(5e-5, 8.0) == \
        __import__("pytest").approx(5e-5 * 3 * 365, rel=1e-9)


def test_annualized_funding_sign_preserved():
    assert bs.annualized_funding(-1e-4, 8.0) < 0  # negative funding stays negative


# ---- z-score ------------------------------------------------------------------
def test_zscore_centers_and_scales():
    z = bs.zscore([1.0, 2.0, 3.0])
    assert z[1] == __import__("pytest").approx(0.0)        # the mean -> 0
    assert z[0] < 0 < z[2]
    assert abs(z[0]) == __import__("pytest").approx(abs(z[2]))


def test_zscore_zero_variance_is_all_zero():
    assert bs.zscore([5.0, 5.0, 5.0]) == [0.0, 0.0, 0.0]


# ---- tradeability filter (exclude pumps / illiquid / artifacts) ---------------
def test_is_tradeable_excludes_parabolic_pump():
    pump = _brief("BICOUSDT", mom=1.03, rsi=83, oi=8e6)   # +103%, RSI 83, $8M
    assert not bs.is_tradeable(pump)


def test_is_tradeable_excludes_parabolic_pump_even_when_rsi_cooled():
    # BTW-shape: +104% over 20 bars but RSI cooled to 62 and OI clears the floor. The soft rule
    # (mom>=0.30 AND rsi>=72) misses it; a hard momentum ceiling must still exclude the parabola
    # (it would also distort the whole cross-section's z-scores).
    assert not bs.is_tradeable(_brief("BTWUSDT", mom=1.047, rsi=62, oi=68e6))
    # symmetric: a -50%+ collapse is degenerate too
    assert not bs.is_tradeable(_brief("CRASHUSDT", mom=-0.6, rsi=40, oi=200e6))


def test_is_tradeable_excludes_illiquid_microcap():
    tiny = _brief("BSBUSDT", mom=-0.28, rsi=45, oi=20e6)  # below the OI floor
    assert not bs.is_tradeable(tiny)


def test_is_tradeable_excludes_nan_atr_artifact():
    art = _brief("REUSDT", atr=float("nan"), oi=7e6)
    assert not bs.is_tradeable(art)


def test_is_tradeable_keeps_normal_liquid_name():
    assert bs.is_tradeable(_brief("BTCUSDT", mom=-0.03, rsi=47, oi=6e9))


# ---- regime weights -----------------------------------------------------------
def test_regime_weights_trend_when_dispersion_high():
    # a wide spread of momentum -> trend -> momentum-led
    spread = [_brief(f"S{i}", mom=m) for i, m in enumerate([-0.15, -0.05, 0.05, 0.15, 0.25])]
    w = bs.regime_weights(spread)
    assert w["mom"] > w["carry"] and w["mom"] > w["mr"]


def test_regime_weights_range_when_dispersion_low():
    # everyone clustered -> flat/range -> carry-led
    flat = [_brief(f"S{i}", mom=m) for i, m in enumerate([-0.01, 0.0, 0.005, -0.005, 0.01])]
    w = bs.regime_weights(flat)
    assert w["carry"] >= w["mom"]
    assert abs(sum(w.values()) - 1.0) < 1e-9


# ---- composite scores ---------------------------------------------------------
def test_strong_momentum_neg_funding_scores_high_for_long():
    briefs = [
        _brief("AAA", mom=0.10, funding=-1e-4),   # strong + pays you to be long
        _brief("BBB", mom=0.0, funding=0.0),
        _brief("CCC", mom=-0.10, funding=1e-4),   # weak + pays you to be short
    ]
    scored = bs.composite_scores(briefs)
    order = [s["symbol"] for s in scored]
    assert order[0] == "AAA" and order[-1] == "CCC"


def test_carry_leads_in_flat_market():
    # momentum is ~flat across names; the richest negative-funding name must still rank top-long
    briefs = [
        _brief("RICH_NEG", mom=0.001, funding=-5e-4, interval=4.0),  # deep neg funding
        _brief("MID", mom=0.0, funding=0.0),
        _brief("RICH_POS", mom=-0.001, funding=5e-4, interval=4.0),  # rich pos funding
    ]
    scored = bs.composite_scores(briefs)
    assert scored[0]["symbol"] == "RICH_NEG"     # collect funding long
    assert scored[-1]["symbol"] == "RICH_POS"    # collect funding short


def test_carry_cannot_flip_a_falling_knife_to_long():
    # ZEC-shape: deeply negative funding (rich long-carry) but a clear downtrend -> NOT a long.
    briefs = [
        _brief("KNIFE", mom=-0.11, funding=-5e-4, interval=8.0, rsi=45),  # falling + neg funding
        _brief("MIDA", mom=0.0, funding=0.0),
        _brief("MIDB", mom=0.01, funding=0.0),
        _brief("LEADER", mom=0.06, funding=-1e-4),
    ]
    scored = bs.composite_scores(briefs)
    longs, shorts = bs.select_book(scored, n_per_side=1)
    assert "KNIFE" not in longs           # carry must not catch the knife
    assert scored[0]["symbol"] != "KNIFE"


def test_carry_cannot_flip_a_pump_to_short():
    # rich positive funding (long-pays-short) on a STRONG riser -> must NOT be shorted for carry.
    briefs = [
        _brief("RIPPER", mom=0.12, funding=5e-4, interval=8.0, rsi=60),  # rising + rich pos funding
        _brief("MIDA", mom=0.0, funding=0.0),
        _brief("MIDB", mom=-0.01, funding=0.0),
        _brief("LAG", mom=-0.06, funding=1e-4),
    ]
    scored = bs.composite_scores(briefs)
    longs, shorts = bs.select_book(scored, n_per_side=1)
    assert "RIPPER" not in shorts


def test_mean_reversion_only_fires_on_absolute_extremes():
    # flat momentum + flat funding; the only signal is RSI. Oversold ranks long, overbought short,
    # and a mid-band name (RSI ~50) carries no fade signal.
    briefs = [
        _brief("OVERSOLD", mom=0.0, funding=0.0, rsi=28),
        _brief("MIDC", mom=0.0, funding=0.0, rsi=50),
        _brief("OVERBOUGHT", mom=0.0, funding=0.0, rsi=74),
    ]
    scored = bs.composite_scores(briefs)
    assert scored[0]["symbol"] == "OVERSOLD"
    assert scored[-1]["symbol"] == "OVERBOUGHT"
    mid = next(s for s in scored if s["symbol"] == "MIDC")
    assert mid["raw"]["mr"] == 0.0             # raw fade signal is silent in the neutral band


def test_composite_excludes_pumps_from_ranking():
    briefs = [
        _brief("PUMP", mom=1.5, rsi=85, oi=5e6),   # excluded
        _brief("AAA", mom=0.05, funding=-1e-4),
        _brief("CCC", mom=-0.05, funding=1e-4),
    ]
    syms = [s["symbol"] for s in bs.composite_scores(briefs)]
    assert "PUMP" not in syms and "AAA" in syms and "CCC" in syms


# ---- selection: always deployed, never flat -----------------------------------
def test_select_book_top_long_bottom_short_balanced():
    briefs = [_brief(f"S{i}", mom=m, funding=-m * 1e-3)
              for i, m in enumerate([0.20, 0.10, 0.0, -0.10, -0.20])]
    scored = bs.composite_scores(briefs)
    longs, shorts = bs.select_book(scored, n_per_side=2)
    assert len(longs) == 2 and len(shorts) == 2
    assert set(longs).isdisjoint(shorts)
    # the strongest names are long, the weakest short
    assert "S0" in longs and "S4" in shorts


def test_select_book_never_returns_empty_when_universe_present():
    briefs = [_brief(f"S{i}", mom=m) for i, m in enumerate([0.03, 0.0, -0.03, -0.06])]
    longs, shorts = bs.select_book(bs.composite_scores(briefs), n_per_side=2)
    assert longs and shorts  # never flat


# ---- hysteresis: minimum rebalance --------------------------------------------
def _scored(order_scores):
    # build a fake scored list (symbol, score) high->low
    return [{"symbol": s, "score": sc, "components": {}} for s, sc in order_scores]


def test_hysteresis_keeps_held_leg_inside_buffer():
    # held long AAA slipped rank0 -> rank1 but is still inside the keep buffer -> KEEP, no churn
    scored = _scored([("XXX", 2.0), ("AAA", 1.8), ("BBB", 0.2),
                      ("YYY", -0.2), ("CCC", -1.8), ("ZZZ", -2.0)])
    holdings = {"AAA": "long", "CCC": "short"}
    plan = bs.apply_hysteresis(scored, holdings, n_per_side=2,
                               keep_buffer=1, swap_margin=0.5)
    assert "AAA" in plan["keep_long"] and "CCC" in plan["keep_short"]
    assert "AAA" not in plan["close"] and "CCC" not in plan["close"]


def test_hysteresis_rotates_when_rank_flips_through_the_book():
    # held long DDD has collapsed to the bottom (now a SHORT-side name) -> must CLOSE it
    scored = _scored([("AAA", 2.0), ("BBB", 1.5), ("CCC", 0.5),
                      ("EEE", -0.5), ("FFF", -1.5), ("DDD", -2.0)])
    holdings = {"DDD": "long"}
    plan = bs.apply_hysteresis(scored, holdings, n_per_side=2,
                               keep_buffer=1, swap_margin=0.5)
    assert "DDD" in plan["close"]
    assert len(plan["open_long"]) >= 1  # refill the long slot from the top


def test_hysteresis_does_not_churn_on_tiny_margin():
    # a fresh name barely edges a held one -> below swap_margin -> do NOT rotate (min rebalance)
    scored = _scored([("NEW", 1.21), ("AAA", 1.20), ("BBB", 1.0),
                      ("CCC", -1.0), ("DDD", -1.2)])
    holdings = {"AAA": "long", "BBB": "long", "CCC": "short", "DDD": "short"}
    plan = bs.apply_hysteresis(scored, holdings, n_per_side=2,
                               keep_buffer=1, swap_margin=0.5)
    # AAA still held (NEW only beats it by 0.01 << 0.5 margin)
    assert "AAA" in plan["keep_long"]
    assert "NEW" not in plan["open_long"]
