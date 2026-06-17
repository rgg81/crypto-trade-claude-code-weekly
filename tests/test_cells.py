"""Phase 3 — per-CELL attribution backend. A lesson is a claim about ONE (regime x desk x direction)
cell; its promotion is gated on THAT cell's DSR, not the desk-wide track record. Below 10 closed
trades a cell's DSR is 0.0, so a cell-specific rule can't validate until its own cell is proven."""
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from futures_fund.cells import cell_dsr, cell_returns, cell_table  # noqa: E402
from futures_fund.journal import append_decision, patch_outcome  # noqa: E402

_T0 = datetime(2026, 6, 1, tzinfo=UTC)
_FP = "risk_on|momentum|long"


def _trade(mem, *, cycle, ret, regime="risk_on", desk="momentum", direction="long"):
    # entry 100 x size 1 => notional 100; realized_pnl = ret * 100 => per-trade return == ret
    did = append_decision(mem, {"cycle": cycle, "symbol": f"S{cycle}", "direction": direction,
                                "entry": 100.0, "stop": 90.0, "size": 1.0, "ts": _T0})
    patch_outcome(mem, did, {"exit_ts": _T0, "realized_pnl": ret * 100.0, "r_multiple": ret * 10,
                             "regime": regime, "desk": desk})
    return did


def test_cell_returns_extracts_per_trade_return_for_the_fingerprint(tmp_path):
    mem = tmp_path / "memory"
    _trade(mem, cycle=1, ret=0.02)
    _trade(mem, cycle=2, ret=-0.01)
    _trade(mem, cycle=3, ret=0.03, desk="carry")   # different cell
    rs = cell_returns(mem, _FP)
    assert sorted(round(r, 4) for r in rs) == [-0.01, 0.02]   # only the risk_on|momentum|long cell


def test_cell_dsr_is_zero_below_ten_trades(tmp_path):
    mem = tmp_path / "memory"
    for c in range(5):
        _trade(mem, cycle=c, ret=0.05)
    assert cell_dsr(mem, _FP) == 0.0          # <10 obs -> deflated-Sharpe floor


def test_cell_dsr_is_positive_for_a_strong_proven_cell(tmp_path):
    mem = tmp_path / "memory"
    for c in range(12):                        # 12 consistently positive trades -> real edge
        _trade(mem, cycle=c, ret=0.02 + 0.001 * (c % 3))
    dsr = cell_dsr(mem, _FP)
    assert dsr > 0.0                           # a genuine positive Sharpe deflates to a real value


def test_cell_table_reports_per_fingerprint_stats(tmp_path):
    mem = tmp_path / "memory"
    _trade(mem, cycle=1, ret=0.02)
    _trade(mem, cycle=2, ret=0.04)
    _trade(mem, cycle=3, ret=-0.02, desk="carry")
    table = cell_table(mem)
    assert table[_FP]["n"] == 2 and abs(table[_FP]["mean_return"] - 0.03) < 1e-9
    assert "risk_on|carry|long" in table
