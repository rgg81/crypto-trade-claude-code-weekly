"""SWAP netting (regression for the cycle-5 swap net-short flip).

When the CIO rotates a held leg (CLOSE A long + OPEN B long in its place), the pre-sizer must net
the CLOSING leg OUT of held_long/held_short before sizing the new leg — else held_long still counts
A, the symmetric trim sizes B tiny (the long side already looks full), and once A closes the book
flips net-SHORT. The orchestration computes the held exposure for the pre-sizer from the positions
that SURVIVE the management-close set, via `_surviving_for_presize`.
"""
import pytest

from futures_fund.models import TradeProposal
from futures_fund.neutral_book import presize_and_balance
from futures_fund.orchestration import _surviving_for_presize
from futures_fund.sizing import qty_from_risk

_EQ = 10_000.0


class _Pos:
    def __init__(self, symbol):
        self.symbol = symbol


def test_surviving_for_presize_excludes_closing_legs():
    positions = [_Pos("WLDUSDT"), _Pos("UNIUSDT"), _Pos("HYPEUSDT"),
                 _Pos("BNBUSDT"), _Pos("DOGEUSDT"), _Pos("BTCUSDT")]
    surviving = _surviving_for_presize(positions, {"HYPEUSDT"})
    assert [p.symbol for p in surviving] == \
        ["WLDUSDT", "UNIUSDT", "BNBUSDT", "DOGEUSDT", "BTCUSDT"]
    # empty close-set => unchanged (HOLD/open-only cycles are a no-op)
    assert _surviving_for_presize(positions, set()) == positions


def test_swap_sizes_replacement_to_refill_long_side_net_zero():
    # cycle-5 shape: held WLD+UNI ($2445 surviving) + BNB/DOGE/BTC short ($3549); HYPE ($1063) is
    # being CLOSED and XPL opened. With held_long EXCLUDING the closing HYPE, the pre-sizer sizes
    # XPL to refill the long side to ~$3549 (== short side) -> the realized book stays net~0.
    xpl = TradeProposal(symbol="XPLUSDT", direction="long", entry=0.1033, stop=0.0955,
                        take_profits=[0.12], atr=0.008, confidence=0.7, horizon_hours=16.0,
                        funding_rate=0.0)
    kept, _ = presize_and_balance(
        [xpl], equity=_EQ, per_trade_risk_pct=0.01, held_long=2445.0, held_short=3549.0,
        risk_pct_by_symbol={"XPLUSDT": 0.01}, heat_headroom_by_symbol={"XPLUSDT": 0.10})
    assert kept, "XPL must size to refill the long side, not drop"
    xpl_notional = qty_from_risk(_EQ, 0.01 * kept[0].risk_mult, kept[0].entry, kept[0].stop) \
        * kept[0].entry
    final_long = 2445.0 + xpl_notional
    assert final_long == pytest.approx(3549.0, rel=0.03)   # refilled to match short side, net~0
