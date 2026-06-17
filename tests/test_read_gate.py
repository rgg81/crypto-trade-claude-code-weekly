"""Adversarial must-fix #1 — the READ-PATH gate. Auto-MINED candidates must EARN their way into an
agent prompt (enough sample OR a recurrence); a thin one-off cohort summary is tracked but withheld.
Curated/legacy candidates are trusted, and validated standing rules always inject. Injected lessons
carry an HONEST confidence tag so the desk never mistakes an unproven prior for a rule."""
from datetime import UTC, datetime

from futures_fund.lessons import append_lesson, format_lesson, retrieve_lessons

_NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _add(mem, **over):
    base = dict(text="cohort summary", regime=None, tags=["risk_off"], importance=6,
                polarity="restrictive", state="candidate", source="mined", n_support=2,
                confirmations=0)
    base.update(over)
    return append_lesson(mem, base, ts=_NOW)


def _texts(mem, **kw):
    return [lz.text for lz in retrieve_lessons(mem, now=_NOW, regime="risk_off",
                                               query_tags=["risk_off"], **kw)]


def test_thin_mined_candidate_is_withheld_from_prompts(tmp_path):
    _add(tmp_path, text="thin mined", n_support=2, confirmations=0)
    assert "thin mined" not in _texts(tmp_path)              # below floor -> not injected


def test_mined_candidate_injects_once_it_has_support(tmp_path):
    _add(tmp_path, text="supported mined", n_support=4, confirmations=0)
    assert "supported mined" in _texts(tmp_path)             # n>=3 -> earns its way in


def test_mined_candidate_injects_once_it_recurs(tmp_path):
    _add(tmp_path, text="recurred mined", n_support=2, confirmations=1)
    assert "recurred mined" in _texts(tmp_path)              # >=1 recurrence -> in


def test_curated_thin_candidate_is_NOT_auto_muted(tmp_path):
    _add(tmp_path, text="curated thin", source="curated", n_support=0, confirmations=0)
    assert "curated thin" in _texts(tmp_path)                # hand/LLM-authored is trusted


def test_validated_always_injects_even_if_thin(tmp_path):
    _add(tmp_path, text="validated rule", state="validated", source="mined",
         n_support=0, confirmations=0)
    assert "validated rule" in _texts(tmp_path)


def test_format_tags_candidate_unproven_and_validated_rule():
    from futures_fund.lessons import Lesson
    cand = Lesson(ts=_NOW, text="press it", polarity="enabling", state="candidate",
                  n_support=4, confirmations=2)
    val = Lesson(ts=_NOW, text="do not short risk-on", polarity="restrictive", state="validated")
    fc, fv = format_lesson(cand), format_lesson(val)
    assert "CANDIDATE — unproven" in fc and "n=4" in fc and "conf=2" in fc and "press it" in fc
    assert fv.startswith("[RULE") and "do not short risk-on" in fv
