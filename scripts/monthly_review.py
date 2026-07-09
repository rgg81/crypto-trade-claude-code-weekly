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
        "review_period_days": args.days,
        "performance": perf,
        "churn": rot,
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
