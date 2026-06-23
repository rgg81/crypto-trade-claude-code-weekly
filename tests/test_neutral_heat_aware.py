"""HEAT-AWARE dollar-neutral pre-size (regression for the cycle-2 net-short flip).

The deterministic gate (risk_gate.evaluate) clamps each NEW leg's per-trade risk to the heat
HEADROOM of that leg's OWN regime: effective_risk_pct = min(risk_pct, max_heat(regime) - used_heat).
A balanced multi-leg neutral book at ~1x can SATURATE the heat cap (esp. high_vol_range, cap 0.04),
so a new long in a strict regime gets clamped to dust and DROPPED by consolidate, while a new short
in a looser regime opens full -> the book FLIPS net-short (neutrality broken) AND the drop is mute.

The pre-sizer (NON-protected) must model that clamp: cap each new leg by its heat headroom, DROP a
leg whose deployable notional falls below the consolidate dust floor (counted, never silent), and
SYMMETRICALLY trim the opposite side so the realized gross_long$ == gross_short$ stays balanced.
The protected gate is untouched — this only makes the upstream balancer honest about the heat cap.
"""
import pytest

from futures_fund.models import TradeProposal
from futures_fund.neutral_book import presize_and_balance
from futures_fund.sizing import qty_from_risk

_EQ = 10_000.0


def _tp(symbol, direction, entry, stop):
    return TradeProposal(symbol=symbol, direction=direction, entry=entry, stop=stop,
                         take_profits=[entry * (1.1 if direction == "long" else 0.9)],
                         atr=entry * 0.02, confidence=0.6, horizon_hours=16.0, funding_rate=0.0)


def _notional(tp, ptr):
    return qty_from_risk(_EQ, ptr * tp.risk_mult, tp.entry, tp.stop) * tp.entry


def test_wide_stop_longs_rm_clamped_do_not_flip_book_net_short():
    # Cycle-1 (post-reset) regression: a FRESH flat book of WIDE-stop high-vol-alt longs
    # (~7-10% stops) cannot reach the equity/2 target -- hitting it needs risk_mult > 1, which the
    # gate clamps to 1.0 (per-trade risk ceiling) -> each long deploys ~ptr*equity*entry/|stop|,
    # well under target. TIGHT-stop shorts (~1.5-3% stops) hit target easily. Without rm-clamp
    # awareness the longs come in ~$3.6k vs shorts ~$5k -> the book flips ~16% net-SHORT.
    # The pre-sizer must cap each leg by its rm=1.0 notional and SYMMETRICALLY trim the other side.
    longs = [_tp("WLDUSDT", "long", 0.6404, 0.5772), _tp("UNIUSDT", "long", 3.189, 2.8887),
             _tp("HYPEUSDT", "long", 72.258, 67.4736)]            # ~7-10% stops
    shorts = [_tp("BNBUSDT", "short", 599.02, 617.5), _tp("DOGEUSDT", "short", 0.08537, 0.087875),
              _tp("BTCUSDT", "short", 64271.3, 65250.0)]           # ~1.5-3% stops
    ptr_by = {"WLDUSDT": 0.01, "UNIUSDT": 0.01, "HYPEUSDT": 0.01,
              "BNBUSDT": 0.015, "DOGEUSDT": 0.01, "BTCUSDT": 0.005}
    heat_by = {s: 0.10 for s in ptr_by}   # ample heat -> isolate the rm=1.0 clamp
    kept, summary = presize_and_balance(
        longs + shorts, equity=_EQ, per_trade_risk_pct=0.01, held_long=0.0, held_short=0.0,
        risk_pct_by_symbol=ptr_by, heat_headroom_by_symbol=heat_by)

    gl = sum(_notional(t, ptr_by[t.symbol]) for t in kept if t.direction == "long")
    gs = sum(_notional(t, ptr_by[t.symbol]) for t in kept if t.direction == "short")
    # longs deploy at their rm=1.0 ceilings (~$3.6k total); shorts TRIMMED to match -> net ~0
    assert gl == pytest.approx(gs, rel=0.03), f"book not neutral: long {gl:.0f} vs short {gs:.0f}"
    tilt = abs(gl - gs) / (gl + gs)
    assert tilt < 0.03, f"tilt {tilt:.3f} too high"
    # every long is at its rm=1.0 ceiling (risk_mult pinned to ~1.0, the binding cap)
    for t in kept:
        if t.direction == "long":
            assert t.risk_mult == pytest.approx(1.0, abs=1e-6)


def test_heat_starved_long_does_not_flip_book_net_short():
    # Held book net +800 long (held_long 4400 vs held_short 3600), already ~saturating heat.
    # New L (long, high_vol_range, ~0 headroom) + S (short, high_vol_trend, room). Without heat
    # awareness the gate clamps L to dust (dropped) and opens S full -> net short. With it: L is
    # dropped AND S is trimmed so both sides finish at 4400 (net 0).
    L = _tp("LUSDT", "long", 100.0, 96.0)     # entry/|stop| = 25
    S = _tp("SUSDT", "short", 100.0, 102.0)   # entry/|stop| = 50
    ptr_by = {"LUSDT": 0.005, "SUSDT": 0.01}
    heat_by = {"LUSDT": 0.0003, "SUSDT": 0.04}   # L starved, S roomy
    kept, summary = presize_and_balance(
        [L, S], equity=_EQ, per_trade_risk_pct=0.01, held_long=4400.0, held_short=3600.0,
        risk_pct_by_symbol=ptr_by, heat_headroom_by_symbol=heat_by)

    kept_syms = {t.symbol for t in kept}
    assert "LUSDT" not in kept_syms          # heat-starved long DROPPED (not sized to dust)
    assert "SUSDT" in kept_syms              # short kept...
    s = next(t for t in kept if t.symbol == "SUSDT")
    s_notional = _notional(s, 0.01)
    # ...but TRIMMED to ~800 so final short = 3600 + 800 = 4400 == final long 4400 (held only)
    assert s_notional == pytest.approx(800.0, rel=0.02)
    final_long = 4400.0 + sum(_notional(t, ptr_by[t.symbol])
                              for t in kept if t.direction == "long")
    final_short = 3600.0 + sum(_notional(t, ptr_by[t.symbol])
                               for t in kept if t.direction == "short")
    assert final_long == pytest.approx(final_short, rel=0.02)   # book stays dollar-neutral
    # the drop is OBSERVABLE, never silent
    assert summary["n_dropped"] >= 1
    assert summary.get("heat_dropped")  # names/symbols the heat ceiling forced out


def test_ample_headroom_is_a_no_op_vs_heat_blind():
    # When every leg has ample headroom, heat awareness must reproduce the heat-blind book exactly
    # (same kept legs, same stamped risk_mult) — it only ever SHRINKS a starved leg, never alters a
    # leg that fits.
    def book():
        return [_tp("LUSDT", "long", 100.0, 95.0), _tp("SUSDT", "short", 100.0, 105.0)]
    blind, blind_sum = presize_and_balance(
        book(), equity=_EQ, per_trade_risk_pct=0.01, held_long=0.0, held_short=0.0)
    aware, aware_sum = presize_and_balance(
        book(), equity=_EQ, per_trade_risk_pct=0.01, held_long=0.0, held_short=0.0,
        heat_headroom_by_symbol={"LUSDT": 0.05, "SUSDT": 0.05})
    assert {t.symbol: round(t.risk_mult, 9) for t in aware} == \
           {t.symbol: round(t.risk_mult, 9) for t in blind}
    long_gross = sum(_notional(t, 0.01) for t in aware if t.direction == "long")
    short_gross = sum(_notional(t, 0.01) for t in aware if t.direction == "short")
    assert long_gross == pytest.approx(short_gross, rel=1e-6)   # still dollar-neutral
    assert not aware_sum.get("heat_dropped")


def test_heat_blind_when_headroom_not_supplied():
    # Backward compat: no heat_headroom_by_symbol -> identical to the pre-heat behavior.
    L = _tp("LUSDT", "long", 100.0, 95.0)
    S = _tp("SUSDT", "short", 100.0, 105.0)
    kept, summary = presize_and_balance(
        [L, S], equity=_EQ, per_trade_risk_pct=0.01, held_long=0.0, held_short=0.0)
    assert {t.symbol for t in kept} == {"LUSDT", "SUSDT"}
    assert "heat_dropped" not in summary or not summary["heat_dropped"]


def test_waterfill_redistributes_capped_leg_budget_to_uncapped_legs():
    # A WIDE-stop long (rm=1.0 ceiling $500, far below the $1667 equal share) on a side with two
    # TIGHT-stop longs. The wide leg's stranded ~$1166 of side budget must be REDISTRIBUTED to the
    # tight legs (water-fill) so the long side fills toward equity/2 -- instead of equal-splitting
    # and stranding it (the 0.55x under-deployment). Tight shorts let the short side match.
    wide = _tp("WIDEUSDT", "long", 100.0, 80.0)       # 20% stop -> ceiling 0.01*10000/0.20 = $500
    tightL1 = _tp("TLA", "long", 100.0, 98.0)          # 2% stop  -> ceiling capped at name_cap
    tightL2 = _tp("TLB", "long", 100.0, 98.0)
    shorts = [_tp("TSA", "short", 100.0, 102.0), _tp("TSB", "short", 100.0, 102.0),
              _tp("TSC", "short", 100.0, 102.0)]
    ptr_by = {s: 0.01 for s in ["WIDEUSDT", "TLA", "TLB", "TSA", "TSB", "TSC"]}
    heat_by = {s: 0.10 for s in ptr_by}
    kept, _ = presize_and_balance(
        [wide, tightL1, tightL2, *shorts], equity=_EQ, per_trade_risk_pct=0.01,
        risk_pct_by_symbol=ptr_by, heat_headroom_by_symbol=heat_by)
    byc = {t.symbol: _notional(t, 0.01) for t in kept}
    assert byc["WIDEUSDT"] == pytest.approx(500.0, rel=0.05)     # wide pinned at its rm=1 ceiling
    # each tight long absorbs the stranded budget -> ~$2250, well ABOVE the $1667 equal split
    assert byc["TLA"] > 2000.0 and byc["TLB"] > 2000.0
    gl = sum(v for s, v in byc.items() if s in ("WIDEUSDT", "TLA", "TLB"))
    gs = sum(v for s, v in byc.items() if s in ("TSA", "TSB", "TSC"))
    assert gl == pytest.approx(gs, rel=0.03)                        # still dollar-neutral
    assert gl > 4500.0                                             # long side ~filled to equity/2
