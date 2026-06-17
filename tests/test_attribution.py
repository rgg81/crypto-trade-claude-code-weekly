"""Phase 0 — attribution substrate. Closed journal records must carry the join keys
(desk x regime x close_reason x r_multiple) so the learner can later slice experience by
'which desk lost, in which regime, and why'. Stamped by a post-cycle orchestration re-patch
(NEVER by editing the protected cycle.py) — see futures_fund/attribution.stamp_cycle_attribution.
"""
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from futures_fund.attribution import stamp_cycle_attribution  # noqa: E402
from futures_fund.journal import append_decision, patch_outcome, read_all_decisions  # noqa: E402

_NOW = datetime(2026, 6, 11, tzinfo=UTC)


def _open(mem, *, cycle, sym, direction="short", entry=100.0, stop=110.0, size=10.0):
    return append_decision(mem, {"cycle": cycle, "symbol": sym, "direction": direction,
                                 "entry": entry, "stop": stop, "size": size, "ts": _NOW})


def test_stamp_fills_desk_regime_close_reason_r_multiple(tmp_path):
    mem = tmp_path / "memory"
    did = _open(mem, cycle=5, sym="ETHUSDT", entry=100.0, stop=110.0, size=10.0)
    # close it at a -100 loss (the gate's normal close patch — note: no close_reason/r_multiple)
    patch_outcome(mem, did, {"exit_ts": _NOW, "realized_pnl": -100.0, "prediction_correct": False})
    report = {"actions": [{"close": "ETHUSDT", "reason": "stop", "pnl": -100.0}]}

    n = stamp_cycle_attribution(mem, 5, report, "risk_off", {"ETHUSDT": "momentum"}, _NOW)

    assert n >= 1
    rec = next(r for r in read_all_decisions(mem) if r["id"] == did)
    assert rec["desk"] == "momentum"
    assert rec["regime"] == "risk_off"
    assert rec["close_reason"] == "stop"
    # original risk = |100-110| * 10 = 100; r_multiple = realized_pnl / risk = -100/100 = -1.0
    assert abs(rec["r_multiple"] - (-1.0)) < 1e-9


def test_stamp_is_idempotent_and_does_not_overwrite_existing(tmp_path):
    mem = tmp_path / "memory"
    did = _open(mem, cycle=7, sym="DOGEUSDT")
    patch_outcome(mem, did, {"exit_ts": _NOW, "realized_pnl": 50.0,
                             "desk": "carry", "regime": "risk_on", "close_reason": "tp"})
    report = {"actions": [{"close": "DOGEUSDT", "reason": "holdings_close", "pnl": 50.0}]}
    # a DIFFERENT regime/desk passed in must NOT overwrite the already-stamped fields
    stamp_cycle_attribution(mem, 7, report, "risk_off", {"DOGEUSDT": "momentum"}, _NOW)
    rec = next(r for r in read_all_decisions(mem) if r["id"] == did)
    assert rec["desk"] == "carry" and rec["regime"] == "risk_on" and rec["close_reason"] == "tp"


def test_stamp_does_not_mislabel_an_old_close_of_the_same_symbol(tmp_path):
    mem = tmp_path / "memory"
    # an OLD ETH close (cycle 3) that legacy-lacks close_reason
    old = _open(mem, cycle=3, sym="ETHUSDT", entry=200.0, stop=210.0, size=5.0)
    patch_outcome(mem, old, {"exit_ts": datetime(2026, 6, 9, tzinfo=UTC), "realized_pnl": -20.0,
                             "prediction_correct": False})
    # a NEW ETH open+close this cycle (cycle 9)
    new = _open(mem, cycle=9, sym="ETHUSDT", entry=100.0, stop=110.0, size=10.0)
    patch_outcome(mem, new, {"exit_ts": _NOW, "realized_pnl": -100.0, "prediction_correct": False})
    report = {"actions": [{"close": "ETHUSDT", "reason": "stop", "pnl": -100.0}]}

    stamp_cycle_attribution(mem, 9, report, "risk_off", {"ETHUSDT": "momentum"}, _NOW)

    recs = {r["id"]: r for r in read_all_decisions(mem)}
    # only the just-closed (most-recent exit_ts) ETH gets this cycle's close_reason; the old one
    # is NOT mislabeled with the current reason.
    assert recs[new].get("close_reason") == "stop"
    assert recs[old].get("close_reason") in (None, "")


def test_stamp_never_raises_on_degraded_inputs(tmp_path):
    mem = tmp_path / "memory"
    _open(mem, cycle=5, sym="ETHUSDT")
    # missing actions / None desk map / weird report shape must not raise (fail-safe at call site)
    assert stamp_cycle_attribution(mem, 5, {}, None, None, _NOW) == 0
    assert stamp_cycle_attribution(mem, 5, {"actions": [{"junk": 1}]}, "x", {}, _NOW) >= 0


def test_stamp_backfills_r_multiple_for_a_fast_loop_close_absent_from_the_report(tmp_path):
    # A position closed by the FAST exit sweep is patched with realized_pnl but never appears in a
    # STRATEGIC report's `actions` (the fast loop runs no attribution). The next strategic stamp
    # must still backfill its r_multiple (derivable from the record) so the miner can learn from it
    # — close_reason stays None (needs the report). Live cy23->cy24: VELVET stopped out.
    mem = tmp_path / "memory"
    did = _open(mem, cycle=22, sym="VELVET", direction="long", entry=1.545, stop=1.45, size=100.0)
    patch_outcome(mem, did, {"exit_ts": _NOW, "realized_pnl": -9.5, "regime": "risk_on",
                             "desk": "momentum"})   # fast-loop close: pnl set, no r_multiple/reason
    # a LATER strategic cycle stamps, but its report has NOTHING about VELVET (it closed earlier)
    n = stamp_cycle_attribution(mem, 24, {"actions": []}, "risk_on", {}, _NOW)
    rec = next(r for r in read_all_decisions(mem) if r["id"] == did)
    # risk = |1.545-1.45|*100 = 9.5; r_multiple = -9.5/9.5 = -1.0  -> now visible to the miner
    assert abs(rec["r_multiple"] - (-1.0)) < 1e-9
    assert rec.get("close_reason") in (None, "")   # close_reason still unknown (not in any report)
    assert n >= 1


def test_stamp_handles_mixed_string_and_dict_actions(tmp_path):
    # the gate report's `actions` is a MIXED list — close/open dicts AND plain-STRING warnings
    # (e.g. a stale-trigger auto-cancel). A string action must not crash the stamp (live cy22 bug:
    # AttributeError 'str' object has no attribute 'get'). The close dict is still attributed.
    mem = tmp_path / "memory"
    did = _open(mem, cycle=8, sym="SOLUSDT", entry=100.0, stop=110.0, size=10.0)
    patch_outcome(mem, did, {"exit_ts": _NOW, "realized_pnl": -100.0, "prediction_correct": False})
    report = {"actions": [
        "auto-canceled STALE long stop_entry WLDUSDT @ 0.515 — swing resistance crossed",
        {"close": "SOLUSDT", "reason": "stop", "pnl": -100.0},
        {"open": "AIOUSDT", "direction": "long"},
    ]}
    n = stamp_cycle_attribution(mem, 8, report, "risk_on", {"SOLUSDT": "carry"}, _NOW)
    assert n >= 1
    rec = next(r for r in read_all_decisions(mem) if r["id"] == did)
    assert rec["close_reason"] == "stop" and rec["desk"] == "carry" and rec["regime"] == "risk_on"
