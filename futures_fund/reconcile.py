"""Reconcile what the book builder SUBMITTED against what the gate actually OPENED.

Legs can disappear inside the protected risk path without any record. `consolidation.consolidate`
batch-scales the approved trades down to the gross-heat cap and then returns

    [t for t in trades if risk(t) >= min_risk_frac]

so a leg scaled below the dust risk floor is simply absent — the list gets shorter and nothing says
why. At cy290-293 that silently swallowed two short legs per cycle: the pre-sizer kept them all and
the gate reported `dropped: 0 / vetoed: 0`, yet fewer opened. The book sat at L3/S1 for four cycles
and the cause was invisible in every report until this canary named BTCUSDT and ETHUSDT.

The dust drop itself is a genuine safety behaviour in PROTECTED code and is not changed here. This
only makes the disappearance observable.
"""


def _symbols_from(entries) -> set[str]:
    """Symbols named in a drop/veto list, tolerating dicts, plain strings, and NON-LISTS.

    `report["vetoed"]` is an int COUNT in production (cycle.py: `report["vetoed"] = len(vetoed)`),
    not a list — the veto details go to the shadow book. Iterating it raised TypeError, which the
    caller's guard swallowed, so the canary went dark on every cycle where anything was vetoed.
    """
    out: set[str] = set()
    if isinstance(entries, (int, float)) or entries is None:
        return out
    try:
        iterator = iter(entries)
    except TypeError:
        return out
    for e in iterator:
        if isinstance(e, dict):
            s = e.get("symbol")
            if s:
                out.add(str(s))
        elif isinstance(e, str):
            out.add(e.split(":")[0].strip())
    return out


def _veto_count(report) -> int:
    """How many legs the gate vetoed, when only a count is available (no symbols)."""
    v = report.get("vetoed")
    if isinstance(v, bool):
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    try:
        return len(v)
    except TypeError:
        return 0


def unexplained_opens(submitted, report, held_by_symbol=None,
                      direction_by_symbol=None) -> list[str]:
    """Submitted symbols that neither opened nor were accounted for as dropped/vetoed.

    Anything returned here vanished silently — the most expensive failure mode to diagnose, because
    every report reads clean while the book is short a leg.

    Two sources of FALSE positives are excluded:

    * `vetoed` carries only a count, so a vetoed leg cannot be named. Rather than accuse a leg that
      may in fact be explained, an unattributable veto suppresses the report entirely — a canary
      that cries wolf gets ignored, which is worse than one that occasionally stays quiet.
    * A proposal on a symbol ALREADY HELD in the same direction is deliberately left untouched
      (`executor.reconcile`, and cycle.py's holdings-review `to_open` filter), so it never appears
      in `actions`. An opposite-direction re-proposal is a genuine flip and IS still reported.
    """
    rep = report or {}
    opened = set()
    for a in rep.get("actions") or []:
        if isinstance(a, dict) and a.get("open"):
            opened.add(str(a["open"]))

    named_vetoes = _symbols_from(rep.get("vetoed"))
    if not named_vetoes and _veto_count(rep) > 0:
        return []                                 # unattributable -> stay quiet rather than guess

    explained = opened | _symbols_from(rep.get("drop_reasons")) | named_vetoes
    held = held_by_symbol or {}
    want = direction_by_symbol or {}
    out = set()
    for s in submitted or []:
        s = str(s)
        if s in explained:
            continue
        if s in held and (s not in want or want[s] == held[s]):
            continue                        # same-direction re-proposal: a no-op by design
        out.add(s)
    return sorted(out)
