"""Phase 1 — the flat-verdict bridge: the CIO's declined edge-aligned setups (written inside
cio.json) must reach the flat-decision journal so evaluate_pending_flats can later score whether
standing aside COST the desk (the source of ENABLING lessons). Must be IDEMPOTENT per (cycle,
symbol) so a RETRY never double-journals, and must fill regime/mark when the CIO omitted them."""
from datetime import UTC, datetime

from futures_fund.flat_journal import read_flat_decisions, record_cycle_flat_verdicts

_NOW = datetime(2026, 6, 1, tzinfo=UTC)


def test_bridge_records_fills_defaults_and_is_idempotent(tmp_path):
    verdicts = [{"symbol": "WLDUSDT", "reason": "squeeze-long edge but no defined-risk entry",
                 "edge_aligned": True, "favored_side": "long"},
                {"symbol": "XRPUSDT", "reason": "no edge", "edge_aligned": False}]
    ids = record_cycle_flat_verdicts(tmp_path, 30, verdicts, _NOW, regime="low_vol_trend",
                                     marks={"WLDUSDT": 0.515, "XRPUSDT": 1.08})
    assert len(ids) == 2
    rows = read_flat_decisions(tmp_path)
    wld = next(r for r in rows if r["symbol"] == "WLDUSDT")
    assert wld["regime"] == "low_vol_trend" and wld["mark"] == 0.515 and wld["cycle"] == 30
    assert wld["edge_aligned"] is True

    # RETRY the SAME cycle -> nothing new appended (idempotent per cycle+symbol)
    again = record_cycle_flat_verdicts(tmp_path, 30, verdicts, _NOW, regime="low_vol_trend")
    assert again == [] and len(read_flat_decisions(tmp_path)) == 2

    # a NEW cycle for the same symbol DOES append
    more = record_cycle_flat_verdicts(tmp_path, 31, verdicts[:1], _NOW, regime="low_vol_trend")
    assert len(more) == 1 and len(read_flat_decisions(tmp_path)) == 3


def test_bridge_skips_verdicts_without_symbol(tmp_path):
    ids = record_cycle_flat_verdicts(tmp_path, 5, [{"reason": "malformed, no symbol"}], _NOW)
    assert ids == [] and read_flat_decisions(tmp_path) == []
