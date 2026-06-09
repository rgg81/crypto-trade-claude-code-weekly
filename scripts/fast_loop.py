"""Fast-loop entry (TEMPEST-WEEKLY, ~15m): single-flight lock -> due-gate -> exit sweep.

This is the safety-critical, ZERO-LLM core of the fast loop. The orchestrator runs it every poll;
it acquires the shared run lock (so it can never race the strategic loop on the book), gates on the
fast 15m candle, and — when due — sweeps every open position against the latest 15m bar, closing any
that hit stop/TP/liq. The Scalper desk that OPENS new scalps is dispatched by the orchestrator
playbook AFTER this sweep (LLM + gate_execute), within the same lock.

    uv run python scripts/fast_loop.py            # uses state/, memory/, now=UTC
    uv run python scripts/fast_loop.py <state> <memory>

Prints a machine-parseable first line: SWEPT <N> | SKIP: <reason> | LOCKED | ERROR: <reason>.
"""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    state_dir = argv[0] if argv else "state"
    memory_dir = argv[1] if len(argv) > 1 else "memory"
    now = datetime.now(UTC)
    try:
        from futures_fund.config import load_settings
        from futures_fund.runlock import single_flight
        from futures_fund.scheduling import cycle_due, tf_to_minutes
        settings = load_settings()
        tf = settings.loops["fast"].timeframe
        tf_min = tf_to_minutes(tf)
    except Exception as e:  # noqa: BLE001 — config/import failed; fail SAFE but visible
        print(f"ERROR: fast_loop setup failed: {e!r}")
        return 2

    with single_flight(state_dir, now, owner="fast") as acquired:
        if not acquired:
            print("LOCKED: another loop is running; stand down")
            return 0
        try:
            mode, n, reason = cycle_due(state_dir, now, tf_minutes=tf_min, loop="fast")
            if mode not in ("FRESH", "RETRY"):
                print(f"SKIP: {reason}")
                return 0
            from futures_fund.exchange import FuturesExchange
            from futures_fund.fast_loop import run_exit_sweep
            ex = FuturesExchange.from_settings(settings)
            report = run_exit_sweep(ex, settings, state_dir, memory_dir, now, n, tf=tf)
        except Exception as e:  # noqa: BLE001 — surface, do not trade on a broken sweep
            print(f"ERROR: fast sweep failed: {e!r}")
            return 2
    print(f"SWEPT {n}")
    print(json.dumps({"closed": report.get("closed", 0), "carried": report.get("carried", 0),
                      "equity": report.get("equity"), "alerts": report.get("alerts", []),
                      "should_halt": report.get("should_halt", False)}, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
