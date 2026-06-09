"""Single-flight run-lock CLI for the STRATEGIC loop (and any multi-step orchestrated cycle).

The fast loop holds the lock inside one process (scripts/fast_loop.py). The strategic loop is
orchestrated by Claude across MANY separate CLI/dispatch processes, so it acquires the lock at the
START of the cycle and releases it at the END — guaranteeing exactly one writer while a strategic
cycle runs (a concurrent fast fire will see LOCKED and stand down). A crashed cycle that never
releases is auto-reclaimed after the stale window (runlock.DEFAULT_STALE_AFTER_S, 30 min).

    uv run python scripts/runlock_cli.py acquire --owner strategic   # ACQUIRED | LOCKED: <holder>
    uv run python scripts/runlock_cli.py release                     # RELEASED
    uv run python scripts/runlock_cli.py status                      # FREE | HELD: <holder>

`acquire` exits 0 on ACQUIRED, 0 on LOCKED (caller stands down — not an error), 2 on internal error.
"""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in ("acquire", "release", "status"):
        print("usage: runlock_cli.py acquire|release|status [--owner NAME] [--state DIR]")
        return 2
    action = argv[0]
    owner = "strategic"
    state_dir = "state"
    i = 1
    while i < len(argv):
        if argv[i] == "--owner" and i + 1 < len(argv):
            owner = argv[i + 1]
            i += 2
        elif argv[i] == "--state" and i + 1 < len(argv):
            state_dir = argv[i + 1]
            i += 2
        else:
            i += 1
    try:
        from futures_fund import runlock
        now = datetime.now(UTC)
        if action == "acquire":
            ok, holder = runlock.try_acquire(state_dir, now, owner=owner)
            if ok:
                print("ACQUIRED")
            else:
                print(f"LOCKED: {json.dumps(holder)}")
            return 0
        if action == "release":
            runlock.release(state_dir)
            print("RELEASED")
            return 0
        # status
        from pathlib import Path
        p = Path(state_dir) / runlock.LOCK_NAME
        holder = runlock._read(p) if p.exists() else None
        print(f"HELD: {json.dumps(holder)}" if holder else "FREE")
        return 0
    except Exception as e:  # noqa: BLE001 — surface, never crash the orchestrator silently
        print(f"ERROR: runlock {action} failed: {e!r}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
