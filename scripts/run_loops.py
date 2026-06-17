"""Single-loop tick (deterministic): ONE writer (single-flight lock) -> strategic-due status. Safe
to run repeatedly (in-session /loop or a cron). TEMPEST-NEUTRAL runs a SINGLE 4h strategic loop —
there is no 15m fast loop. Exits (stop/TP/liq) are checked at the 4h strategic cycle's preflight
(cycle.audit_and_reflect on the 4h bar), which the Claude orchestrator dispatches when this prints
strategic.due=true. At ~1x no-leverage liquidation is effectively unreachable, so a 4h exit cadence
carries no gap-to-liq tail (the optimistic stop-level fill is the only 4h-cadence cost — see
tests/test_neutral_exit_gap.py).

    uv run python scripts/run_loops.py

Prints a JSON object: {strategic:{due,mode,cycle,action?}}.
"""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime


def main() -> int:
    from futures_fund.config import load_settings
    from futures_fund.runlock import single_flight
    from futures_fund.scheduling import cycle_due, tf_to_minutes
    settings = load_settings()
    state = "state"
    now = datetime.now(UTC)
    strat_tf = tf_to_minutes(settings.loops["strategic"].timeframe)
    out: dict = {"ts": now.isoformat()}

    with single_flight(state, now, owner="runner") as ok:
        if not ok:
            print(json.dumps({"locked": True, "msg": "another loop is running; stand down"}))
            return 0
        # STRATEGIC is the only loop, on the legacy state/cycle root (loop=None).
        smode, sn, _ = cycle_due(state, now, tf_minutes=strat_tf, loop=None)
        out["strategic"] = {"due": smode in ("FRESH", "RETRY"), "mode": smode, "cycle": sn}
        # Flag for the orchestrator: the LLM 4h cycle (scout/preflight -> momentum/carry/news desks
        # -> CIO -> Trader -> gate, which closes exits in preflight) runs OUTSIDE this deterministic
        # runner. The single 4h loop both rebalances the dollar-neutral book AND sweeps exits.
        if smode in ("FRESH", "RETRY"):
            out["strategic"]["action"] = (
                f"DUE cycle {sn}: run scout+preflight (sweeps 4h exits) then dispatch "
                "desks->CIO->Trader->gate")
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
