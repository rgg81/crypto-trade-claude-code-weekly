"""Adversarial-review hardening: a single malformed line in any append-only store (journal,
flat-decisions, lessons) must be SKIPPED, never raised — these feed both the trading audit and the
learning layer, and neither may crash the cycle over one corrupt line (disk/external corruption)."""
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from futures_fund.flat_journal import append_flat_decision, read_flat_decisions  # noqa: E402
from futures_fund.journal import append_decision, patch_outcome, read_all_decisions  # noqa: E402
from futures_fund.lessons import append_lesson, read_lessons  # noqa: E402

_T0 = datetime(2026, 6, 1, tzinfo=UTC)


def _corrupt(path: Path):
    with path.open("a") as fh:
        fh.write("{ this is not valid json at all \n")


def test_read_all_decisions_skips_a_corrupt_line(tmp_path):
    mem = tmp_path / "memory"
    did = append_decision(mem, {"cycle": 1, "symbol": "BTCUSDT", "direction": "long",
                                "entry": 100.0, "stop": 90.0, "size": 1.0, "ts": _T0})
    jf = next((mem / "episodic").glob("journal-*.jsonl"))
    _corrupt(jf)
    recs = read_all_decisions(mem)                       # must NOT raise
    assert len(recs) == 1 and recs[0]["id"] == did       # the good record survives


def test_patch_outcome_survives_a_corrupt_line(tmp_path):
    mem = tmp_path / "memory"
    did = append_decision(mem, {"cycle": 1, "symbol": "BTCUSDT", "direction": "long",
                                "entry": 100.0, "stop": 90.0, "size": 1.0, "ts": _T0})
    _corrupt(next((mem / "episodic").glob("journal-*.jsonl")))
    assert patch_outcome(mem, did, {"realized_pnl": 5.0}) is True    # must NOT raise; still patches


def test_read_flat_decisions_skips_a_corrupt_line(tmp_path):
    mem = tmp_path / "memory"
    append_flat_decision(mem, {"cycle": 1, "symbol": "WLDUSDT", "edge_aligned": True}, ts=_T0)
    _corrupt(mem / "flat-decisions.jsonl")
    assert len(read_flat_decisions(mem)) == 1


def test_read_lessons_skips_a_corrupt_line(tmp_path):
    mem = tmp_path / "memory"
    append_lesson(mem, {"text": "good lesson", "state": "candidate"}, ts=_T0)
    _corrupt(mem / "lessons" / "lessons.jsonl")
    lessons = read_lessons(mem)                          # must NOT raise on the bad line
    assert len(lessons) == 1 and lessons[0].text == "good lesson"
