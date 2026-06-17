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
from datetime import UTC, datetime, timedelta
from pathlib import Path

from futures_fund.config import Settings
from futures_fund.cycle import CycleContext, audit_and_reflect
from futures_fund.monitor import check_positions
from futures_fund.portfolio import portfolio_health
from futures_fund.scheduling import floor_tf, last_served_candle, tf_to_minutes
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


def _sliced_ctx(ctx: CycleContext, bar_open) -> CycleContext | None:
    """A CycleContext whose every frame is truncated so its LAST row is the bar opening at
    `bar_open` — i.e. `audit_and_reflect` (which reads `.iloc[-1]`) checks exactly that bar. Symbols
    with no bar at `bar_open` are dropped (so a stale earlier bar is never mis-checked); None if no
    held symbol has a bar there."""
    frames, fundings, specs, raw_to_unified, specs_by_raw, prices = {}, {}, {}, {}, {}, {}
    for unified, df in ctx.frames.items():
        sub = df[df["timestamp"] <= bar_open]
        if len(sub) == 0 or sub["timestamp"].iloc[-1] != bar_open:
            continue
        spec = ctx.specs[unified]
        frames[unified] = sub
        fundings[unified] = ctx.fundings[unified]
        specs[unified] = spec
        raw_to_unified[spec.symbol] = unified
        specs_by_raw[spec.symbol] = spec
        prices[spec.symbol] = float(sub["close"].iloc[-1])
    if not frames:
        return None
    return CycleContext(ctx.settings, frames, fundings, specs, raw_to_unified, specs_by_raw, prices)


def _opened_le(position, bar_open) -> bool:
    """True if `position` already existed at `bar_open` (opened at/before the bar). Coerces a naive
    opened_ts to UTC; an absent opened_ts -> True (pre-existing; don't suppress a check)."""
    ot = getattr(position, "opened_ts", None)
    if ot is None:
        return True
    bo = bar_open.to_pydatetime() if hasattr(bar_open, "to_pydatetime") else bar_open
    if getattr(ot, "tzinfo", None) is None:
        ot = ot.replace(tzinfo=UTC)
    if getattr(bo, "tzinfo", None) is None:
        bo = bo.replace(tzinfo=UTC)
    return ot <= bo


def _replay_missed_bars(ctx, positions, account, memory_dir, prev_open, current_open, tf, report):
    """Downtime-gap backfill: replay every COMPLETED bar whose open lies strictly between
    `prev_open` (the last actually-swept candle) and `current_open` (the forming bar), oldest-first,
    so a stop/TP/liq that triggered while the desk was DOWN still closes — at that bar's price, with
    funding accrued only to that bar's close. Reuses the PROTECTED `audit_and_reflect`/`detect_exit`
    verbatim via frame slicing. Closing on the FIRST hitting bar is pessimistic (correct for a paper
    fill). No-op (empty gap set) on normal cadence, so the live path is unchanged.

    Two safety gates: (1) a bar is only exit-checked against positions that already EXISTED at its
    open (`_opened_le`) — the fast anchor does NOT advance on a strategic open, so the gap window
    can contain bars predating a just-opened position, and detect_exit has no opened_ts guard;
    without this a pre-entry stop/liq/TP would spuriously close a position at a price never traded.
    (2) If the outage predates the fetched OHLCV window, early bars can't be replayed -> alert
    instead of silently dropping them."""
    if not positions:
        return positions
    step = timedelta(minutes=tf_to_minutes(tf))
    # PER-SYMBOL partial-window alert: a held symbol whose fetched history starts AFTER prev_open
    # couldn't be replayed across the early gap. Check each symbol (NOT a global min — a deep symbol
    # like BTC would otherwise mask a thin symbol's short window) and name it.
    partial = sorted(
        ctx.specs[u].symbol for u, df in ctx.frames.items()
        if len(df) and df["timestamp"].iloc[0] > prev_open
    )
    if partial:
        report.setdefault("alerts", []).append(
            f"gap exceeds fetch window; early-outage bars unswept for {','.join(partial)}")
    gap_ts = sorted({
        ts for df in ctx.frames.values() for ts in df["timestamp"]
        if prev_open < ts < current_open
    })
    for bar_open in gap_ts:
        if not positions:
            break
        eligible = [p for p in positions if _opened_le(p, bar_open)]
        deferred = [p for p in positions if not _opened_le(p, bar_open)]
        if not eligible:
            continue
        ctx_i = _sliced_ctx(ctx, bar_open)
        if ctx_i is None:
            continue
        bar_close = bar_open.to_pydatetime().astimezone(UTC) + step
        # A throwaway sub-report: audit_and_reflect bumps carried for any eligible position whose
        # symbol _sliced_ctx dropped at this bar (sym is None branch). That per-bar carried count is
        # meaningless here (run_exit_sweep sets the true carried after the live sweep), so only the
        # real closes/actions flow back — no polluted carried, no load-bearing coupling.
        sub = {"closed": 0, "carried": 0, "actions": []}
        survivors = audit_and_reflect(ctx_i, eligible, account, memory_dir, bar_close, sub)
        report["closed"] += sub["closed"]
        report["actions"].extend(sub["actions"])
        positions = deferred + survivors
    return positions


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
        report["swept"] = False  # a halted no-sweep tick must NOT advance the backfill anchor:
        _write_report(state_dir, cycle_no, report)  # else a stop/liq during the halt is missed
        return report

    if not positions:
        # FLAT BOOK: no exits to sweep, but the -45% drawdown tripwire is a PURE equity check
        # (equity vs peak), independent of open positions. Still evaluate it so (a) the report
        # carries a real boolean should_halt (never None/ambiguous), and (b) a flat book sitting
        # at >=45% drawdown still HALTS — blocking re-deploys into the hole before the -50%
        # force-flatten. Reuse the module-level check_positions verbatim with an empty book (no
        # liq alerts; only the dd_halt arm fires). Flat -> equity == realized balance (no unreal).
        mon = check_positions([], {}, equity=account.balance, peak_equity=account.peak_equity,
                              liq_buffer=0.03, dd_halt=0.45)
        report["should_halt"] = mon["should_halt"]
        report["alerts"] = list(report.get("alerts", [])) + list(mon["alerts"])
        if mon["should_halt"]:
            from futures_fund.state import set_halt
            set_halt(state_dir, True,
                     reason="fast monitor: -45% pre-flatten drawdown tripwire (flat book)")
        report["swept"] = True  # empty book is a valid sweep (nothing to miss) -> anchor advances
        _write_report(state_dir, cycle_no, report)
        return report

    ctx, prices = _held_context(exchange, settings, positions, tf)
    # DOWNTIME-GAP backfill: if this fire follows an outage, completed bars went unswept. Replay
    # them so a stop/TP/liq that triggered while the desk was down still closes (a missed mid-gap
    # liquidation at 10x is the worst tail — the book would carry a position that no longer exists).
    # No-op on normal 15m cadence (the gap set is empty), so the live path is unchanged. Anchor on
    # the last ACTUALLY-SWEPT candle (require_swept) so a halt window's no-sweep ticks don't hide a
    # mid-halt exit. FAIL-SAFE: any backfill error degrades to latest-bar-only, never crashes the
    # load-bearing live exit check below.
    tfm = tf_to_minutes(tf)
    prev_served = last_served_candle(state_dir, now, tf_minutes=tfm, loop="fast",
                                     require_swept=True)
    if prev_served is not None:
        try:
            positions = _replay_missed_bars(ctx, positions, account, memory_dir,
                                            prev_served, floor_tf(now, tfm), tf, report)
        except Exception:  # noqa: BLE001 — backfill is best-effort; NEVER crash the live sweep
            report.setdefault("alerts", []).append("gap-backfill skipped after internal error")
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
    # EXTEND (don't clobber) — preserve any backfill alert (partial-window / error) raised above.
    report["alerts"] = list(report.get("alerts", [])) + list(mon["alerts"])
    report["should_halt"] = mon["should_halt"]
    report["swept"] = True  # a full exit check ran -> this candle is a real anchor for backfill
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
