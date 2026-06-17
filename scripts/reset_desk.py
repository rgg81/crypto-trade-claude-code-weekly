"""Reset the PAPER desk to a clean starting book.

ARCHIVES the current runtime state to state/archive/reset_<ts>/ (recoverable — nothing is deleted),
then re-initializes a FLAT account at account_size_usdt: no positions, no armed triggers, no cycle
history, fresh equity/regime/news history. Keeps ALL code, agents, SKILL, config, and the lessons
corpus + memory — only the trading RUNTIME state under state/ is reset. PAPER-ONLY: refuses if live.

    uv run python scripts/reset_desk.py            # DRY RUN — shows what it would do
    uv run python scripts/reset_desk.py --confirm  # perform the reset
"""
from __future__ import annotations

import argparse
import shutil
from datetime import UTC, datetime
from pathlib import Path

from futures_fund.config import load_settings
from futures_fund.pending_orders import save_pending_orders
from futures_fund.state import AccountState, save_account, save_positions

# Runtime trading state under state/ that a reset wipes — everything EXCEPT the archive dir itself
# (the lessons corpus + memory live OUTSIDE state/ and are deliberately kept).
RESET_ENTRIES = [
    "account.json", "positions.json", "pending_orders.json",
    "equity-history.jsonl", "regime_history.jsonl", "news_shock.json",
    "notifications.jsonl", ".run.lock",
    "cycle", "fast", "strategic",
]


# Trade-derived LEARNING corpus under memory/ that a STRATEGY pivot wipes (it is mis-contextualized
# for a new strategy). The META repair-journal (orchestrator code-fix audit) is KEPT — it is
# desk-agnostic, not a trade lesson.
LEARNING_ENTRIES = [
    "episodic",              # the closed-decision journal the reflector mines
    "flat-decisions.jsonl",  # flat-verdict journal (enabling-lesson source)
    "lessons",               # the mined lessons corpus + reflect state
    "hitrate",               # per-agent hit rates from the old strategy's trades
    "semantic", "procedural",  # learned beliefs/playbook (strategy-specific)
]


def do_reset_learning(memory_dir, archive_ts: str) -> dict:
    """Archive the trade-derived learning corpus so a pivoted strategy learns its OWN edge from a
    clean slate. Nothing is deleted — entries move to memory/archive/learning_<ts>/ (recoverable).
    The reflector/lesson machinery is untouched; only the accumulated history is cleared."""
    mem = Path(memory_dir)
    present = [e for e in LEARNING_ENTRIES if (mem / e).exists()]
    archive = mem / "archive" / f"learning_{archive_ts}"
    archive.mkdir(parents=True, exist_ok=True)
    for e in present:
        shutil.move(str(mem / e), str(archive / e))
    return {"archived": present, "archive_dir": str(archive)}


def do_reset(state_dir, start_balance: float, archive_ts: str) -> dict:
    """Archive the present runtime entries, then write a clean FLAT book. Returns a summary dict.
    Nothing is deleted — archived entries move under state/archive/reset_<ts>/ (recoverable)."""
    state = Path(state_dir)
    present = [e for e in RESET_ENTRIES if (state / e).exists()]
    archive = state / "archive" / f"reset_{archive_ts}"
    archive.mkdir(parents=True, exist_ok=True)
    for e in present:
        shutil.move(str(state / e), str(archive / e))
    # re-initialize a clean PAPER book (mirrors the engine's first-run defaults)
    save_account(state, AccountState(balance=start_balance, peak_equity=start_balance, halt=False))
    save_positions(state, [])
    save_pending_orders(state, [])
    return {"archived": present, "archive_dir": str(archive), "balance": start_balance}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="state")
    ap.add_argument("--memory", default="memory")
    ap.add_argument("--confirm", action="store_true", help="actually perform the reset")
    ap.add_argument("--reset-learning", action="store_true",
                    help="ALSO archive the trade-derived learning corpus for a STRATEGY pivot "
                         "(the new strategy learns its own edge; the repair-journal is kept)")
    args = ap.parse_args()

    settings = load_settings()
    if settings.live:
        raise SystemExit("REFUSED: live is true — reset is PAPER-ONLY")

    state = Path(args.state)
    start_balance = settings.account_size_usdt
    present = [e for e in RESET_ENTRIES if (state / e).exists()]
    learn_present = ([e for e in LEARNING_ENTRIES if (Path(args.memory) / e).exists()]
                     if args.reset_learning else [])

    if not args.confirm:
        print(f"DRY RUN — would archive + reset these under {state}/ :")
        for e in present:
            print("  -", e)
        print(f"then re-init a FLAT account at {start_balance:.2f} (0 positions, 0 triggers).")
        if args.reset_learning:
            print(f"AND archive the trade-derived learning corpus under {args.memory}/ :")
            for e in learn_present:
                print("  -", e)
            print("  (the repair-journal is KEPT; the reflector/lesson machinery is untouched)")
        else:
            print("Lessons corpus + all code/agents/config are KEPT. Re-run with --confirm.")
        return

    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    res = do_reset(state, start_balance, ts)
    print(f"RESET complete. Archived {len(res['archived'])} entries -> {res['archive_dir']}")
    if args.reset_learning:
        lres = do_reset_learning(args.memory, ts)
        print(f"LEARNING reset. Archived {len(lres['archived'])} corpus entries -> "
              f"{lres['archive_dir']} (the desk now learns its own edge from a clean slate).")
    print(f"Fresh book: balance {start_balance:.2f}, equity {start_balance:.2f}, "
          f"0 positions, 0 triggers, halt False. Next cycle starts cold at cycle 1.")


if __name__ == "__main__":
    main()
