"""Journal of FLAT / declined-setup verdicts.

The desk only ever journaled OPENED trades, so reflection could only mine a winners-vs-losers
contrast — structurally producing risk-reducing ('don't') lessons only. To learn whether standing
aside HELPS or COSTS, we must also persist the trades the desk DECLINED, flagged by whether they
matched its proven edge, then later evaluate how price actually moved. That closes the feedback
loop so the corpus can mint enabling ('DO take it when X') lessons too.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path


def _store(memory_dir) -> Path:
    return Path(memory_dir) / "flat-decisions.jsonl"


def append_flat_decision(memory_dir, fields: dict, ts: datetime) -> str:
    """Record a FLAT verdict. Expected fields: cycle, symbol, regime, rating, reason,
    edge_aligned (bool — did it match the crowded-short squeeze-long edge?), favored_side
    ('long'|'short' — the direction the passed-on setup leaned), mark (price at decision).
    Outcome fields (evaluated, favored_move_pct, flat_cost_us) are patched later."""
    data = {**fields, "ts": ts.isoformat() if hasattr(ts, "isoformat") else ts}
    data.setdefault("id", uuid.uuid4().hex)
    data.setdefault("evaluated", False)
    p = _store(memory_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as fh:
        fh.write(json.dumps(data, default=str) + "\n")
    return data["id"]


def read_flat_decisions(memory_dir) -> list[dict]:
    p = _store(memory_dir)
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text().splitlines():
        if not line.strip():
            continue
        try:                                  # one corrupt line must not crash the learning pass
            out.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            continue
    return out


def record_cycle_flat_verdicts(memory_dir, cycle_no: int, verdicts: list[dict],
                               now: datetime, *, regime: str | None = None,
                               marks: dict[str, float] | None = None) -> list[str]:
    """Bridge the CIO's `flat_verdicts` (declined edge-aligned setups, written inside cio.json) into
    the flat-decision journal so `evaluate_pending_flats` can later score whether standing aside
    COST the desk — the data source for ENABLING 'DO take it' lessons. IDEMPOTENT per (cycle,
    symbol) so a cycle RETRY never double-journals. Fills regime/mark from the cycle context when
    the CIO omitted them. Returns the ids actually appended."""
    marks = marks or {}
    seen = {(r.get("cycle"), r.get("symbol")) for r in read_flat_decisions(memory_dir)}
    ids: list[str] = []
    for v in verdicts or []:
        sym = v.get("symbol")
        if not sym or (cycle_no, sym) in seen:
            continue
        fields = {**v, "cycle": cycle_no}
        fields.setdefault("regime", regime)
        if fields.get("mark") is None and sym in marks:
            fields["mark"] = marks[sym]
        ids.append(append_flat_decision(memory_dir, fields, ts=now))
        seen.add((cycle_no, sym))
    return ids


def _write_all(memory_dir, rows: list[dict]) -> None:
    _store(memory_dir).write_text("".join(json.dumps(r, default=str) + "\n" for r in rows))


def patch_flat_outcome(memory_dir, fid: str, fields: dict) -> bool:
    rows = read_flat_decisions(memory_dir)
    hit = False
    for r in rows:
        if r.get("id") == fid:
            r.update(fields)
            hit = True
    if hit:
        _write_all(memory_dir, rows)
    return hit


def evaluate_pending_flats(memory_dir, marks: dict[str, float], now: datetime,
                           *, now_cycle: int | None = None, eval_after_cycles: int = 6,
                           min_move: float = 0.02) -> int:
    """Score un-evaluated, edge-aligned FLATs by how price moved in the setup's FAVORED direction —
    but over a MULTI-DAY horizon, not the next-candle bounce. Two mechanics fix the short-horizon
    artifact that kept 'vindicating' the holds on 1-cycle noise:

    1. HORIZON GATING — a decision FINALIZES (`evaluated=True`) only once it is `eval_after_cycles`
       cycles old (≈24h on the 4h cadence, a multi-day window). Before that it stays pending, so a
       single-candle bounce can never lock the verdict. (Falls back to immediate single-shot eval
       when `now_cycle` is unknown — preserves the legacy call/tests.)
    2. MAX FAVORABLE EXCURSION — while pending, each call advances a running `max_favored_move`, so
       a declined trade that trends our way then ROUND-TRIPS still registers the move it would have
       captured (the desk's trades carry take-profits; they don't sit through a full round-trip).
       `favored_move_pct` (and `flat_cost_us`) use that peak; `endpoint_move_pct` keeps the last
       mark for transparency.

    `flat_cost_us` = the peak favorable excursion over the window >= min_move (standing aside cost
    us). Only edge-aligned flats are evaluated. Returns the number NEWLY FINALIZED this call."""
    rows = read_flat_decisions(memory_dir)
    n = 0
    dirty = False
    for r in rows:
        if r.get("evaluated") or not r.get("edge_aligned"):
            continue
        m0, sym = r.get("mark"), r.get("symbol")
        m1 = marks.get(sym)
        if not m0 or not m1:
            continue
        side = r.get("favored_side", "long")
        move = (m1 - m0) / m0 * (1.0 if side == "long" else -1.0)
        prev_max = r.get("max_favored_move")
        max_move = move if prev_max is None else max(prev_max, move)
        dcyc = r.get("cycle")
        ready = now_cycle is None or dcyc is None or (now_cycle - dcyc) >= eval_after_cycles
        if ready:
            r.update({"evaluated": True, "eval_mark": m1,
                      "eval_ts": now.isoformat() if hasattr(now, "isoformat") else now,
                      "max_favored_move": max_move, "endpoint_move_pct": move,
                      "favored_move_pct": max_move, "flat_cost_us": max_move >= min_move})
            n += 1
            dirty = True
        elif max_move != prev_max:           # still pending — just advance the running peak
            r["max_favored_move"] = max_move
            dirty = True
    if dirty:
        _write_all(memory_dir, rows)
    return n
