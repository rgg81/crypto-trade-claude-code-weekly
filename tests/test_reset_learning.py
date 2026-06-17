"""reset_desk --reset-learning: a STRATEGY pivot (not just a book reset) must also clear the
trade-derived learning corpus, else the new strategy inherits the OLD strategy's mis-contextualized
lessons/episodic (e.g. an aggressive 10x naked-long 'momentum long net-loses' prior wrongly
discouraging a HEDGED neutral long sleeve). This ARCHIVES (never deletes) the trade-derived memory
so the desk learns its own edge from scratch; the meta repair-journal is KEPT.
"""
from pathlib import Path

from scripts.reset_desk import do_reset_learning


def _seed_memory(m: Path):
    (m / "episodic").mkdir(parents=True, exist_ok=True)
    (m / "lessons").mkdir(parents=True, exist_ok=True)
    (m / "hitrate").mkdir(parents=True, exist_ok=True)
    (m / "semantic").mkdir(parents=True, exist_ok=True)
    (m / "procedural").mkdir(parents=True, exist_ok=True)
    (m / "episodic" / "journal-2026-06.jsonl").write_text('{"id":"x","realized_pnl":1.0}\n')
    (m / "flat-decisions.jsonl").write_text('{"symbol":"BTCUSDT"}\n')
    (m / "lessons" / "lessons.jsonl").write_text('{"text":"[CANDIDATE] LONG net-lost"}\n')
    (m / "hitrate" / "agent_scores.json").write_text('{"momentum": 0.4}')
    (m / "repair-journal.md").write_text("# Repair Journal\n- a code fix\n")


def test_reset_learning_archives_trade_derived_keeps_repair_journal(tmp_path):
    m = tmp_path / "memory"
    _seed_memory(m)
    res = do_reset_learning(str(m), "20260617T000000Z")
    arch = Path(res["archive_dir"])
    # trade-derived corpus is MOVED to the archive (recoverable), not left live
    assert not (m / "episodic" / "journal-2026-06.jsonl").exists()
    assert not (m / "flat-decisions.jsonl").exists()
    assert not (m / "lessons" / "lessons.jsonl").exists()
    assert not (m / "hitrate" / "agent_scores.json").exists()
    assert (arch / "episodic" / "journal-2026-06.jsonl").exists()      # archived, recoverable
    assert (arch / "lessons" / "lessons.jsonl").exists()
    # the META repair journal is KEPT (desk-agnostic, not a trade lesson)
    assert (m / "repair-journal.md").exists()
    assert "episodic" in res["archived"] and "lessons" in res["archived"]


def test_reset_learning_is_safe_on_missing_entries(tmp_path):
    m = tmp_path / "memory"
    m.mkdir()
    res = do_reset_learning(str(m), "20260617T000000Z")   # nothing to archive -> no error
    assert res["archived"] == []
