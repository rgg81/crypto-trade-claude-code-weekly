"""Decision/outcome ATTRIBUTION — stamps the journal join-keys the learner needs to slice
experience by (desk x regime x close_reason -> r_multiple).

The deterministic gate's own close-patch (cycle.py) records realized_pnl but NOT the desk that
proposed the trade, the regime it was taken in, WHY it closed, or its R-multiple. Those three
join-keys are computed-then-discarded at gate time. Rather than edit the PROTECTED cycle.py, this
module re-patches the journal AFTER the gate runs (called fail-safe from the strategic cycle).
It only ADDS attribution metadata — it touches no limit/breaker/sizing path (HARD RULE 1).
"""
from __future__ import annotations

from futures_fund.journal import patch_outcome, read_all_decisions


def _missing(rec: dict, key: str) -> bool:
    v = rec.get(key)
    return v is None or v == ""


def _r_multiple(rec: dict) -> float | None:
    """R earned vs ORIGINAL journaled risk = realized_pnl / (|entry-stop| * size), using the
    record's OPEN-time entry/stop/size (never a trailed stop), per the r_progress convention."""
    try:
        entry, stop, size, pnl = (rec.get("entry"), rec.get("stop"),
                                  rec.get("size"), rec.get("realized_pnl"))
        if None in (entry, stop, size, pnl):
            return None
        risk = abs(float(entry) - float(stop)) * float(size)
        return float(pnl) / risk if risk > 0 else None
    except (TypeError, ValueError):
        return None


def stamp_cycle_attribution(memory_dir, cycle_no: int, report: dict, regime_label,
                            desk_by_symbol, now) -> int:
    """Backfill the attribution join-keys onto the journal AFTER the gate runs, WITHOUT touching a
    protected module. Idempotent (only fills absent fields). Returns the number of records stamped.

    OPEN-time keys on THIS cycle's opens: `desk` (from the CIO allocations) + `regime` (the cycle's
    final regime), where absent. CLOSE-time keys on this cycle's closes: `close_reason`
    (stop|tp|liq|holdings_close|reconcile|force_flatten, read from the gate report's `actions`) +
    `r_multiple` (vs ORIGINAL risk) — stamped on the MOST-RECENT closed decision per symbol that
    still lacks a close_reason, so an OLDER close of the same symbol is never mislabeled with the
    current cycle's reason.
    """
    desk_by_symbol = desk_by_symbol or {}
    close_reason: dict[str, str] = {}
    for a in (report or {}).get("actions") or []:
        # report['actions'] is a MIXED list: close/open records are dicts
        # ({"close": sym, "reason": ..., "pnl": ...}), but warnings (e.g. stale-trigger
        # auto-cancels) are plain STRINGS — skip anything that isn't a close dict.
        if not isinstance(a, dict):
            continue
        sym, reason = a.get("close"), a.get("reason")
        if sym and reason:
            close_reason[sym] = reason  # last reported close of a symbol this cycle wins

    recs = read_all_decisions(memory_dir)

    # the single most-recent UNSTAMPED close per reported symbol (don't backfill an old close)
    latest: dict[str, tuple] = {}
    for r in recs:
        sym = r.get("symbol")
        if (r.get("realized_pnl") is not None and sym in close_reason
                and _missing(r, "close_reason")):
            key = (str(r.get("exit_ts") or ""), int(r.get("cycle") or 0))
            if sym not in latest or key > latest[sym][0]:
                latest[sym] = (key, r.get("id"))
    close_targets = {v[1] for v in latest.values()}

    n = 0
    for r in recs:
        did = r.get("id")
        if not did:
            continue
        patch: dict = {}
        if r.get("cycle") == cycle_no:                       # OPEN-time keys
            desk = desk_by_symbol.get(r.get("symbol"))
            if desk and _missing(r, "desk"):
                patch["desk"] = desk
            if regime_label and _missing(r, "regime"):
                patch["regime"] = regime_label
        # CLOSE-time r_multiple is derivable from the record ALONE -> backfill on ANY closed-but-
        # unstamped decision, INCLUDING positions closed by the FAST exit sweep (which runs no
        # attribution pass and never appears in a strategic report's `actions`). Without this a
        # fast-loop stop-out is INVISIBLE to the miner — the cohort never learns from it.
        if r.get("realized_pnl") is not None and _missing(r, "r_multiple"):
            rm = _r_multiple(r)
            if rm is not None:
                patch["r_multiple"] = rm
        if did in close_targets:                       # close_reason needs THIS cycle's report
            patch["close_reason"] = close_reason[r["symbol"]]
        if patch:
            patch_outcome(memory_dir, did, patch)
            n += 1
    return n
