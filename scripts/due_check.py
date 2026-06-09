"""Multi-cadence due-gate CLI for the Operation TEMPEST-WEEKLY dual-loop.

Run as the FIRST action each poll fire. It decides whether the current candle (for a given loop)
still needs a cycle and prints ONE of:

    DUE FRESH <N>   -> run a brand-new cycle end-to-end; create <root>/<N>/
    DUE RETRY <N>   -> a prior dir crashed before the gate; re-run/OVERWRITE <root>/<N>/
    SKIP: <reason>  -> this candle is already served; exit quietly (the line is a liveness ping)
    ERROR: <reason> -> internal failure (exit code 2); do NOT trade, surface/notify

Exit code: 0 for DUE*/SKIP, 2 for ERROR. Makes ZERO exchange/network calls and ZERO writes.

    python scripts/due_check.py                    # legacy single-loop 4h gate (state/cycle)
    python scripts/due_check.py <state_dir>        # explicit state dir (testing)
    python scripts/due_check.py <dir> --loop fast       # 15m gate (state/fast/cycle)
    python scripts/due_check.py <dir> --loop strategic  # 1h gate (state/strategic/cycle)

With --loop, the loop's timeframe is read from config.yaml (loops.<name>.timeframe); override with
--tf <minutes>. The dual-loop runner invokes this once per loop and runs whichever loops are DUE,
strategic first, all under the single-flight run lock.
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime


def _loop_tf_minutes(loop: str, tf_override: int | None) -> int:
    if tf_override is not None:
        return tf_override
    from futures_fund.config import load_settings
    from futures_fund.scheduling import tf_to_minutes
    s = load_settings()
    ls = s.loops.get(loop)
    if ls is None:
        raise SystemExit(f"unknown loop {loop!r}; config defines {sorted(s.loops)}")
    return tf_to_minutes(ls.timeframe)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    loop = None
    tf_override = None
    # crude flag parse (no argparse dependency churn): pull --loop/--tf, leave state_dir positional
    rest: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--loop" and i + 1 < len(argv):
            loop = argv[i + 1]
            i += 2
            continue
        if a == "--tf" and i + 1 < len(argv):
            tf_override = int(argv[i + 1])
            i += 2
            continue
        rest.append(a)
        i += 1
    state_dir = rest[0] if rest else "state"

    try:
        from futures_fund.scheduling import cycle_due
        if loop is None:
            mode, n, reason = cycle_due(state_dir, datetime.now(UTC))  # legacy 4h, state/cycle
        else:
            tf_min = _loop_tf_minutes(loop, tf_override)
            # The STRATEGIC loop is the main cycle and lives on the legacy state/cycle root (where
            # gate_execute_cli writes its served-candle report); only the FAST loop gets its own
            # namespaced root (state/fast/cycle, written by scripts/fast_loop.py). Mapping the
            # strategic gate to loop=None keeps its served candle visible to this due-gate.
            root_loop = None if loop == "strategic" else loop
            mode, n, reason = cycle_due(state_dir, datetime.now(UTC),
                                        tf_minutes=tf_min, loop=root_loop)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 — the import/now itself failed; fail SAFE but visible
        print(f"ERROR: due_check failed before decision: {e!r}")
        return 2
    if mode in ("FRESH", "RETRY"):
        print(f"DUE {mode} {n}")
        print(reason)
        return 0
    print(f"SKIP: {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
