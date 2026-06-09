"""Fast-loop exit sweep (TEMPEST-WEEKLY, ~15m cadence).

The aggressive desk runs scalps at 10x; a 4h exit check is useless for them. This module is the
fast loop's first, deterministic, ZERO-LLM phase: every ~15m it re-checks EVERY open position
(scalps AND strategic swings — one shared book) against the latest FAST bar (the live last row —
exits intentionally use the forming bar's high/low) and closes any that hit their stop / take-profit
/ liquidation, with correct fees + funding + slippage. It
reuses the PROTECTED, already-tested `cycle.audit_and_reflect` + `exits.detect_exit` — only the
candle timeframe and the cycle root differ from the strategic loop.

It does NOT open new trades (that is the Scalper desk, wired by the runner after this sweep) and it
does NOT append to the canonical strategic-cadence equity-history (that would corrupt the NAV
series / Sharpe). It persists the book (account + positions), runs the liquidation-proximity / hard-
drawdown monitor tripwire, and writes state/fast/cycle/<N>/report.json so the multi-cadence due-gate
records the served 15m candle.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from futures_fund.config import Settings
from futures_fund.cycle import CycleContext, audit_and_reflect
from futures_fund.monitor import check_positions
from futures_fund.portfolio import portfolio_health
from futures_fund.scheduling import floor_tf, tf_to_minutes
from futures_fund.state import (
    load_account,
    load_positions,
    save_account,
    save_positions,
)


def _is_halted(state_dir) -> bool:
    p = Path(state_dir) / "account.json"
    if not p.exists():
        return False
    try:
        return bool(json.loads(p.read_text()).get("halt", False))
    except (json.JSONDecodeError, OSError):
        return False


def _held_context(exchange, settings: Settings, positions, tf: str) -> tuple[CycleContext, dict]:
    """Build a CycleContext covering the held positions' symbols at timeframe `tf`. Also returns a
    complete raw->mark price map (last fast close where available, else the position entry) so
    equity never KeyErrors on an unmapped/dataless holding."""
    frames, fundings, specs, raw_to_unified, specs_by_raw, prices = {}, {}, {}, {}, {}, {}
    entry_by_raw = {p.symbol: p.entry for p in positions}
    for raw in sorted({p.symbol for p in positions}):
        unified = exchange.unified_for_raw(raw)
        if unified is None:
            continue
        try:
            df = exchange.ohlcv(unified, tf)
            fundings[unified] = exchange.funding(unified)
            spec = exchange.symbol_spec(unified)
        except Exception:  # noqa: BLE001 — a dataless holding is carried, never crashes the sweep
            continue
        frames[unified] = df
        specs[unified] = spec
        raw_to_unified[spec.symbol] = unified
        specs_by_raw[spec.symbol] = spec
        prices[spec.symbol] = float(df["close"].iloc[-1])
    # complete the price map for every held position (fallback to entry -> zero unrealized)
    for raw, entry in entry_by_raw.items():
        prices.setdefault(raw, entry)
    ctx = CycleContext(settings, frames, fundings, specs, raw_to_unified, specs_by_raw, prices)
    return ctx, prices


def _write_report(state_dir, cycle_no: int, report: dict) -> None:
    d = Path(state_dir) / "fast" / "cycle" / str(cycle_no)
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.json").write_text(json.dumps(report, default=str))


def run_exit_sweep(exchange, settings: Settings, state_dir, memory_dir,
                   now: datetime, cycle_no: int, *, tf: str | None = None) -> dict:
    """Run one fast-loop exit sweep. Closes any held position whose latest fast candle hit
    stop/TP/liq; persists the book; runs the monitor tripwire; writes the fast-cycle report."""
    from futures_fund.memory_layout import ensure_memory_layout
    ensure_memory_layout(memory_dir)
    tf = tf or settings.loops["fast"].timeframe
    account = load_account(state_dir, settings.account_size_usdt)
    positions = load_positions(state_dir)
    candle = floor_tf(now, tf_to_minutes(tf)).isoformat()
    report: dict = {"cycle": cycle_no, "loop": "fast", "candle": candle, "ran_at": now.isoformat(),
                    "halted": False, "opened": 0, "closed": 0, "carried": 0,
                    "equity": account.balance, "actions": [], "alerts": []}

    if _is_halted(state_dir):
        report["halted"] = True
        _write_report(state_dir, cycle_no, report)
        return report

    if not positions:
        _write_report(state_dir, cycle_no, report)  # nothing open -> nothing to sweep
        return report

    ctx, prices = _held_context(exchange, settings, positions, tf)
    positions = audit_and_reflect(ctx, positions, account, memory_dir, now, report)

    health = portfolio_health(account.balance, account.peak_equity, positions, prices)
    account.peak_equity = max(account.peak_equity, health.equity)
    account.updated_ts = now
    save_account(state_dir, account)
    save_positions(state_dir, positions)
    report["equity"] = health.equity
    # carried = positions still open after the sweep (audit_and_reflect's own counter only tracks
    # unmapped/dataless holdings, so report the true held count here for the fast-loop output).
    report["carried"] = len(positions)

    # liquidation-proximity / hard-drawdown tripwire (re-parameterized for the aggressive envelope)
    pos_dicts = [{"symbol": p.symbol, "direction": p.direction, "liq_price": p.liq_price}
                 for p in positions]
    # liq_buffer 0.03 (not the 0.10 default): at up to 10x a HEALTHY stop-protected position sits
    # ~5% from liquidation, so a 10% buffer would alert on nearly every position (noise that buries
    # real warnings). The STOP (far inside liq per the gate's 2.5x liq-distance invariant) is the
    # binding exit; a 3% buffer fires only when liq is genuinely close (a gap PAST the stop toward
    # liq) — the actual 10x danger worth surfacing.
    mon = check_positions(pos_dicts, prices, equity=health.equity,
                          peak_equity=account.peak_equity, liq_buffer=0.03, dd_halt=0.45)
    report["alerts"] = mon["alerts"]
    report["should_halt"] = mon["should_halt"]
    # ACTUALLY TRIP the tripwire: at -45% drawdown set the HALT flag so new opens are blocked before
    # the -50% force-flatten (a single gap can breach -50% between 15m sweeps); notify on any alert.
    if mon["should_halt"]:
        from futures_fund.state import set_halt
        set_halt(state_dir, True, reason="fast monitor: -45% pre-flatten drawdown tripwire")
    if mon["alerts"]:
        from futures_fund.monitor import notify
        notify(state_dir, f"fast-loop risk alerts: {mon['alerts']}", now)
    _write_report(state_dir, cycle_no, report)
    return report
