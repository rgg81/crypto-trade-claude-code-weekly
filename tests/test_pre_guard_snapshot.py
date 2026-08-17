"""The post-gate neutrality guard must not destroy the gate's own diagnostics.

The guard writes its own trim-only `proposals.json` and re-runs the gate, which overwrites
`report.json`. So after a guard pass the cycle dir describes the TRIM, not the original execution:
`dropped: 0`, `drop_reasons: []`, `proposals: []`. At cy290/cy291 short-sleeve opens were silently
dropped, the guard trimmed the long sleeve 12% then 83% to force neutrality, deployment collapsed
0.85x -> 0.11x, and the reason the opens were dropped was unrecoverable — the evidence had been
overwritten by the thing reacting to it.

Snapshot the gate report and the blended plan BEFORE the guard runs, so the drop reason survives.
"""
import json

from scripts.auto_cycle import _snapshot_pre_guard


def test_snapshot_writes_the_gate_report(tmp_path):
    rep = {"opened": 1, "closed": 1, "dropped": 1,
           "drop_reasons": [{"symbol": "HYPEUSDT", "reason": "heat cap"}]}
    _snapshot_pre_guard(tmp_path, rep)
    saved = json.loads((tmp_path / "report_pre_guard.json").read_text())
    assert saved["dropped"] == 1
    assert saved["drop_reasons"][0]["symbol"] == "HYPEUSDT"


def test_snapshot_preserves_the_blended_plan_proposals(tmp_path):
    """The guard replaces proposals.json with its trim list; the original opens must survive."""
    (tmp_path / "proposals.json").write_text(json.dumps(
        {"proposals": [{"symbol": "HYPEUSDT", "direction": "short"}],
         "management": [{"symbol": "BTCUSDT", "action": "close"}]}))
    _snapshot_pre_guard(tmp_path, {"opened": 1})
    plan = json.loads((tmp_path / "proposals_pre_guard.json").read_text())
    assert plan["proposals"][0]["symbol"] == "HYPEUSDT"
    assert plan["management"][0]["action"] == "close"


def test_snapshot_survives_a_subsequent_guard_overwrite(tmp_path):
    """Simulates the real sequence: snapshot, then the guard clobbers both live files."""
    (tmp_path / "proposals.json").write_text(json.dumps(
        {"proposals": [{"symbol": "XRPUSDT", "direction": "short"}], "management": []}))
    _snapshot_pre_guard(tmp_path, {"opened": 1, "dropped": 2})
    (tmp_path / "proposals.json").write_text(json.dumps({"proposals": [], "management": []}))
    (tmp_path / "report.json").write_text(json.dumps({"opened": 0, "dropped": 0}))
    assert json.loads((tmp_path / "report_pre_guard.json").read_text())["dropped"] == 2
    assert json.loads(
        (tmp_path / "proposals_pre_guard.json").read_text())["proposals"][0]["symbol"] == "XRPUSDT"


def test_snapshot_is_best_effort_and_never_raises(tmp_path):
    """Diagnostics must never break the driver — a bad dir or unserialisable report is tolerated."""
    _snapshot_pre_guard(tmp_path / "does" / "not" / "exist", {"opened": 1})
    _snapshot_pre_guard(tmp_path, {"bad": {1, 2, 3}})     # sets are not JSON-serialisable
    _snapshot_pre_guard(tmp_path, None)


def test_missing_proposals_file_is_tolerated(tmp_path):
    _snapshot_pre_guard(tmp_path, {"opened": 0})
    assert (tmp_path / "report_pre_guard.json").exists()
    assert not (tmp_path / "proposals_pre_guard.json").exists()
