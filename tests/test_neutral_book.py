"""Dollar-neutral pre-size + balance pass (NON-protected, runs PRE-gate on TradeProposal objects).

One merged computation: bucket props long/short, assign each leg a target notional so gross_long$ ==
gross_short$ (~equity/2 per side, netting held notional), and stamp risk_mult so the gate sizes each
leg to its target. Trim-not-veto: when one side is scarce, balance to the gross BOTH sides can
muster; veto (drop everything) only when a side is truly empty AND unheld. The gate's (0,1] clamp
means a wide-stop leg deploys at the per-trade cap (best-effort, never over-deploys).
"""
import pytest

from futures_fund.models import TradeProposal
from futures_fund.neutral_book import presize_and_balance
from futures_fund.sizing import qty_from_risk

_PTR = 0.015   # per_trade_risk_pct (low_vol_trend healthy)
_EQ = 10_000.0


def _tp(symbol, direction, entry, stop):
    return TradeProposal(symbol=symbol, direction=direction, entry=entry, stop=stop,
                         take_profits=[entry * (1.1 if direction == "long" else 0.9)],
                         atr=entry * 0.02, confidence=0.6, horizon_hours=16.0, funding_rate=0.0)


def _notional(tp):
    """The notional the gate will realize from the stamped risk_mult (breaker mult assumed 1)."""
    return qty_from_risk(_EQ, _PTR * tp.risk_mult, tp.entry, tp.stop) * tp.entry


def test_balanced_book_equal_gross_each_side():
    props = [_tp("AUSDT", "long", 100.0, 95.0), _tp("BUSDT", "long", 50.0, 47.5),
             _tp("CUSDT", "short", 200.0, 210.0), _tp("DUSDT", "short", 20.0, 21.0)]
    kept, summary = presize_and_balance(props, equity=_EQ, per_trade_risk_pct=_PTR)
    long_gross = sum(_notional(t) for t in kept if t.direction == "long")
    short_gross = sum(_notional(t) for t in kept if t.direction == "short")
    # each side ~= equity/2 = 5000, and they MATCH (dollar-neutral)
    assert long_gross == pytest.approx(5_000.0, rel=1e-6)
    assert short_gross == pytest.approx(5_000.0, rel=1e-6)


def test_unequal_counts_still_dollar_balanced():
    # 3 longs (~$1667 each, ~5% stops) + 2 shorts (~$2500 each at the per-name cap, 2% stops):
    # balanced by $, not by count.
    props = [_tp("A", "long", 100.0, 95.0), _tp("B", "long", 100.0, 96.0),
             _tp("C", "long", 100.0, 94.0), _tp("D", "short", 100.0, 102.0),
             _tp("E", "short", 100.0, 102.0)]
    kept, _ = presize_and_balance(props, equity=_EQ, per_trade_risk_pct=_PTR)
    long_gross = sum(_notional(t) for t in kept if t.direction == "long")
    short_gross = sum(_notional(t) for t in kept if t.direction == "short")
    assert long_gross == pytest.approx(short_gross, rel=1e-6)
    assert long_gross == pytest.approx(5_000.0, rel=1e-6)


def test_one_sided_book_sizes_present_side_but_flags_imbalance():
    # SOFT neutrality: 2 longs, no shorts -> the longs DO size (the gate never blocks), but the
    # short target is 0, so balanced_gross is 0 -> the post-gate canary flags the one-sided book.
    props = [_tp("A", "long", 100.0, 98.5), _tp("B", "long", 50.0, 49.25)]  # tight stops
    kept, summary = presize_and_balance(props, equity=_EQ, per_trade_risk_pct=_PTR)
    assert len(kept) == 2                                  # not vetoed — they open
    assert summary["gross_long_target"] == pytest.approx(5_000.0)
    assert summary["gross_short_target"] == 0.0
    assert summary["balanced_gross"] == 0.0                # one-sided -> zero balanced (canary)


def test_held_short_nets_into_target():
    # held short $5k already at the per-side target; TWO new longs (tight 1.5% stops, each ~$2500 at
    # the per-name cap) -> long sleeve fills $5k to match the held short side.
    props = [_tp("A", "long", 100.0, 98.5), _tp("B", "long", 100.0, 98.5)]
    kept, summary = presize_and_balance(props, equity=_EQ, per_trade_risk_pct=_PTR,
                                        held_long=0.0, held_short=5_000.0)
    long_gross = sum(_notional(t) for t in kept if t.direction == "long")
    assert long_gross == pytest.approx(5_000.0, rel=1e-6)   # matches the held short side


def test_wide_stop_leg_clamps_risk_mult_to_one():
    # a leg targeting $5k at a very wide ~40% stop needs risk_pct >> cap -> risk_mult = 1.0
    props = [_tp("A", "long", 100.0, 60.0), _tp("B", "short", 100.0, 140.0)]  # 40% stops
    kept, _ = presize_and_balance(props, equity=_EQ, per_trade_risk_pct=_PTR)
    assert all(t.risk_mult <= 1.0 for t in kept)
    assert any(t.risk_mult == pytest.approx(1.0) for t in kept)   # at least one clamped


def test_risk_mult_clamped_in_unit_interval():
    props = [_tp("A", "long", 100.0, 95.0), _tp("B", "short", 100.0, 105.0)]
    kept, _ = presize_and_balance(props, equity=_EQ, per_trade_risk_pct=_PTR)
    assert all(0.0 < t.risk_mult <= 1.0 for t in kept)


def test_per_name_cap_limits_concentration():
    # a single short would otherwise fill the $5k short side; the per-name cap (0.25 of gross =
    # $2500) keeps any ONE name from dominating. Tight stops so the cap (not the clamp) binds.
    props = [_tp("L1", "long", 100.0, 98.5), _tp("L2", "long", 100.0, 98.5),
             _tp("S1", "short", 100.0, 101.5)]   # ONE short vs two longs
    kept, summary = presize_and_balance(props, equity=_EQ, per_trade_risk_pct=_PTR,
                                        max_name_frac=0.25)
    cap = 0.25 * _EQ
    for t in kept:
        assert _notional(t) <= cap + 1e-6        # no leg exceeds the per-name cap
    short_gross = sum(_notional(t) for t in kept if t.direction == "short")
    assert short_gross == pytest.approx(cap, rel=1e-6)   # lone short capped at $2500, not $5k
