"""The monthly review must not hand out advice for a strategy the desk no longer runs.

At cy366 the review fired against the RETIRED blended book: it is titled "blended all-weather",
reads `blended_book_cli._structure` for the live stop, and recommended tightening the stop to
0.75xATR. The live desk is the cross-sectional factor book, where a TIGHT stop is precisely the
defect that inverted the strategy (2xATR -> sharpe -1.63 vs +2.33 unstopped; the desk now runs
8xATR). Nothing auto-applies, but a stale recommendation that points the wrong way is a landmine for
whoever reads it next.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "monthly_review", _ROOT / "scripts" / "monthly_review.py")
mr = importlib.util.module_from_spec(_spec)
sys.modules["monthly_review"] = mr
_spec.loader.exec_module(mr)


def test_active_book_engine_is_detected_from_the_driver():
    """Read what auto_cycle ACTUALLY calls rather than assuming."""
    assert mr.active_book_engine(_ROOT / "scripts" / "auto_cycle.py") == "xsection"


def test_engine_detection_reports_blended_when_that_is_what_is_wired(tmp_path):
    p = tmp_path / "driver.py"
    p.write_text('run(["scripts/blended_book_cli.py", "--cycle", str(cycle)])\n')
    assert mr.active_book_engine(p) == "blended"


def test_engine_detection_is_unknown_when_neither_is_wired(tmp_path):
    p = tmp_path / "driver.py"
    p.write_text("print('hello')\n")
    assert mr.active_book_engine(p) == "unknown"


def test_stop_recommendation_is_suppressed_when_the_blended_book_is_not_live():
    """The atr_mult replay measures the BLENDED book's stop. If that book is not the live one the
    number is meaningless here, and pointing it at the factor desk would be harmful."""
    rec = {"recommend": 0.75, "expected_gain": 507.73, "reason": "improved in both halves"}
    out = mr.gate_stop_recommendation(rec, engine="xsection")
    assert out["recommend"] is None
    assert "xsection" in out["suppressed_because"]
    # and the original figure is preserved for the record, not deleted
    assert out["measured_for_blended"]["recommend"] == 0.75


def test_stop_recommendation_passes_through_when_blended_IS_live():
    rec = {"recommend": 0.75, "expected_gain": 507.73, "reason": "improved in both halves"}
    out = mr.gate_stop_recommendation(rec, engine="blended")
    assert out["recommend"] == 0.75


def test_the_live_review_actually_suppresses_its_stop_recommendation():
    """END-TO-END on the committed report shape: run the review and confirm the recommendation is
    gated, not merely gate-able."""
    import subprocess
    r = subprocess.run([sys.executable, str(_ROOT / "scripts" / "monthly_review.py"),
                        "--state", str(_ROOT / "state")],
                       capture_output=True, text=True, cwd=_ROOT)
    assert "STALE REVIEW" in r.stdout, r.stdout[-2000:]
    assert "'xsection'" in r.stdout
    # The banner alone is NOT proof — it prints independently of the gate (my first version of this
    # test passed with the gate removed). Assert the RECOMMENDATION itself is actually suppressed,
    # both on the console and in the persisted report.
    assert "verdict: SUPPRESSED" in r.stdout, r.stdout[-2000:]
    import glob
    import json as _json
    latest = sorted(glob.glob(str(_ROOT / "state" / "monthly_review" / "*.json")))[-1]
    rep = _json.loads(Path(latest).read_text())
    sm = rep["stop_multiple"]
    assert sm["recommend"] is None, f"a stale atr_mult recommendation survived: {sm}"
    assert "suppressed_because" in sm
    assert sm["measured_for_blended"]["recommend"] is not None, (
        "the underlying measurement must be preserved for the record, not deleted")
