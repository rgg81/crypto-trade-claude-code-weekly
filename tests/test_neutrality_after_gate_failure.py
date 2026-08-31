"""Dollar-neutrality must be enforced even when the gate produced NO report.

cy372 (a rebalance) timed out mid-execute AFTER the gate had applied all 4 planned closes but BEFORE
any of the 13 opens. Because the post-gate neutrality guard reads `rep["exposure"]` from the gate's
report, and there was no report, the guard never ran. The desk was left:

    L6/S9 | gross long $670.44 / short $1,085.81 | NET -$415.37 | TILT 0.2365

i.e. 23.65% net short — a direct breach of the mandate's hard invariant — held until the next tick.

cy368/cy370 were harmless because they hit HOLD cycles with nothing planned. A rebalance is the
dangerous case: closes land, opens do not, and nothing checks the result.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "auto_cycle", Path(__file__).resolve().parents[1] / "scripts" / "auto_cycle.py")
ac = importlib.util.module_from_spec(_spec)
sys.modules["auto_cycle"] = ac
_spec.loader.exec_module(ac)


def _pos(sym, direction, qty, entry):
    return {"symbol": sym, "direction": direction, "qty": qty, "entry": entry, "stop": entry * 0.9}


def test_exposure_is_computable_from_positions_without_the_gate():
    """The guard needs a truth source independent of a report the gate never wrote."""
    pos = [_pos("A", "long", 1.0, 100.0), _pos("B", "short", 2.0, 100.0)]
    e = ac.exposure_from_positions(pos)
    assert e["gross_long"] == 100.0
    assert e["gross_short"] == 200.0
    assert e["net"] == -100.0
    assert e["tilt"] == 1.0 / 3.0
    assert e["n_long"] == 1 and e["n_short"] == 1


def test_the_cy372_state_is_detected_as_a_neutrality_breach():
    """The exact partial-execution state must trip the guard threshold."""
    pos = [_pos("L", "long", 670.44, 1.0), _pos("S", "short", 1085.81, 1.0)]
    e = ac.exposure_from_positions(pos)
    assert round(e["tilt"], 4) == 0.2365
    assert ac._guard_should_trim(e), "23.65% net short must trip the guard"


def test_a_balanced_book_after_a_failed_gate_needs_no_trim():
    pos = [_pos("L", "long", 100.0, 1.0), _pos("S", "short", 100.0, 1.0)]
    assert not ac._guard_should_trim(ac.exposure_from_positions(pos))


def test_empty_or_one_sided_book_does_not_crash_the_check():
    assert ac.exposure_from_positions([])["tilt"] == 0.0
    one = ac.exposure_from_positions([_pos("L", "long", 100.0, 1.0)])
    assert one["tilt"] == 1.0 and one["n_short"] == 0


def test_gate_failure_path_reports_the_breach():
    """A HOLD-ON-DATA-OUTAGE that leaves the book off-neutral must SAY SO — silence here is how
    23.65% net short went unnoticed until the operator happened to look."""
    pos = [_pos("L", "long", 670.44, 1.0), _pos("S", "short", 1085.81, 1.0)]
    line = ac.neutrality_alarm(ac.exposure_from_positions(pos))
    assert line and "NEUTRALITY BREACH" in line
    assert "0.236" in line or "23.6" in line
    assert ac.neutrality_alarm(ac.exposure_from_positions(
        [_pos("L", "long", 100.0, 1.0), _pos("S", "short", 100.0, 1.0)])) == ""
