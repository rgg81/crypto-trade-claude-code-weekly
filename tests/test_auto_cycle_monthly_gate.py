"""The monthly-review gate must fire ONCE a month, not on every 30-min tick.

cy216 bug: the gate derived "last review" from the report FILENAME (`YYYY-MM.json` -> the 1st of
that month). The report is named by the month it COVERS and is rewritten in place on a re-run, so
the filename never advances: on the 31st the gate read "30 days ago", re-ran the review, rewrote
the same file, and re-fired on EVERY subsequent tick. The gate must key off when the review
ACTUALLY RAN (`run_ts` in the report, else the file mtime).
"""
import json
import os
from datetime import datetime, timedelta

from scripts.auto_cycle import (
    _last_review_ts,
    _monthly_review_due,
    _review_summary_line,
)


def _write(review_dir, name: str, *, run_ts: datetime | None = None, mtime: datetime | None = None):
    review_dir.mkdir(parents=True, exist_ok=True)
    body = {"review_date": name[:-5]}
    if run_ts is not None:
        body["run_ts"] = run_ts.isoformat()
    p = review_dir / name
    p.write_text(json.dumps(body))
    if mtime is not None:
        os.utime(p, (mtime.timestamp(), mtime.timestamp()))
    return p


def test_review_not_due_right_after_it_ran(tmp_path):
    now = datetime(2026, 7, 31, 0, 38)
    _write(tmp_path, "2026-07.json", run_ts=now)
    assert _monthly_review_due(tmp_path, now=now + timedelta(minutes=30)) is False


def test_month_end_report_does_not_refire_every_tick(tmp_path):
    """The exact cy216 production case: a report named 2026-07 written ON 2026-07-31."""
    ran_at = datetime(2026, 7, 31, 0, 38)
    _write(tmp_path, "2026-07.json", run_ts=ran_at)
    for tick in range(1, 8):  # every 30 min for the rest of the day
        assert _monthly_review_due(tmp_path, now=ran_at + timedelta(minutes=30 * tick)) is False


def test_review_due_after_30_days(tmp_path):
    ran_at = datetime(2026, 7, 31, 0, 38)
    _write(tmp_path, "2026-07.json", run_ts=ran_at)
    assert _monthly_review_due(tmp_path, now=ran_at + timedelta(days=29)) is False
    assert _monthly_review_due(tmp_path, now=ran_at + timedelta(days=30)) is True


def test_review_due_when_never_run(tmp_path):
    assert _monthly_review_due(tmp_path, now=datetime(2026, 7, 31)) is True
    assert _monthly_review_due(tmp_path / "missing", now=datetime(2026, 7, 31)) is True


def test_last_review_falls_back_to_mtime_when_run_ts_absent(tmp_path):
    """Legacy reports (written before run_ts existed) must still gate off a real run time."""
    ran_at = datetime(2026, 7, 31, 0, 38)
    _write(tmp_path, "2026-07.json", mtime=ran_at)
    assert _last_review_ts(tmp_path) == ran_at.replace(microsecond=0)
    assert _monthly_review_due(tmp_path, now=ran_at + timedelta(days=1)) is False


def test_run_ts_wins_over_mtime(tmp_path):
    """A copied/restored file gets a fresh mtime; the stamped run_ts is the truth."""
    ran_at = datetime(2026, 6, 1, 12, 0)
    _write(tmp_path, "2026-06.json", run_ts=ran_at, mtime=datetime(2026, 7, 31))
    assert _last_review_ts(tmp_path) == ran_at
    assert _monthly_review_due(tmp_path, now=datetime(2026, 7, 31)) is True


def test_last_review_takes_the_latest_of_several_reports(tmp_path):
    _write(tmp_path, "2026-05.json", run_ts=datetime(2026, 5, 30))
    _write(tmp_path, "2026-07.json", run_ts=datetime(2026, 7, 31))
    _write(tmp_path, "2026-06.json", run_ts=datetime(2026, 6, 29))
    assert _last_review_ts(tmp_path) == datetime(2026, 7, 31)


def test_tz_aware_run_ts_does_not_explode(tmp_path):
    """A tz-aware stamp must not raise on the naive-datetime subtraction."""
    p = tmp_path / "2026-07.json"
    tmp_path.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"run_ts": "2026-07-31T00:38:00+00:00"}))
    assert isinstance(_last_review_ts(tmp_path), datetime)
    assert _last_review_ts(tmp_path).tzinfo is None


def test_summary_line_carries_the_actual_metrics(tmp_path):
    """The loop printed bare 'Performance:'/'Neutrality:' headers with NO numbers — useless."""
    report = {
        "performance": {"monthly_return_pct": 3.67, "max_drawdown_pct": 3.16,
                        "final_equity": 10440.89, "cycles_analyzed": 216},
        "neutrality": {"cycles_checked": 216, "neutrality_violations": 0},
        "recommendations": [],
    }
    line = _review_summary_line(report)
    assert "+3.67%/mo" in line
    assert "3.16%" in line
    assert "0 neutrality violations" in line
    assert "216 cycles" in line
    assert "none" in line


def test_summary_line_flags_recommendations(tmp_path):
    report = {
        "performance": {"monthly_return_pct": 0.4, "max_drawdown_pct": 9.0,
                        "final_equity": 9000.0, "cycles_analyzed": 180},
        "neutrality": {"cycles_checked": 180, "neutrality_violations": 2},
        "recommendations": [{"type": "underperformance", "severity": "high"},
                            {"type": "churn", "severity": "medium"}],
    }
    line = _review_summary_line(report)
    assert "+0.40%/mo" in line
    assert "2 neutrality violations" in line
    assert "underperformance" in line


def test_summary_line_survives_a_partial_report():
    assert isinstance(_review_summary_line({}), str)
    assert isinstance(_review_summary_line({"performance": {}}), str)
