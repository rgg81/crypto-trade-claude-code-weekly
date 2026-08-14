#!/usr/bin/env python3
"""Monthly parameter review for the blended all-weather strategy.

Validates that the current blended score formula is performing well using
the last N days of live cycle data. Checks:
- Profitability (equity growth)
- Churn (rotation frequency)
- Neutrality (book balance)
- Risk (drawdown, stop-outs)

Proposes parameter adjustments if performance degrades.

Usage:
    uv run python scripts/monthly_review.py --days 60
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

from futures_fund.costs import trade_fee
from futures_fund.journal import read_all_decisions


def load_cycle_equity_curve(days: int, state_dir: Path = Path("state")) -> list[dict]:
    """Load equity curve from cycle data.

    Returns list of (cycle_num, equity, timestamp).
    """
    account_file = state_dir / "account.json"
    if not account_file.exists():
        raise FileNotFoundError(f"No account data found in {account_file}")

    with open(account_file) as f:
        account = json.load(f)

    # Load cycle history from equity log if available
    equity_log = state_dir / "equity_log.json"
    if equity_log.exists():
        with open(equity_log) as f:
            return json.load(f)

    # Otherwise, build from cycle folders
    cycles = []
    cycle_dir = state_dir / "cycle"

    for cycle_path in sorted(cycle_dir.iterdir(), key=lambda p: int(p.name)):
        try:
            cycle_num = int(cycle_path.name)
        except ValueError:
            continue

        context_file = cycle_path / "context.json"
        if not context_file.exists():
            continue

        with open(context_file) as f:
            context = json.load(f)

        equity = context.get("equity", account.get("equity", 10000.0))
        cycle_time = context.get("cycle_time")

        cycles.append({
            "cycle": cycle_num,
            "equity": equity,
            "time": cycle_time,
        })

    return cycles


def compute_performance_metrics(cycles: list[dict]) -> dict:
    """Compute performance metrics from equity curve."""
    if len(cycles) < 2:
        return {"error": "Not enough data points"}

    equities = [c["equity"] for c in cycles]
    init_equity = equities[0]
    final_equity = equities[-1]

    # Total return
    total_return_pct = (final_equity - init_equity) / init_equity * 100

    # Monthly return (at 4h cadence: 6 cycles/day = 180 cycles/month)
    cycles_per_month = 180.0
    months = len(cycles) / cycles_per_month
    if months > 0:
        monthly_return_pct = total_return_pct / months
    else:
        monthly_return_pct = 0.0

    # Max drawdown
    peak = equities[0]
    max_dd = 0.0
    for eq in equities:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100
        if dd > max_dd:
            max_dd = dd

    # Volatility (std of cycle returns)
    returns = []
    for i in range(1, len(equities)):
        ret = (equities[i] - equities[i-1]) / equities[i-1]
        returns.append(ret)

    volatility = np.std(returns) * 100 if returns else 0.0

    return {
        "total_return_pct": round(total_return_pct, 2),
        "monthly_return_pct": round(monthly_return_pct, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "volatility_pct": round(volatility, 2),
        "final_equity": round(final_equity, 2),
        "cycles_analyzed": len(cycles),
    }


def _num(v) -> float:
    """Coerce a journal field to float; a corrupt record must degrade, never raise. A raise here
    exits the script non-zero, auto_cycle prints `review FAILED`, no report is written, so
    `_monthly_review_due` stays True and the driver re-fails on EVERY 30-min tick."""
    try:
        return float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0


def filter_to_window(trades: list[dict], days: int, now=None) -> list[dict]:
    """Keep trades that EXITED within the review window.

    The decomposition is a *monthly parameter review* input, so it must not be all-time: an
    all-time numerator and denominator only accumulate, so once enough historical fee bleed is
    booked the flag can never clear again (and symmetrically a strong early period masks present
    degradation). Records with an unparseable/missing exit_ts are KEPT — dropping them would
    silently shrink the measured cost base, which biases toward a pass.
    """
    if not days or days <= 0:
        return list(trades)
    ref = now if isinstance(now, datetime) else (
        datetime.fromisoformat(str(now).replace("Z", "+00:00")) if now else datetime.now())
    out = []
    for t in trades:
        raw = t.get("exit_ts")
        if not raw:
            out.append(t)
            continue
        try:
            ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            out.append(t)
            continue
        r = ref if ts.tzinfo == ref.tzinfo else ref.replace(tzinfo=ts.tzinfo)
        if (r - ts).days <= days:
            out.append(t)
    return out


def load_reduce_realized(state_dir, since_cycle: int | None = None) -> float:
    """Realized PnL banked by PARTIAL REDUCES, summed from the cycle reports.

    `reduce.py` banks a slice via `close_at_mark` and the gate credits it to the wallet, but no
    code path patches the decisions journal for a reduce — the runner keeps the same decision_id
    and the final close overwrites the record. So the journal alone understates the edge (live:
    69 reduces worth +$107.69 invisible against a measured gross of $149.39). The gate already
    records each one as `{"reduce": SYM, "fraction": f, "pnl": ...}`, so this is a pure reporting
    read — no change to the live trading path.
    """
    total = 0.0
    cycle_dir = Path(state_dir) / "cycle"
    if not cycle_dir.is_dir():
        return 0.0
    for d in cycle_dir.iterdir():
        if not d.name.isdigit() or (since_cycle is not None and int(d.name) < since_cycle):
            continue
        try:
            rep = json.loads((d / "report.json").read_text())
        except (OSError, ValueError):
            continue
        for a in rep.get("actions") or []:
            if isinstance(a, dict) and a.get("reduce"):
                total += _num(a.get("pnl"))
    return total


def cost_decomposition(trades: list[dict], reduce_realized: float = 0.0) -> dict:
    """Split realized PnL into EDGE vs TURNOVER COST vs CARRY.

    HARD RULE 6: a leg must clear its round-trip cost. The headline return cannot show whether it
    does — a book whose price selection loses money can still print a gain if funding carry covers
    the fees. This inverts the exits.py identity (`realized = gross - exit_fee - funding`) to
    recover the gross price PnL, then charges BOTH fee legs:

        net = gross_price_pnl - (entry_fees + exit_fees) + funding_carry

    `funding` is signed at source: POSITIVE = we paid it, NEGATIVE = we received a credit. It is
    reported as `funding_carry` with the sign FLIPPED, so positive always means income.
    `realized_pnl` nets only the EXIT fee (the entry fee is charged at open), so the entry side is
    reconstructed from the recorded notional — counting only the exit side halves the true cost.

    `reduce_realized` folds in partial-reduce slices (see `load_reduce_realized`). It is already
    net of that slice's own fees, which cannot be split out, so adding it to `gross` UNDERSTATES
    the edge slightly — the resulting ratio is therefore a conservative LOWER bound.

    `unknown_notional` counts records with no size/entry: their entry fee reads as zero, which
    understates fees and OVERSTATES the ratio, so a pass on such data is not trustworthy.
    """
    gross = entry_fees = exit_fees = funding = 0.0
    unknown = 0
    for t in trades:
        realized, exit_fee = _num(t.get("realized_pnl")), _num(t.get("fees"))
        fund = _num(t.get("funding_paid"))
        gross += realized + exit_fee + fund          # invert realized = gross - fee - funding
        exit_fees += exit_fee
        funding += fund
        notional = _num(t.get("size")) * _num(t.get("entry"))
        if notional <= 0:
            unknown += 1
        entry_fees += trade_fee(notional, maker=False)
    gross += float(reduce_realized or 0.0)
    total_fees = entry_fees + exit_fees
    return {
        "trades": len(trades),
        "gross_price_pnl": round(gross, 2),
        "reduce_realized": round(float(reduce_realized or 0.0), 2),
        "entry_fees": round(entry_fees, 2),
        "exit_fees": round(exit_fees, 2),
        "total_fees": round(total_fees, 2),
        "funding_carry": round(-funding, 2),          # flip: positive = credit RECEIVED
        "net": round(gross - total_fees - funding, 2),
        "edge_cover_ratio": round(gross / total_fees, 3) if total_fees else None,
        "unknown_notional": unknown,
    }


def carry_is_income(decomp: dict) -> bool:
    """True when funding is a net CREDIT. The 'carry is doing the work' verdict must not fire on
    a desk that is PAYING funding — there the carry sleeve is a second cost, not the rescue."""
    return _num(decomp.get("funding_carry")) > 0


def edge_covers_costs(decomp: dict) -> bool | None:
    """True when the price-selection edge pays for its own turnover. None when unknowable — an
    unknown must never read as a pass.

    With `unknown_notional > 0` the fee total is a LOWER bound, so the ratio is overstated:
      * a FAIL still holds (true fees are higher, so the true ratio is even worse) -> report False
      * a PASS is not trustworthy on undercounted fees                             -> report None
    """
    total_fees = _num(decomp.get("total_fees"))
    if not total_fees:
        return None
    covers = _num(decomp.get("gross_price_pnl")) > total_fees
    if covers and int(decomp.get("unknown_notional") or 0) > 0:
        return None
    return covers


def analyze_rotation_pattern(cycles: list[dict], state_dir: Path) -> dict:
    """Analyze rotation/churn pattern from cycle data."""
    rotations = []

    cycle_dir = state_dir / "cycle"
    for cyc in cycles:
        cycle_num = cyc["cycle"]
        cycle_path = cycle_dir / str(cycle_num)

        # Check for plan file
        plan_file = cycle_path / "plan.json"
        if plan_file.exists():
            with open(plan_file) as f:
                plan = json.load(f)

            # Count rotations (changes from previous book)
            n_rot = len(plan.get("close", []))
            rotations.append(n_rot)

    if not rotations:
        return {"error": "No rotation data found"}

    avg_rotations = np.mean(rotations)
    max_rotations = max(rotations)

    # Estimate turnover cost (0.14% per leg rotation)
    avg_turnover_cost_pct = avg_rotations * 0.14

    return {
        "avg_rotations_per_cycle": round(avg_rotations, 1),
        "max_rotations_in_cycle": max_rotations,
        "estimated_turnover_cost_pct_per_cycle": round(avg_turnover_cost_pct, 2),
        "total_rotations": len(rotations),
    }


def check_neutrality_compliance(cycles: list[dict], state_dir: Path) -> dict:
    """Check if book stayed neutral across all cycles."""
    violations = []

    cycle_dir = state_dir / "cycle"
    for cyc in cycles:
        cycle_num = cyc["cycle"]
        cycle_path = cycle_dir / str(cycle_num)

        # Check for post-gate positions
        pos_file = cycle_path / "positions_after.json"
        if pos_file.exists():
            with open(pos_file) as f:
                positions = json.load(f)

            # Count longs vs shorts
            n_long = sum(1 for p in positions if p.get("direction") == "long")
            n_short = sum(1 for p in positions if p.get("direction") == "short")

            if n_long == 0 or n_short == 0:
                violations.append({
                    "cycle": cycle_num,
                    "issue": "flat_book",
                    "n_long": n_long,
                    "n_short": n_short,
                })

    return {
        "cycles_checked": len(cycles),
        "neutrality_violations": len(violations),
        "violation_details": violations[:5],  # First 5
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60,
                    help="Number of days to review (default: 60)")
    ap.add_argument("--min-monthly-return", type=float, default=3.0,
                    help="Min acceptable monthly return % "
                    "(default: 3.0 per TEMPEST-NEUTRAL mandate)")
    ap.add_argument("--max-drawdown", type=float, default=10.0,
                    help="Max acceptable drawdown % (default: 10.0)")
    ap.add_argument("--state", default="state", help="State directory")
    ap.add_argument("--memory", default="memory",
                    help="Decisions journal dir (closed-trade PnL/fees/funding)")
    args = ap.parse_args()

    print(f"\n{'='*60}")
    print("BLENDED SCORE MONTHLY REVIEW")
    print(f"{'='*60}")
    print(f"Reviewing last {args.days} days")
    print()

    state_dir = Path(args.state)

    # Load equity curve
    try:
        cycles = load_cycle_equity_curve(args.days, state_dir)
    except FileNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)

    if len(cycles) < 5:
        print(f"Warning: Only {len(cycles)} cycles found. Need at least 5 for review.")
        sys.exit(1)

    print(f"Loaded {len(cycles)} cycles")

    # Compute performance metrics
    perf = compute_performance_metrics(cycles)
    print("\nPerformance:")
    print(f"  Total return: {perf['total_return_pct']:+.2f}%")
    print(f"  Monthly return: {perf['monthly_return_pct']:+.2f}%")
    print(f"  Max drawdown: {perf['max_drawdown_pct']:.1f}%")
    print(f"  Volatility: {perf['volatility_pct']:.2f}%")
    print(f"  Final equity: ${perf['final_equity']:,.2f}")

    # EDGE vs TURNOVER COST (HARD RULE 6) — from the decisions journal, which records every
    # closed trade's realized PnL / fees / funding. This is the check the churn section below
    # could never make: it answers "does the selection edge pay for its own trading?".
    # The whole computation is guarded: `read_all_decisions` is already defensive, but the
    # numeric coercions are where a hand-edited/truncated record would blow up — and an exception
    # here writes no report, so `_monthly_review_due` stays True and auto_cycle re-fails the
    # review on every 30-min tick forever.
    try:
        _all = [r for r in read_all_decisions(Path(args.memory))
                if r.get("realized_pnl") is not None]
        _closed = filter_to_window(_all, args.days)
        _first = min((c["cycle"] for c in cycles), default=None)
        decomp = cost_decomposition(_closed, reduce_realized=load_reduce_realized(
            state_dir, since_cycle=_first))
    except Exception as e:  # noqa: BLE001 — never wedge the driver on a bad record
        _closed, decomp = [], {"error": str(e)[:200]}
    covers = None if "error" in decomp else edge_covers_costs(decomp)
    if "error" not in decomp and decomp["trades"]:
        print(f"\nEdge vs cost (closed trades, last {args.days}d):")
        print(f"  Gross price PnL: ${decomp['gross_price_pnl']:+,.2f}   <- selection edge "
              f"(incl. ${decomp['reduce_realized']:+,.2f} from partial reduces)")
        print(f"  Entry+exit fees: ${-decomp['total_fees']:+,.2f}")
        print(f"  Funding carry:   ${decomp['funding_carry']:+,.2f}"
              f"{'' if carry_is_income(decomp) else '   <- a COST, not income'}")
        print(f"  Net:             ${decomp['net']:+,.2f}  over {decomp['trades']} trades")
        _r = decomp["edge_cover_ratio"]
        _ratio = f" ({_r:.2f}x)" if _r is not None else ""
        print(f"  Edge covers fees: {covers}{_ratio}")
        if decomp["unknown_notional"]:
            print(f"  NOTE: {decomp['unknown_notional']} record(s) with unknown notional — "
                  f"entry fees undercounted, so a PASS is withheld")

    # Analyze rotation pattern
    rot = analyze_rotation_pattern(cycles, state_dir)
    if "error" not in rot:
        print("\nChurn:")
        print(f"  Avg rotations/cycle: {rot['avg_rotations_per_cycle']:.1f}")
        print(f"  Max rotations in cycle: {rot['max_rotations_in_cycle']}")
        print(f"  Est. turnover cost: {rot['estimated_turnover_cost_pct_per_cycle']:.2f}%/cycle")

    # Check neutrality
    neut = check_neutrality_compliance(cycles, state_dir)
    print("\nNeutrality:")
    print(f"  Cycles checked: {neut['cycles_checked']}")
    print(f"  Violations: {neut['neutrality_violations']}")

    # Build recommendations
    recommendations = []

    # Check if monthly return is below target
    if perf['monthly_return_pct'] < args.min_monthly_return:
        recommendations.append({
            "type": "performance_degradation",
            "severity": "warning" if perf['monthly_return_pct'] > 0 else "critical",
            "issue": (f"Monthly return {perf['monthly_return_pct']:.2f}% "
                     f"below target {args.min_monthly_return}%"),
            "suggested_action": "Review formula weights; consider backtest alternatives",
        })

    # Check if drawdown is excessive
    if perf['max_drawdown_pct'] > args.max_drawdown:
        recommendations.append({
            "type": "risk_spike",
            "severity": "critical",
            "issue": (f"Max drawdown {perf['max_drawdown_pct']:.1f}% "
                     f"exceeds threshold {args.max_drawdown}%"),
            "suggested_action": "Review stop-loss sizing; consider reducing per-trade risk",
        })

    # Check neutrality violations
    if neut['neutrality_violations'] > 0:
        recommendations.append({
            "type": "neutrality_breach",
            "severity": "critical",
            "issue": f"{neut['neutrality_violations']} cycles with flat books",
            "suggested_action": ("Review hysteresis logic; "
                               "ensure universe always has viable candidates"),
        })

    # HARD RULE 6: the selection edge must clear its own round-trip cost. When it doesn't, the
    # book is solvent only via funding carry — a structural drag no drawdown breaker can see
    # (the -5/-10/-15% ladder measures peak-to-trough, not fee bleed).
    if covers is False:
        _r = decomp["edge_cover_ratio"]
        _income = carry_is_income(decomp)
        # Only credit carry when it is actually a CREDIT — when the desk is PAYING funding the
        # sleeve is a second cost, the verdict is strictly worse, and "reweight toward carry"
        # would be advice to lean into the bleed.
        _carry_note = (f"carry ${decomp['funding_carry']:+,.2f} is covering the gap"
                       if _income else
                       f"and carry ${decomp['funding_carry']:+,.2f} is a COST too — "
                       f"nothing is paying for the turnover")
        recommendations.append({
            "type": "edge_below_turnover_cost",
            "severity": "critical",
            "issue": (f"Gross price edge ${decomp['gross_price_pnl']:+,.2f} covers only "
                      f"{_r:.0%} of ${decomp['total_fees']:,.2f} in fees over "
                      f"{decomp['trades']} trades; net ${decomp['net']:+,.2f} ({_carry_note})"),
            "suggested_action": (
                "Cut turnover (raise swap_margin / lengthen hold horizon)"
                + (" or reweight toward the carry sleeve that is actually paying" if _income
                   else "; do NOT lean on carry — it is negative. Re-examine the score weights")),
        })

    # Check excessive churn
    if "error" not in rot and rot['avg_rotations_per_cycle'] > 6:
        recommendations.append({
            "type": "excessive_churn",
            "severity": "warning",
            "issue": f"Avg {rot['avg_rotations_per_cycle']:.1f} rotations/cycle (costly)",
            "suggested_action": "Increase swap_margin or keep_buffer to reduce churn",
        })

    # Print recommendations
    print(f"\n{'='*60}")
    print("RECOMMENDATIONS")
    print(f"{'='*60}")

    if not recommendations:
        print("No issues found. Current parameters are robust.")
    else:
        for i, rec in enumerate(recommendations, 1):
            print(f"\n{i}. {rec['type'].upper()} [{rec['severity'].upper()}]")
            print(f"   Issue: {rec['issue']}")
            print(f"   Action: {rec['suggested_action']}")

    # Write report
    review_date = datetime.now().strftime("%Y-%m")
    review_dir = state_dir / "monthly_review"
    review_dir.mkdir(parents=True, exist_ok=True)

    report = {
        "review_date": review_date,
        # WHEN THIS REVIEW RAN — the driver's 30-day gate keys off this. The filename is the month
        # REVIEWED and is rewritten in place on a re-run, so it can't date the run itself (cy216).
        "run_ts": datetime.now().isoformat(timespec="seconds"),
        "review_period_days": args.days,
        "performance": perf,
        "churn": rot,
        "cost_decomposition": decomp,
        "edge_covers_costs": covers,
        "neutrality": neut,
        "recommendations": recommendations,
        "current_params": {
            "trend_weights": {"mom": 0.55, "carry": 0.35, "mr": 0.10},
            "range_weights": {"mom": 0.40, "carry": 0.40, "mr": 0.20},
            "n_per_side": 3,
            "swap_margin": 0.5,
            "keep_buffer": 2,
            "min_oi_usd": 75e6,
        },
    }

    report_file = review_dir / f"{review_date}.json"
    with open(report_file, "w") as f:
        json.dump(report, f, indent=2)

    print(f"\nReport written to: {report_file}")
    print("\nNext review: Run this script again in ~30 days.")


if __name__ == "__main__":
    main()
