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
    ap.add_argument("--confirm", action="store_true", help="actually perform the reset")
    args = ap.parse_args()

    settings = load_settings()
    if settings.live:
        raise SystemExit("REFUSED: live is true — reset is PAPER-ONLY")

    state = Path(args.state)
    start_balance = settings.account_size_usdt
    present = [e for e in RESET_ENTRIES if (state / e).exists()]

    if not args.confirm:
        print(f"DRY RUN — would archive + reset these under {state}/ :")
        for e in present:
            print("  -", e)
        print(f"then re-init a FLAT account at {start_balance:.2f} (0 positions, 0 triggers).")
        print("Lessons corpus + memory + all code/agents/config are KEPT. Re-run with --confirm.")
        return

    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    res = do_reset(state, start_balance, ts)
    print(f"RESET complete. Archived {len(res['archived'])} entries -> {res['archive_dir']}")
    print(f"Fresh book: balance {start_balance:.2f}, equity {start_balance:.2f}, "
          f"0 positions, 0 triggers, halt False. Next cycle starts cold at cycle 1.")


if __name__ == "__main__":
    main()
