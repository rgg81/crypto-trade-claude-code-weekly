"""The monthly review runs on a FIXED cadence anchored to the first review, not "30 days since
the last run".

With the old rule any ad-hoc run reset the clock: running the review twice by hand on 2026-08-14
pushed the next automatic fire from 2026-08-30 out to 2026-09-13. A parameter review that slips
every time someone inspects it is not a monthly review.

Anchored schedule: boundaries fall at anchor + 30k days. A review is due when the current boundary
has passed and no review has run since it. Manual runs in between are recorded but do not move any
boundary.
"""
import json
from datetime import datetime, timedelta

from scripts.auto_cycle import _monthly_review_due, _review_anchor

ANCHOR = datetime(2026, 7, 31, 5, 38)


def _report(d, name, run_ts):
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps({"run_ts": run_ts.isoformat()}))


def test_anchor_is_the_earliest_review_not_the_latest(tmp_path):
    _report(tmp_path, "2026-07.json", ANCHOR)
    _report(tmp_path, "2026-08.json", datetime(2026, 8, 14, 18, 56))
    assert _review_anchor(tmp_path) == ANCHOR


def test_ad_hoc_run_does_not_push_the_next_boundary(tmp_path):
    """The live regression: two manual runs on Aug 14 must not delay the Aug 30 review."""
    _report(tmp_path, "2026-07.json", ANCHOR)
    _report(tmp_path, "2026-08.json", datetime(2026, 8, 14, 18, 56))
    assert _monthly_review_due(tmp_path, now=datetime(2026, 8, 29, 12, 0)) is False
    assert _monthly_review_due(tmp_path, now=ANCHOR + timedelta(days=30)) is True


def test_not_due_again_until_the_following_boundary(tmp_path):
    _report(tmp_path, "2026-07.json", ANCHOR)
    _report(tmp_path, "2026-08.json", ANCHOR + timedelta(days=30))      # the Aug 30 run
    assert _monthly_review_due(tmp_path, now=ANCHOR + timedelta(days=45)) is False
    assert _monthly_review_due(tmp_path, now=ANCHOR + timedelta(days=60)) is True


def test_manual_run_between_boundaries_does_not_suppress_the_next_one(tmp_path):
    _report(tmp_path, "2026-07.json", ANCHOR)
    _report(tmp_path, "2026-09.json", ANCHOR + timedelta(days=45))      # ad-hoc
    assert _monthly_review_due(tmp_path, now=ANCHOR + timedelta(days=50)) is False
    assert _monthly_review_due(tmp_path, now=ANCHOR + timedelta(days=60)) is True


def test_first_ever_review_is_due_when_no_reports_exist(tmp_path):
    assert _review_anchor(tmp_path) is None
    assert _monthly_review_due(tmp_path, now=datetime(2026, 8, 14)) is True
    assert _monthly_review_due(tmp_path / "missing", now=datetime(2026, 8, 14)) is True


def test_a_missed_boundary_still_fires_late(tmp_path):
    """If the loop was down across a boundary (the Aug 8-13 outage), the review must still run."""
    _report(tmp_path, "2026-07.json", ANCHOR)
    assert _monthly_review_due(tmp_path, now=ANCHOR + timedelta(days=37)) is True


def test_legacy_report_without_run_ts_falls_back_to_mtime(tmp_path):
    import os
    tmp_path.mkdir(parents=True, exist_ok=True)
    p = tmp_path / "2026-07.json"
    p.write_text(json.dumps({"review_date": "2026-07"}))
    os.utime(p, (ANCHOR.timestamp(), ANCHOR.timestamp()))
    assert _review_anchor(tmp_path) == ANCHOR.replace(microsecond=0)


def test_tz_aware_run_ts_is_normalised(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "2026-07.json").write_text(
        json.dumps({"run_ts": "2026-07-31T05:38:00+00:00"}))
    a = _review_anchor(tmp_path)
    assert a is not None and a.tzinfo is None
