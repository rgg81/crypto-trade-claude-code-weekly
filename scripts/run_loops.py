"""Dual-loop tick (deterministic): ONE writer (single-flight lock) -> fast exit sweep + per-loop due
status. Safe to run repeatedly (in-session /loop or a cron). The STRATEGIC LLM cycle
(desks -> CIO -> Trader -> gate) is dispatched by the Claude orchestrator when this prints
strategic.due=true; this runner only does the deterministic, zero-LLM work.

    uv run python scripts/run_loops.py

Prints a JSON object: {strategic:{due,mode,cycle}, fast:{due,mode,cycle,swept?}}.
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
    state, memory = "state", "memory"
    now = datetime.now(UTC)
    fast_tf = tf_to_minutes(settings.loops["fast"].timeframe)
    strat_tf = tf_to_minutes(settings.loops["strategic"].timeframe)
    out: dict = {"ts": now.isoformat()}

    with single_flight(state, now, owner="runner") as ok:
        if not ok:
            print(json.dumps({"locked": True, "msg": "another loop is running; stand down"}))
            return 0
        # STRATEGIC is the main cycle on the legacy state/cycle root (loop=None); FAST is its own.
        smode, sn, _ = cycle_due(state, now, tf_minutes=strat_tf, loop=None)
        fmode, fn, _ = cycle_due(state, now, tf_minutes=fast_tf, loop="fast")
        out["strategic"] = {"due": smode in ("FRESH", "RETRY"), "mode": smode, "cycle": sn}
        out["fast"] = {"due": fmode in ("FRESH", "RETRY"), "mode": fmode, "cycle": fn}

        # FAST loop: the deterministic exit sweep + risk tripwire runs every fire it is due.
        if fmode in ("FRESH", "RETRY"):
            from futures_fund.exchange import FuturesExchange
            from futures_fund.fast_loop import run_exit_sweep
            ex = FuturesExchange.from_settings(settings)
            rep = run_exit_sweep(ex, settings, state, memory, now, fn)
            out["fast"]["swept"] = {
                "closed": rep.get("closed"), "carried": rep.get("carried"),
                "equity": rep.get("equity"), "alerts": rep.get("alerts"),
                "should_halt": rep.get("should_halt"),
            }
        # STRATEGIC loop: flag for the orchestrator (LLM dispatch happens outside this deterministic
        # runner — scout/preflight -> desks/CIO/Trader -> gate_execute_cli --loop strategic).
        if smode in ("FRESH", "RETRY"):
            out["strategic"]["action"] = (
                f"DUE cycle {sn}: run scout+preflight then dispatch desks->CIO->Trader->gate")
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
