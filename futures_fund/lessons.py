from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

LessonState = Literal["candidate", "validated", "retired"]
# A lesson's directional pull on the desk. 'restrictive' = a brake (do NOT / cut / avoid);
# 'enabling' = an accelerator (DO take / size the trade when X); 'process' = neutral discipline
# (journal a falsifiable prediction, etc.). Used by the retrieval quota so a losing record can't
# flood every debate with prohibitions and talk the desk out of its own edge.
Polarity = Literal["restrictive", "enabling", "process"]


class Lesson(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    ts: datetime
    text: str
    regime: str | None = None         # quadrant it applies to; None = applies in all regimes
    symbol: str | None = None
    tags: list[str] = Field(default_factory=list)
    importance: int = 5               # 1-10
    polarity: Polarity = "restrictive"  # legacy lessons (no field) default to the dominant type
    state: LessonState = "candidate"
    confirmations: int = 0               # at most ONCE per cycle => counts DISTINCT cycles
    n_support: int = 0                   # cohort sample size behind the candidate (read-gate floor)
    last_seen_cycle: int = -1            # last cycle the cohort re-produced it (TTL expiry)
    source: Literal["mined", "curated"] = "curated"  # 'mined' = auto cohort summary (read-gated);
    #                                      'curated' = hand/LLM-authored (legacy default, NOT gated)
    provenance: list[str] = Field(default_factory=list)  # journal decision ids


def _store(memory_dir) -> Path:
    return Path(memory_dir) / "lessons" / "lessons.jsonl"


def append_lesson(memory_dir, fields: dict, ts: datetime) -> str:
    data = {**fields, "ts": ts}
    data.setdefault("id", uuid.uuid4().hex)
    lesson = Lesson.model_validate(data)
    p = _store(memory_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as fh:
        fh.write(lesson.model_dump_json() + "\n")
    return lesson.id


def read_lessons(memory_dir) -> list[Lesson]:
    p = _store(memory_dir)
    if not p.exists():
        return []
    out: list[Lesson] = []
    for line in p.read_text().splitlines():
        if not line.strip():
            continue
        try:                          # a malformed/invalid lesson line is skipped, not raised
            out.append(Lesson.model_validate_json(line))
        except Exception:  # noqa: BLE001 — corpus read must never crash the cycle over one bad line
            continue
    return out


def score_lesson(lesson: Lesson, now: datetime, query_tags: list[str],
                 w_rec: float = 1.0, w_imp: float = 1.0, w_rel: float = 1.0) -> float:
    """Generative-Agents-style score: recency (Ebbinghaus) + importance + tag relevance (Jaccard).
    """
    hours = max(0.0, (now - lesson.ts).total_seconds() / 3600.0)
    recency = 0.995 ** hours
    importance = lesson.importance / 10.0
    qt, lt = set(query_tags), set(lesson.tags)
    relevance = len(qt & lt) / len(qt | lt) if (qt or lt) else 0.0
    return w_rec * recency + w_imp * importance + w_rel * relevance


READ_GATE_MIN_N = 3          # an auto-MINED candidate needs >=3 cohort samples OR >=1 recurrence
READ_GATE_MIN_CONFIRM = 1    # before it may reach an agent prompt (adversarial must-fix #1)


def _injectable(lz: Lesson) -> bool:
    """The READ-PATH gate. VALIDATED standing rules always inject. A CURATED/legacy candidate is
    trusted (hand/LLM-authored, never auto-muted). An auto-MINED candidate is a raw cohort summary
    and must EARN its way to a prompt: >=READ_GATE_MIN_N samples OR >=1 recurrence — otherwise a
    single thin pattern would advise the desk ungated (the very flow must-fix #1 closes)."""
    if lz.state == "validated":
        return True
    if lz.source != "mined":
        return True
    return lz.n_support >= READ_GATE_MIN_N or lz.confirmations >= READ_GATE_MIN_CONFIRM


def format_lesson(lz: Lesson) -> str:
    """Render a lesson for an agent prompt with an HONEST confidence tag so the desk weights it
    correctly: a VALIDATED lesson reads as a standing RULE; a candidate reads as an UNPROVEN prior
    (with its sample size + recurrence count). The deterministic gate NEVER reads these — they shape
    agent JUDGMENT only."""
    if lz.state == "validated":
        return f"[RULE · {lz.polarity}] {lz.text}"
    # conf is a RECURRENCE count, NOT a significance measure — a thin cell can recur many times
    # without ever clearing the per-cell DSR. Say so, so a high conf can't be misread as a proven
    # edge (only a [RULE] is statistically proven).
    return (f"[CANDIDATE — unproven (n={lz.n_support}, conf={lz.confirmations} recurrences; "
            f"NOT a proven rule) · {lz.polarity}] {lz.text}")


def retrieve_lessons(memory_dir, now: datetime, regime: str | None,
                     query_tags: list[str], k: int = 5,
                     max_restrictive: int = 3) -> list[Lesson]:
    """Regime-filter FIRST (a lesson with regime=None applies everywhere), rank by score, then
    apply a POLARITY QUOTA so the injected set is two-sided: VALIDATED lessons (standing rules)
    are always kept; >=1 enabling lesson is force-included when any exists; and restrictive
    *fills* are capped at `max_restrictive` so a losing record's prohibitions can't monopolize
    every debate. Retired lessons excluded.

    NOTE: passing regime=None as the QUERY matches only universal (lz.regime is None) lessons,
    NOT all lessons; pass a regime string to also include lessons tagged to that regime."""
    candidates = [
        lz for lz in read_lessons(memory_dir)
        if lz.state != "retired" and (lz.regime is None or lz.regime == regime) and _injectable(lz)
    ]
    candidates.sort(key=lambda lz: score_lesson(lz, now, query_tags), reverse=True)

    validated = [lz for lz in candidates if lz.state == "validated"]
    pool = [lz for lz in candidates if lz.state != "validated"]
    out: list[Lesson] = list(validated)  # standing rules are never dropped by the quota

    # Force-include the highest-scored enabling lesson if none is in the set yet.
    if len(out) < k and not any(lz.polarity == "enabling" for lz in out):
        enabling = next((lz for lz in pool if lz.polarity == "enabling"), None)
        if enabling is not None:
            out.append(enabling)

    # Fill the remaining slots by score, capping restrictive FILLS (validated already counted).
    n_restrict = 0
    for lz in pool:
        if lz in out:
            continue
        if len(out) >= k:
            break
        if lz.polarity == "restrictive" and n_restrict >= max_restrictive:
            continue  # don't flood the debate with prohibitions
        out.append(lz)
        if lz.polarity == "restrictive":
            n_restrict += 1

    out.sort(key=lambda lz: score_lesson(lz, now, query_tags), reverse=True)
    return out[:max(k, len(validated))]  # never truncate away a validated standing rule


def _write_all(memory_dir, lessons: list[Lesson]) -> None:
    p = _store(memory_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(lz.model_dump_json() + "\n" for lz in lessons))


def update_lesson(memory_dir, lesson_id: str, **fields) -> bool:
    """Merge `fields` into the lesson with `lesson_id`; rewrites the store. False if not found."""
    lessons = read_lessons(memory_dir)
    hit = False
    for i, lz in enumerate(lessons):
        if lz.id == lesson_id:
            lessons[i] = lz.model_copy(update=fields)
            hit = True
    if hit:
        _write_all(memory_dir, lessons)
    return hit


def confirm_lesson(memory_dir, lesson_id: str, *, promote_threshold: int = 5) -> bool:
    """Increment a lesson's confirmation count; promote CANDIDATE -> VALIDATED at the threshold.
    (Count-based here; Phase C gates promotion additionally on statistical support — spec §6.)"""
    lessons = read_lessons(memory_dir)
    hit = False
    for i, lz in enumerate(lessons):
        if lz.id == lesson_id:
            c = lz.confirmations + 1
            state = (
                "validated"
                if (lz.state == "candidate" and c >= promote_threshold)
                else lz.state
            )
            lessons[i] = lz.model_copy(update={"confirmations": c, "state": state})
            hit = True
    if hit:
        _write_all(memory_dir, lessons)
    return hit


def demote_lesson(memory_dir, lesson_id: str) -> bool:
    """Step a lesson down: VALIDATED -> CANDIDATE, CANDIDATE/RETIRED -> RETIRED.
    Used to aggressively age out stale or regime-mismatched rules (spec §6).
    Resets confirmations to 0 so a demoted lesson must re-earn promotion
    (anti-ossification, spec §6)."""
    lessons = read_lessons(memory_dir)
    hit = False
    for i, lz in enumerate(lessons):
        if lz.id == lesson_id:
            new = "candidate" if lz.state == "validated" else "retired"
            lessons[i] = lz.model_copy(update={"state": new, "confirmations": 0})
            hit = True
    if hit:
        _write_all(memory_dir, lessons)
    return hit


def retire_lesson(memory_dir, lesson_id: str) -> bool:
    return update_lesson(memory_dir, lesson_id, state="retired")


def validated_lessons(memory_dir) -> list[Lesson]:
    """The VALIDATED lessons — high-confidence ADVISORY standing rules that shape agent JUDGMENT.
    They are NOT hard vetoes and NEVER gate execution: the deterministic gate is the sole risk
    authority and does not read lessons. A validated 'do not short risk-on' rule strongly biases
    the desk away from that trade; the gate still owns sizing/leverage/RR/breakers regardless."""
    return [lz for lz in read_lessons(memory_dir) if lz.state == "validated"]


def statistically_promote(memory_dir, lesson_id: str, *, dsr_pvalue: float,
                          promote_threshold: int = 5, dsr_threshold: float = 0.95) -> bool:
    """Confirm a lesson, but only allow CANDIDATE->VALIDATED promotion when the desk's edge is
    statistically proven (DSR p-value >= threshold). Below the gate the confirmation still
    counts, but the lesson stays CANDIDATE — the statistical layer over B3's count-based rule
    (spec §6). Returns True if the lesson was found."""
    lessons = read_lessons(memory_dir)
    hit = False
    for i, lz in enumerate(lessons):
        if lz.id == lesson_id:
            c = lz.confirmations + 1
            promote = (lz.state == "candidate" and c >= promote_threshold
                       and dsr_pvalue >= dsr_threshold)
            lessons[i] = lz.model_copy(update={"confirmations": c,
                                               "state": "validated" if promote else lz.state})
            hit = True
    if hit:
        _write_all(memory_dir, lessons)
    return hit
