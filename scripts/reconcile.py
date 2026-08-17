"""Reconcile what the book builder SUBMITTED against what the gate actually OPENED.

Legs can disappear inside the protected risk path without any record. `consolidation.consolidate`
batch-scales the approved trades down to the gross-heat cap and then returns

    [t for t in trades if risk(t) >= min_risk_frac]

so a leg scaled below the dust risk floor is simply absent — the list gets shorter and nothing says
why. At cy290-292 that silently swallowed two short legs per cycle: the pre-sizer kept all 6
(`n_kept: 6`, `heat_dropped: []`) and the gate reported `dropped: 0 / vetoed: 0`, yet only 4 opened.
The book sat at L3/S1 for three cycles and the cause was invisible in every report.

The dust drop itself is a genuine safety behaviour in PROTECTED code and is not changed here. This
just makes the disappearance observable, so an operator (and the next cycle) can see it.
"""


def _symbols_from(entries) -> set[str]:
    """Symbols named in a drop/veto list, tolerating dicts or plain strings."""
    out: set[str] = set()
    for e in entries or []:
        if isinstance(e, dict):
            s = e.get("symbol")
            if s:
                out.add(str(s))
        elif isinstance(e, str):
            out.add(e.split(":")[0].strip())
    return out


def unexplained_opens(submitted, report) -> list[str]:
    """Submitted symbols that neither opened nor were accounted for as dropped/vetoed.

    Anything returned here vanished silently — the single most expensive failure mode to diagnose,
    because every report reads clean while the book is short a leg.
    """
    rep = report or {}
    opened = set()
    for a in rep.get("actions") or []:
        if isinstance(a, dict) and a.get("open"):
            opened.add(str(a["open"]))
    explained = opened | _symbols_from(rep.get("drop_reasons")) | _symbols_from(rep.get("vetoed"))
    return sorted({str(s) for s in submitted or []} - explained)
