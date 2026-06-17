"""Phases 7-10 CLI: gate + consolidate + execute the trader proposals; persist + report.

    uv run python scripts/gate_execute_cli.py --cycle N
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from futures_fund.config import load_settings
from futures_fund.cycle_io import load_output, save_output
from futures_fund.exchange import FuturesExchange
from futures_fund.orchestration import (
    funnel_skipped,
    gate_execute_step,
    management_review,
    reclassify_skipped,
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycle", type=int, required=True)
    ap.add_argument("--symbols", default=None,
                    help="comma-separated unified symbols (the CIO/scout universe); overrides "
                         "config. Held positions are folded in automatically.")
    ap.add_argument("--loop", default="strategic", choices=["strategic", "fast"],
                    help="which loop's opens these are (journaled for loop attribution)")
    args = ap.parse_args()
    settings = load_settings()
    # explicit --symbols (even empty) is the Watcher's universe for this cycle; never the default
    if args.symbols is not None:
        syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
        settings = settings.model_copy(update={"symbols": syms})
    else:
        # ROBUSTNESS: when --symbols is omitted, fold in the universe the desks actually analyzed —
        # the unified symbols from THIS cycle's context.json briefs. Without this the gate would
        # fetch only the config-default symbols and DROP every proposal on a scout-picked alt
        # (their specs/funding were never loaded) — silently flattening the whole book. Fail-safe:
        # on any error we leave settings unchanged (prior behavior).
        try:
            _ctx0 = load_output("state", args.cycle, "context")
            _uni = [b.get("symbol") for b in (_ctx0.get("briefs") or [])
                    if b.get("symbol") and not b.get("regime_panel_only")]
            if _uni:
                settings = settings.model_copy(update={"symbols": sorted(set(_uni))})
                print(f"INFO: --symbols omitted; folded {len(set(_uni))} symbols from "
                      f"cycle {args.cycle} context briefs into the gate universe.", file=sys.stderr)
        except FileNotFoundError:
            print("WARNING: --symbols omitted and context.json missing — gate runs on config "
                  "default symbols; proposals on unfetched symbols will drop.", file=sys.stderr)
    ex = FuturesExchange.from_settings(settings)
    payload = load_output("state", args.cycle, "proposals")
    # The agent path ALWAYS carries a holdings review (possibly empty). A missing/null management
    # key must NEVER reach the gate as None — that would close the whole book by absence on a
    # stand-down/HALT. Coerce to an empty review (keep holdings) and surface the anomaly.
    if payload.get("management") is None:
        print("WARNING: proposals.json has no 'management' key — treating as an empty holdings "
              "review (holdings KEPT, not closed by absence).", file=sys.stderr)
    management = management_review(payload)
    # regime_state (the SYMMETRIC conviction + entry-style shaper) is classified in preflight ->
    # context.json; the `triggers` list (resting conditional orders) rides alongside proposals.
    # FAIL-CLOSED at this production boundary: if context.json is missing/stale OR carries no
    # regime_state, substitute a DEGRADED sentinel (no quorum) rather than None. None would
    # pass-through to a naked MARKET entry; the degraded sentinel routes BOTH directions through a
    # confirmation trigger — a never-read tape can never open a naked position (mirror-symmetric).
    _DEGRADED = {"regime": "mixed", "confirmed": False,
                 "drivers": {"quorum_met": False, "degraded": ["context_missing"]}}
    # Pillar 4 AUDIT — build the anti-hallucination ground truth {raw symbol: {mark, atr}} from the
    # context briefs so the gate can drop any proposal/trigger whose entry/atr was fabricated.
    ground_truth: dict = {}
    try:
        _ctx = load_output("state", args.cycle, "context")
        regime_state = _ctx.get("regime_state") or _DEGRADED
        for b in _ctx.get("briefs", []):
            sym = b.get("exchange_id")
            if sym:
                ground_truth[sym] = {"mark": b.get("last_close"), "atr": b.get("atr")}
    except FileNotFoundError:
        print("WARNING: context.json missing — regime UNREAD; fail-closed (both directions must "
              "confirm via trigger, no naked market entry).", file=sys.stderr)
        regime_state = _DEGRADED
    # FAIL-LOUD: a SKIPPED reclassify (Phase 4.6) must not run the gate with a news-blind regime.
    # If the analysts produced a news judgement but the news-fold was never applied, BLOCK and tell
    # the orchestrator to run reclassify first (the candle is left UNSERVED -> the next due_check
    # returns DUE RETRY, so the self-healing poll re-runs it correctly).
    try:
        analyst_reports = load_output("state", args.cycle, "analyst_reports")
    except FileNotFoundError:
        analyst_reports = None
    # FAIL-LOUD: a cycle that submits TRADES but has NO analyst_reports.json skipped the whole
    # analyst funnel (Phases 4-4.6). Block so it can't execute on a news-blind preflight regime; the
    # candle stays UNSERVED -> next due_check returns DUE RETRY and the poll re-runs it correctly.
    if funnel_skipped(analyst_reports, payload.get("proposals"), payload.get("triggers")):
        print(f"ERROR: cycle {args.cycle} submits trades (proposals/triggers) but "
              f"analyst_reports.json is MISSING — the analyst pass / screen / reclassify (Phases "
              f"4-4.6) were SKIPPED. Run the full funnel (4 analysts -> screen -> reclassify) "
              f"first. A genuine stand-down must submit EMPTY proposals AND triggers.",
              file=sys.stderr)
        raise SystemExit(2)
    if reclassify_skipped(regime_state, analyst_reports):
        print(f"ERROR: reclassify (Phase 4.6) was SKIPPED for cycle {args.cycle} — analyst_reports "
              f"carry a news risk_off judgement but regime_state.drivers.news_risk_off was never "
              f"folded (news-blind regime). Run:\n"
              f"  uv run python scripts/reclassify_cli.py --cycle {args.cycle}\n"
              f"then re-run the gate.", file=sys.stderr)
        raise SystemExit(2)
    triggers = payload.get("triggers") or []
    cancel_triggers = payload.get("cancel_triggers") or []  # team retires decayed armed triggers
    now = datetime.now(UTC)  # gate-START instant: stamps the SERVED CANDLE for the due-gate
    report = gate_execute_step(ex, settings, "state", "memory",
                               now=now, cycle_no=args.cycle,
                               proposals=payload.get("proposals", []),
                               management=management, regime_state=regime_state, triggers=triggers,
                               cancel_triggers=cancel_triggers, ground_truth=ground_truth,
                               loop=args.loop)
    # ATTRIBUTION (Phase 0 of the learning loop): stamp desk x regime x close_reason x r_multiple
    # onto the journal so the learner can later slice experience by 'which desk lost, in which
    # regime, and why'. The gate's own close-patch omits these; we re-patch here rather than edit
    # the PROTECTED cycle.py. FAIL-SAFE: learning is advisory — a bug here must never break trading.
    try:
        from futures_fund.attribution import stamp_cycle_attribution
        desk_by_symbol: dict = {}
        try:
            _cio = load_output("state", args.cycle, "cio")
            desk_by_symbol = {a.get("symbol"): a.get("desk")
                              for a in (_cio.get("allocations") or []) if a.get("symbol")}
        except FileNotFoundError:
            pass
        stamp_cycle_attribution("memory", args.cycle, report,
                                (regime_state or {}).get("regime"), desk_by_symbol, now)
    except Exception as _e:  # noqa: BLE001 — never let the learning layer break the trading cycle
        print(f"WARNING: attribution stamping failed (non-fatal): {_e!r}", file=sys.stderr)
    # LEARNING LOOP (Phase 1): on the STRATEGIC loop only (the fast loop scalps within strategic
    # posture and does not re-reflect), bridge the CIO's declined-setup verdicts into the flat
    # journal, then run ONE reflect pass so every close updates the two-sided, DSR-gated lesson
    # corpus the next cycle's agents read. FAIL-SAFE — advisory; a bug here never breaks trading.
    if args.loop == "strategic":
        _reg = (regime_state or {}).get("regime")
        try:
            from futures_fund.flat_journal import record_cycle_flat_verdicts
            _cioj = {}
            try:
                _cioj = load_output("state", args.cycle, "cio")
            except FileNotFoundError:
                # cio.json should ALWAYS exist on a strategic cycle — warn LOUDLY (don't silently
                # pass) so a skipped 'write cio.json' step is visible: its flat_verdicts (declined
                # edge-aligned setups) are then lost to the learning loop.
                print(f"WARNING: cycle {args.cycle} cio.json MISSING on the strategic loop — the "
                      f"CIO's flat_verdicts were NOT journaled (the enabling-lesson data source is "
                      f"lost for this cycle). Write state/cycle/{args.cycle}/cio.json.",
                      file=sys.stderr)
            _marks: dict = {}
            try:
                _ctx2 = load_output("state", args.cycle, "context")
                _marks = {b.get("exchange_id"): b.get("last_close")
                          for b in (_ctx2.get("briefs") or []) if b.get("exchange_id")}
            except FileNotFoundError:
                pass
            record_cycle_flat_verdicts("memory", args.cycle, _cioj.get("flat_verdicts") or [],
                                       now, regime=_reg, marks=_marks)
        except Exception as _e:  # noqa: BLE001
            print(f"WARNING: flat-verdict bridge failed (non-fatal): {_e!r}", file=sys.stderr)
        try:
            from futures_fund.reflect_runner import reflect_and_record
            # dsr_pvalue=None -> each lesson is gated on its OWN cell's DSR (Phase 3), not the
            # desk-wide track record. A cell-specific rule can't validate until that cell is proven.
            _summary = reflect_and_record("memory", now, args.cycle, dsr_pvalue=None)
            print(f"LEARN: reflect cycle {args.cycle} (per-cell DSR) -> {_summary}",
                  file=sys.stderr)
        except Exception as _e:  # noqa: BLE001
            print(f"WARNING: reflect pass failed (non-fatal): {_e!r}", file=sys.stderr)
    # Run-markers consumed by scripts/due_check.py (poll candle gate). candle = the served candle of
    # this loop's OWN timeframe (floor of the gate-start), so the per-loop due-gate sees it served;
    # ran_at = audit/clock-skew sentinel. (Strategic 1h: floor_tf(now,60); the legacy 4h path: 240.)
    from futures_fund.scheduling import floor_tf, tf_to_minutes
    _ls = settings.loops.get(args.loop)
    tf_min = tf_to_minutes(_ls.timeframe) if _ls is not None else 240
    report["ran_at"] = now.isoformat()
    report["candle"] = floor_tf(now, tf_min).isoformat()
    save_output("state", args.cycle, "report", report)
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
