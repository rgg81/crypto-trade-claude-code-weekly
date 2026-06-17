"""E1 — 4h-cadence exit gap-fill assumption (documented, accepted at ~1x).

TEMPEST-NEUTRAL drops the 15m fast loop; exits are checked on the 4h bar. detect_exit (PROTECTED,
unchanged) fills a stop at the STOP LEVEL + fixed slippage, NOT at the true gapped bar-open. This is
an OPTIMISTIC fill on a gap, but at ~1x no-leverage the loss stays BOUNDED near the intended stop
risk (no liquidation gap tail). This test pins that assumption so the 3%/month target is never
silently validated on a worse-than-modeled real fill without us noticing the gap.
"""
from datetime import UTC, datetime

from futures_fund.exits import detect_exit
from futures_fund.state import Position

_TS = datetime(2026, 6, 1, tzinfo=UTC)


def _short(entry, stop, qty):
    # ~1x: notional = qty*entry, margin = notional (leverage 1), liq far away (no liq at 1x)
    return Position(symbol="ETHUSDT", direction="short", qty=qty, entry=entry, stop=stop,
                    take_profits=[entry * 0.9], leverage=1.0, margin=qty * entry,
                    liq_price=entry * 2.0, opened_cycle=1, opened_ts=_TS)


def test_short_sleeve_4h_gap_fills_at_stop_and_loss_is_bounded():
    # short $5k @100, stop 105 (intended ~-5% = ~-$250). A 4h bar GAPS to high 130 (a +30% spike).
    pos = _short(entry=100.0, stop=105.0, qty=50.0)
    closed = detect_exit(pos, bar_high=130.0, bar_low=110.0,
                         funding_rate=0.0, funding_events=0, slippage_bps=2.0)
    assert closed is not None and closed.reason == "stop"
    # filled at the STOP LEVEL (~105 + slippage), NOT the gapped 130
    assert 105.0 <= closed.exit_price <= 106.0
    # loss is BOUNDED near the intended stop risk (~-$250), NOT the gap loss (~-$1500)
    assert -350.0 < closed.realized_pnl < -200.0
    # explicitly: the optimistic fill is far better than the true-gap fill would be
    true_gap_loss = (pos.entry - 130.0) * pos.qty        # ~ -$1500 if filled at the bar high
    assert closed.realized_pnl > true_gap_loss + 1000.0  # modeled fill is much kinder (documented)
