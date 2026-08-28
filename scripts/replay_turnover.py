"""Replay the recorded cycles under alternative turnover settings and price the trade-off.

THE PROBLEM THIS EXISTS TO SOLVE. Over 71 days the desk earned +$564.22 gross (price +$137.52,
reduces +$134.24, funding carry +$292.46) and paid $561.30 in fees — 99.5% of the edge. The single
largest identified waste is structural rather than market-driven: 123 of 381 opens (32.3%) close and
reopen the SAME symbol in the SAME cycle to change a leg's SIZE, a full 0.14% round trip that
carries no information.

So: replay all 355 recorded cycles under different hysteresis / resize settings, and measure what
each setting costs in fees against what it gives up in signal.

SIZING IS HELD CONSTANT per leg, on purpose. The live book's size swung 0.85x -> 0.11x -> 0.5x,
driven by the breaker ladder, which depends on the equity path, which depends on the policy under
test — circular. Fixing notional makes policies directly comparable; the harness is validated
against TURNOVER instead, which is parameter-driven and observable in the record.

Read-only: it touches nothing under state/ except to read, and never places an order.

Usage: uv run python scripts/replay_turnover.py [--sweep]
"""
from __future__ import annotations

import argparse
import glob
import json

from futures_fund import blended_score as bs

FEE_PER_FILL = 0.0007          # Binance USD-M taker; a round trip is 2 fills = 0.14%
CYCLE_HOURS = 4.0              # the desk's single 4h loop
LEG_NOTIONAL = 500.0           # constant per leg (see module docstring)


def fees_for(opens: int, closes: int, notional: float) -> float:
    """Taker fees for a cycle's fills. One fill per open and per close."""
    return (max(0, opens) + max(0, closes)) * max(0.0, notional) * FEE_PER_FILL


def forward_return(price_now: float, price_next: float) -> float:
    """Simple return over one cycle; 0.0 rather than a divide-by-zero on missing data."""
    p = float(price_now or 0.0)
    return (float(price_next or 0.0) - p) / p if p > 0 else 0.0


def leg_pnl(direction: str, notional: float, price_now: float, price_next: float) -> float:
    """PnL of holding one leg across a cycle. A short profits when price falls."""
    sign = 1.0 if direction == "long" else -1.0
    return sign * notional * forward_return(price_now, price_next)


def funding_pnl(direction: str, notional: float, rate: float | None,
                interval_hours: float | None) -> float:
    """Carry over one cycle, pro-rated against the symbol's funding interval.

    Same sign convention as `exits.py` ("positive = we PAID it") and `_long_favorability` (carry
    favours a long when funding is negative): a LONG pays a positive rate, a SHORT collects it.
    """
    if rate is None:
        return 0.0
    # MISSING interval -> the exchange default of 8h. An explicit 0 or negative is BAD DATA, not a
    # missing value: `float(x or 8.0)` would silently turn 0.0 into 8h and invent carry from
    # nothing, so the two cases are separated deliberately.
    iv = 8.0 if interval_hours is None else float(interval_hours)
    if iv <= 0:
        return 0.0
    sign = -1.0 if direction == "long" else 1.0
    return sign * float(rate) * notional * (CYCLE_HOURS / iv)


def _raw(sym: str) -> str:
    return sym.split("/")[0] + "USDT" if "/" in sym else sym


def load_cycles(state: str = "state") -> list[dict]:
    """Every recorded cycle with usable briefs, in cycle order."""
    out = []
    for p in sorted(glob.glob(f"{state}/cycle/*/context.json"),
                    key=lambda s: int(s.split("/")[-2])):
        try:
            c = json.load(open(p))
        except Exception:  # noqa: BLE001, PERF203 — a corrupt cycle is skipped, not fatal
            continue
        briefs = [{**b, "symbol": _raw(b["symbol"])} for b in (c.get("briefs") or [])
                  if b.get("last_close")]
        if briefs:
            out.append({"cycle": int(p.split("/")[-2]), "briefs": briefs})
    return out


# Measured from the record, not simulated: 123 of 381 opens were a close+reopen of the SAME symbol
# in the SAME cycle (32.3%), across 49 of 355 cycles. Each is two taker fills.
OBSERVED_RESIZES = 123


def replay(cycles: list[dict], *, n_per_side: int = 3, swap_margin: float = 0.5,
           keep_buffer: int = 2, resize: bool = True, leg_notional: float = LEG_NOTIONAL) -> dict:
    """Walk the recorded cycles under one policy and price it.

    `resize=False` models dropping the close+reopen size top-up entirely — the 32.3%-of-opens
    waste. Everything else (scores, universe, price path) is the RECORDED history, so the only
    thing varying between runs is the turnover policy.
    """
    holdings: dict[str, str] = {}
    gross = funding = fees = 0.0
    opens = closes = 0
    for i, cy in enumerate(cycles[:-1]):
        briefs = cy["briefs"]
        by = {b["symbol"]: b for b in briefs}
        nxt = {b["symbol"]: b for b in cycles[i + 1]["briefs"]}
        scored = bs.composite_scores(briefs)
        if not scored:
            continue
        plan = bs.apply_hysteresis(scored, holdings, n_per_side=n_per_side,
                                   keep_buffer=keep_buffer, swap_margin=swap_margin,
                                   openable={b["symbol"] for b in briefs})
        o = list(plan["open_long"]) + list(plan["open_short"])
        c = list(plan["close"])
        # ROTATION ONLY. The resize close+reopen is NOT simulated: its trigger is a size deficit,
        # and constant sizing eliminates size deficits by construction, so a simulated trigger
        # fires 1862 times against 123 observed — pure artefact. The resize cost is known exactly
        # from the record instead (see OBSERVED_RESIZES) and is added as a separate line item.
        for s_ in c:
            holdings.pop(s_, None)
        for s_ in plan["open_long"]:
            holdings[s_] = "long"
        for s_ in plan["open_short"]:
            holdings[s_] = "short"
        opens += len(o)
        closes += len(c)
        fees += fees_for(len(o), len(c), leg_notional)
        # carry the resulting book one cycle forward on the RECORDED prices
        for sym, d in holdings.items():
            b, n = by.get(sym), nxt.get(sym)
            if not b or not n:
                continue
            gross += leg_pnl(d, leg_notional, b["last_close"], n["last_close"])
            funding += funding_pnl(d, leg_notional, b.get("funding_rate"),
                                   b.get("funding_interval_hours"))
    resizes = OBSERVED_RESIZES if resize else 0
    resize_fees = fees_for(resizes, resizes, leg_notional)
    fees += resize_fees
    net = gross + funding - fees
    return {"gross": gross, "funding": funding, "fees": fees, "net": net,
            "opens": opens + resizes, "closes": closes + resizes, "resizes": resizes,
            "resize_fees": resize_fees, "cycles": len(cycles) - 1}


def _fmt(label: str, r: dict) -> str:
    return (f"{label:<28} net ${r['net']:+9,.2f} | gross ${r['gross']:+8,.2f} "
            f"carry ${r['funding']:+8,.2f} fees ${r['fees']:8,.2f} | "
            f"opens {r['opens']:4d} closes {r['closes']:4d} resizes {r['resizes']:4d}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="state")
    ap.add_argument("--sweep", action="store_true")
    args = ap.parse_args()
    cycles = load_cycles(args.state)
    print(f"replaying {len(cycles)} recorded cycles, ${LEG_NOTIONAL:.0f}/leg, "
          f"fee {FEE_PER_FILL:.2%}/fill\n")
    base = replay(cycles)
    print(_fmt("BASELINE (live settings)", base))
    if args.sweep:
        print()
        for sm in (0.5, 1.0, 1.5, 2.0):
            for rz in (True, False):
                r = replay(cycles, swap_margin=sm, resize=rz)
                print(_fmt(f"swap_margin={sm} resize={rz}", r))


if __name__ == "__main__":
    main()
