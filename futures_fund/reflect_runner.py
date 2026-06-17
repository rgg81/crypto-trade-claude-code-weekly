"""Deterministic REFLECT RUNNER — the auto-on-close step that turns the attributed journal into a
growing, two-sided, statistically-gated lesson corpus. Designed to run ONCE per strategic cycle
(inside the single-flight lock), fail-safe at the call site.

A lesson's IDENTITY is its COHORT FINGERPRINT — (regime, desk, direction, polarity) — NOT its
rendered text. The miner's text embeds drifting stats (counts, means) that change every cycle as
new trades close; keying confirmation on text would never match twice and would flood the corpus
with near-duplicates. Keying on the fingerprint makes "this pattern recurred" robust: the standing
text is refreshed in place while the lesson's identity (and confirmation streak) persists.

Lifecycle per run:
  1. MINE two-sided candidates from the journal's recent per-cohort window (reflect_miner).
  2. For each candidate: if its fingerprint is NEW -> persist as a CANDIDATE; if it RECURS ->
     refresh the standing text/stats and CONFIRM once (DSR-gated). Since reflect runs once per
     cycle, confirmations count DISTINCT cycles; candidate->validated needs >=5 confirmations AND
     DSR>=0.95 (0.0 below 10 closed trades, structurally blocking thin-record overfit).
  3. DEMOTE a VALIDATED lesson whose cohort's RECENT window now mints the OPPOSITE polarity (a real
     regime change, not noise — the window is what makes this sustained-flip, not wobble) — anti-
     ossification; resets its confirmations so it must re-earn promotion.
"""
from __future__ import annotations

import json
from pathlib import Path

from futures_fund.cells import cell_dsr
from futures_fund.fingerprint import episode_fingerprint
from futures_fund.lessons import (
    demote_lesson,
    read_lessons,
    retire_lesson,
    statistically_promote,
    update_lesson,
)
from futures_fund.reflect import record_lesson, reflection_payload
from futures_fund.reflect_miner import mine_candidates

TTL_CYCLES = 48   # ~1 week of 4h strategic cycles a lesson may go un-reproduced before it ages out


def _cohort(tags: list[str]) -> tuple:
    """The (regime, desk, dir) identity of a cohort lesson, from its tags."""
    d = {t.split(":", 1)[0]: t.split(":", 1)[1] for t in tags if ":" in t}
    return (d.get("regime"), d.get("desk"), d.get("dir"))


def _resolve_dsr(memory_dir, cohort: tuple, override) -> float:
    """The DSR p-value gating THIS lesson's promotion. `override` (a float, used by tests + any
    caller that wants a single desk-wide value) wins; otherwise compute the PER-CELL DSR from the
    cohort's own return series (Phase 3) — 0.0 below 10 closed trades, so a cell-specific rule can't
    validate until its OWN cell is statistically proven, not merely the desk overall."""
    if override is not None:
        return override
    regime, desk, direction = cohort
    return cell_dsr(memory_dir, episode_fingerprint(regime, desk, direction))


def _expire_stale(memory_dir, cycle_no: int) -> int:
    """ASYMMETRIC TTL expiry (anti-ossification, must-fix #3). A lesson whose cohort hasn't
    re-produced it in > TTL_CYCLES is stale: a CANDIDATE is RETIRED (transient noise that never
    established); a VALIDATED ENABLING 'press' rule is DEMOTED (its edge may have decayed — make it
    re-prove). A VALIDATED RESTRICTIVE brake is NEVER expired on silence: its cohort going quiet is
    the rule SUCCEEDING (the desk correctly stopped taking the setup), not the edge decaying — and a
    lesson with no last_seen stamp (legacy/curated) is left alone. Returns the count aged out."""
    expired = 0
    for lz in read_lessons(memory_dir):
        if lz.state == "retired" or lz.last_seen_cycle < 0:
            continue
        if cycle_no - lz.last_seen_cycle <= TTL_CYCLES:
            continue
        if lz.state == "candidate":
            retire_lesson(memory_dir, lz.id)
            expired += 1
        elif lz.state == "validated" and lz.polarity == "enabling":
            demote_lesson(memory_dir, lz.id)
            expired += 1
    return expired


def _state_path(memory_dir) -> Path:
    return Path(memory_dir) / "lessons" / ".reflect_state.json"


def _last_reflect_cycle(memory_dir) -> int:
    try:
        return int(json.loads(_state_path(memory_dir).read_text()).get("last_reflect_cycle", -1))
    except Exception:  # noqa: BLE001 — missing/corrupt state -> treat as never reflected
        return -1


def _set_last_reflect_cycle(memory_dir, cycle_no: int) -> None:
    p = _state_path(memory_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"last_reflect_cycle": int(cycle_no)}))


def reflect_and_record(memory_dir, now, cycle_no: int, dsr_pvalue: float | None = None) -> dict:
    """Run one reflection pass. Returns {new, confirmed, demoted, expired, n_closed, total, valid}.

    DSR GATING: `dsr_pvalue=None` (default, live) gates each lesson on its OWN cell's DSR (Phase 3);
    pass a float to override with one value for all (tests / a desk-wide gate).

    CYCLE-IDEMPOTENT: confirmations count DISTINCT cycles, so the count-mutating steps (CONFIRM,
    DEMOTE, EXPIRE) run only when cycle_no advances past the last reflected cycle. A RETRY of the
    SAME cycle still MINTS brand-new candidates (minting is fingerprint-idempotent) but does NOT
    re-confirm, re-demote or re-expire — protecting the distinct-cycle invariant."""
    advanced = cycle_no > _last_reflect_cycle(memory_dir)
    payload = reflection_payload(memory_dir)
    candidates = mine_candidates(payload, now_cycle=cycle_no)

    # index the LIVE (non-retired) corpus by fingerprint = (cohort, polarity)
    by_fp = {(_cohort(lz.tags), lz.polarity): lz
             for lz in read_lessons(memory_dir) if lz.state != "retired"}

    new = confirmed = 0
    minted: dict[tuple, set] = {}                 # cohort -> {polarities minted THIS cycle}
    for c in candidates:
        cohort = _cohort(c["tags"])
        minted.setdefault(cohort, set()).add(c["polarity"])
        lz = by_fp.get((cohort, c["polarity"]))
        if lz is None:
            record_lesson(memory_dir, text=c["text"], regime=c.get("regime"),
                          tags=list(c["tags"]), importance=int(c.get("importance", 5)),
                          provenance=list(c.get("provenance") or []), ts=now,
                          polarity=c["polarity"], n_support=int(c.get("n_support", 0)),
                          source="mined", last_seen_cycle=cycle_no)
            new += 1
        else:
            # recurring pattern: refresh standing text/stats/ts AND last_seen_cycle, confirm once.
            # Refreshing keeps a recurring lesson fresh (recency + TTL); a pattern that STOPS
            # recurring ages out — softly via recency, then hard via the TTL sweep below.
            fields = {"text": c["text"], "ts": now, "n_support": int(c.get("n_support", 0)),
                      "provenance": list(c.get("provenance") or []),
                      "importance": int(c.get("importance", 5))}
            if advanced:
                fields["last_seen_cycle"] = cycle_no
            update_lesson(memory_dir, lz.id, **fields)
            if advanced:                          # confirm at most ONCE per distinct cycle
                statistically_promote(memory_dir, lz.id,
                                      dsr_pvalue=_resolve_dsr(memory_dir, cohort, dsr_pvalue))
                confirmed += 1

    # contradiction: a VALIDATED lesson whose cohort's recent window now mints the OPPOSITE polarity
    demoted = expired = 0
    if advanced:
        for lz in read_lessons(memory_dir):
            if lz.state != "validated":
                continue
            pols = minted.get(_cohort(lz.tags), set())
            opposite = "enabling" if lz.polarity == "restrictive" else "restrictive"
            if opposite in pols and lz.polarity not in pols:   # cohort flipped sign, sustained
                demote_lesson(memory_dir, lz.id)
                demoted += 1
        expired = _expire_stale(memory_dir, cycle_no)          # TTL staleness sweep
        _set_last_reflect_cycle(memory_dir, cycle_no)

    final = read_lessons(memory_dir)
    return {"new": new, "confirmed": confirmed, "demoted": demoted, "expired": expired,
            "n_closed": payload.get("n_closed", 0),
            "total": len(final), "validated": sum(1 for lz in final if lz.state == "validated")}
