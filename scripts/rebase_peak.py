"""Re-base `peak_equity` onto the LIVE strategy era (NON-protected capability).

WHY. `peak_equity` is the reference for every drawdown brake — the -5% step-down (halves risk), the
-10% reduce-only, the -15% force-flatten — and for the health tier, where `caution` halves max_heat
and per-trade risk again. The desk was converted from the blended 3-leg book to the cross-sectional
factor book at cy358, but the standing peak was set at cy71 on 2026-07-02 by the SUPERSEDED blended
desk. The factor desk is therefore throttled for a drawdown it never took:

    peak $10,668 (blended) -> dd 7.57% -> caution + breaker x0.5 -> max_heat 0.020 -> gross 0.067x
    peak $10,017 (factor)  -> dd 1.55% -> healthy, no step-down  -> max_heat 0.040 -> gross 0.133x

That is a stale INPUT, not a weakened limit: -5/-10/-15% all still fire, measured against the
capital this strategy was actually handed. HARD RULE 5 forbids hand-editing `state/`, so this is a
capability with guardrails rather than an edit, and it is DRY-RUN unless `--apply` is passed.

THE GUARDRAILS ARE THE POINT. Lowering a high-water mark lowers the force-flatten floor, so this
must never become a way to walk away from a real drawdown:

  * only ever LOWERS the peak — raising one would fabricate a high-water mark never reached;
  * never below the era's STARTING equity, so a losing era cannot re-base its own losses away
    (that would be the martingale the breakers exist to stop);
  * never below current equity, which would imply a negative drawdown;
  * the era boundary is READ from the cycle reports' `desk` tag, never passed in — a future
    conversion moves it automatically and it cannot be aimed at an arbitrary cycle;
  * idempotent: once re-based there is nothing left to do.

Usage:
    uv run python scripts/rebase_peak.py                 # dry run, prints the plan
    uv run python scripts/rebase_peak.py --apply         # writes state/account.json
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re

_FLATTEN_FRAC = 0.15   # risk_gate's force-flatten, for reporting the safety cost only


def _cycle_no(path: str) -> int:
    m = re.search(r"[/\\](\d+)[/\\]", path)
    return int(m.group(1)) if m else -1


def era_start_cycle(state_dir: str) -> int | None:
    """First cycle whose report is tagged with the CURRENTLY live book engine.

    Read from the reports themselves so a future conversion moves this on its own, and so the
    boundary can never be aimed at a convenient cycle.
    """
    tagged: list[tuple[int, set[str]]] = []
    for p in glob.glob(os.path.join(state_dir, "cycle", "*", "cio.json")):
        n = _cycle_no(p)
        if n < 0:
            continue
        try:
            allocs = (json.load(open(p)) or {}).get("allocations") or []
        except Exception:  # noqa: BLE001 - a corrupt report must not decide risk posture
            continue
        desks = {a.get("desk") for a in allocs if isinstance(a, dict) and a.get("desk")}
        if desks:
            tagged.append((n, desks))
    if not tagged:
        return None
    tagged.sort()
    live = None
    for _, desks in reversed(tagged):
        live = sorted(desks)[0]
        break
    if live is None:
        return None
    # walk back while the tag still matches: the first cycle of the CURRENT contiguous era
    start = None
    for n, desks in reversed(tagged):
        if live in desks:
            start = n
        else:
            break
    return start


def _equity_rows(state_dir: str) -> list[dict]:
    p = os.path.join(state_dir, "equity-history.jsonl")
    rows = []
    try:
        for line in open(p):
            try:
                rows.append(json.loads(line))
            except Exception:  # noqa: BLE001
                continue
    except OSError:
        return []
    return rows


def era_peak(state_dir: str, start_cycle: int) -> float | None:
    eq = [float(r["equity"]) for r in _equity_rows(state_dir)
          if r.get("cycle") is not None and int(r["cycle"]) >= start_cycle
          and r.get("equity") is not None]
    return max(eq) if eq else None


def era_start_equity(state_dir: str, start_cycle: int) -> float | None:
    rows = [r for r in _equity_rows(state_dir)
            if r.get("cycle") is not None and int(r["cycle"]) >= start_cycle
            and r.get("equity") is not None]
    rows.sort(key=lambda r: int(r["cycle"]))
    return float(rows[0]["equity"]) if rows else None


def plan_rebase(state_dir: str) -> dict:
    """What a re-base WOULD do, with every guardrail applied. Never writes."""
    try:
        acct = json.load(open(os.path.join(state_dir, "account.json")))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"cannot read account.json ({type(exc).__name__})"}

    old_peak = float(acct.get("peak_equity") or 0.0)
    equity = float(acct.get("balance") or 0.0)

    start = era_start_cycle(state_dir)
    if start is None:
        return {"ok": False, "reason": "cannot identify the live strategy era from the cycle "
                                       "reports' desk tags — failing closed"}

    peak = era_peak(state_dir, start)
    started = era_start_equity(state_dir, start)
    if peak is None or started is None:
        return {"ok": False, "reason": f"no equity history at or after cy{start}"}

    # WHY A LOSING ERA CANNOT RE-BASE ITS LOSSES AWAY. The new reference is the era's own PEAK,
    # which is itself a high-water mark: it only ratchets up within the era and never follows
    # equity down. So a factor desk that drops 20% keeps the peak it made and gets flattened on
    # schedule — the protection is inherent in using a peak, not bolted on.
    #
    # (An earlier version also floored at the era's STARTING equity. That term was dead: era_peak
    # is the max over the same rows era_start_equity takes its first from, so peak >= started
    # always. Mutation testing caught it surviving — a guardrail that reads as protection but
    # cannot fire is worse than none, so it is gone rather than left as decoration.)
    #
    # `equity` IS a live floor: account.balance is realised cash and can exceed the last logged
    # mark-to-market equity, and a peak under current equity implies a negative drawdown.
    new_peak = max(peak, equity)

    if new_peak >= old_peak:
        return {"ok": False, "era_start_cycle": start, "old_peak": old_peak,
                "reason": f"nothing to do — a re-base may only LOWER the peak "
                          f"(era peak {new_peak:.2f} >= standing {old_peak:.2f})"}

    return {
        "ok": True,
        "era_start_cycle": start,
        "era_start_equity": started,
        "old_peak": old_peak,
        "new_peak": new_peak,
        "equity": equity,
        "old_drawdown": max(0.0, 1 - equity / old_peak) if old_peak > 0 else 0.0,
        "new_drawdown": max(0.0, 1 - equity / new_peak) if new_peak > 0 else 0.0,
        "old_flatten_at": old_peak * (1 - _FLATTEN_FRAC),
        "new_flatten_at": new_peak * (1 - _FLATTEN_FRAC),
    }


def apply_rebase(state_dir: str, plan: dict) -> None:
    """Write the new peak, preserving every other account field."""
    p = os.path.join(state_dir, "account.json")
    acct = json.load(open(p))
    acct["peak_equity"] = float(plan["new_peak"])
    json.dump(acct, open(p, "w"), indent=2)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="state")
    ap.add_argument("--apply", action="store_true",
                    help="write state/account.json (default: dry run)")
    args = ap.parse_args()

    plan = plan_rebase(args.state)
    print(json.dumps(plan, indent=2, default=str))
    if not plan.get("ok"):
        return
    print(f"\n  era starts cy{plan['era_start_cycle']} at ${plan['era_start_equity']:,.2f}")
    print(f"  peak  ${plan['old_peak']:,.2f} -> ${plan['new_peak']:,.2f}")
    print(f"  dd    {plan['old_drawdown']*100:.2f}% -> {plan['new_drawdown']*100:.2f}%")
    print(f"  SAFETY COST: force-flatten floor ${plan['old_flatten_at']:,.2f} -> "
          f"${plan['new_flatten_at']:,.2f} (moves DOWN by "
          f"${plan['old_flatten_at'] - plan['new_flatten_at']:,.2f})")
    if args.apply:
        apply_rebase(args.state, plan)
        print("\n  APPLIED to", os.path.join(args.state, "account.json"))
    else:
        print("\n  dry run — pass --apply to write")


if __name__ == "__main__":
    main()
