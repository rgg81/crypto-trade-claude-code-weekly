"""Blended-score book builder (all-weather, never-flat, minimum-rebalance).

Reads a cycle's preflight context + the live positions, ranks the liquid non-pump universe by the
regime-weighted blended score (futures_fund.blended_score), applies the hysteresis rotation vs the
held book, structures the NEW legs (ATR stop, >=2.2R TP), and writes state/cycle/N/cio.json +
proposals.json so the deterministic gate can execute. The selection is deterministic and tested, so
the book can no longer drift inverted or go flat. Agents still run for the news risk-off flag / pump
veto; the gate still owns all sizing and risk.

Usage: uv run python scripts/blended_book_cli.py --cycle N [--n-per-side 3] [--state state]
"""
import argparse
import json
import os

from futures_fund import blended_score as bs


def _raw(sym: str) -> str:
    """context briefs use 'BTC/USDT:USDT'; positions/proposals use 'BTCUSDT'."""
    return sym.split("/")[0] + "USDT" if "/" in sym else sym


def _structure(brief: dict, direction: str, *, rr1: float = 2.2, rr2: float = 3.5,
               atr_mult: float = 2.0) -> dict:
    entry = float(brief["last_close"])
    atr = float(brief["atr"])
    risk = atr_mult * atr
    if direction == "long":
        stop = entry - risk
        tps = [round(entry + rr1 * risk, 8), round(entry + rr2 * risk, 8)]
    else:
        stop = entry + risk
        tps = [round(entry - rr1 * risk, 8), round(entry - rr2 * risk, 8)]
    return {"entry": round(entry, 8), "stop": round(stop, 8), "take_profits": tps,
            "atr": round(atr, 8)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycle", type=int, required=True)
    ap.add_argument("--n-per-side", type=int, default=3)
    ap.add_argument("--state", default="state")
    ap.add_argument("--keep-buffer", type=int, default=2)   # stickier book = minimum rebalance
    ap.add_argument("--swap-margin", type=float, default=0.5)
    args = ap.parse_args()

    cdir = os.path.join(args.state, "cycle", str(args.cycle))
    ctx = json.load(open(os.path.join(cdir, "context.json")))
    briefs = [{**b, "symbol": _raw(b["symbol"])} for b in ctx["briefs"]]
    by_sym = {b["symbol"]: b for b in briefs}

    positions = json.load(open(os.path.join(args.state, "positions.json"))) \
        if os.path.exists(os.path.join(args.state, "positions.json")) else []
    holdings = {p["symbol"]: p["direction"] for p in positions}

    scored = bs.composite_scores(briefs)
    weights = scored[0]["weights"] if scored else {}
    plan = bs.apply_hysteresis(scored, holdings, n_per_side=args.n_per_side,
                               keep_buffer=args.keep_buffer, swap_margin=args.swap_margin)

    # Build proposals (new opens) + management (close rotated-out, hold kept) + cio allocations.
    score_of = {s["symbol"]: s for s in scored}
    proposals, allocations, management = [], [], []

    def _conv(sym):
        if sym not in score_of:
            return 0.5
        return round(min(0.9, 0.4 + abs(score_of[sym]["score"]) * 0.2), 3)

    for sym in plan["open_long"] + plan["open_short"]:
        direction = "long" if sym in plan["open_long"] else "short"
        st = _structure(by_sym[sym], direction)
        sc = score_of[sym]
        side = "LONG top" if direction == "long" else "SHORT bottom"
        thesis = (f"BLENDED score {sc['score']:+.2f} (mom_z {sc['components']['mom']:+.2f}, "
                  f"carry_z {sc['components']['carry']:+.2f}, mr_z {sc['components']['mr']:+.2f}; "
                  f"weights {weights}). {side}-sleeve.")
        proposals.append({
            "symbol": sym, "direction": direction, **st, "confidence": _conv(sym),
            "horizon_hours": 8, "confirmation": False, "risk_mult": 1.0,
            "rationale": thesis,
            "falsifiable_prediction": (
                f"{sym} {'out' if direction == 'long' else 'under'}-performs the opposite sleeve "
                f"over 2 cycles; invalidated if its blended score crosses to the other side.")})
        allocations.append({"symbol": sym, "direction": direction, "desk": "blended",
                            "conviction": _conv(sym), "risk_budget_frac": 0.9,
                            "entry_style": "market", "thesis": thesis,
                            "falsifiable_prediction": "blended score holds its sleeve 2 cycles."})

    for sym in plan["keep_long"] + plan["keep_short"]:
        direction = holdings[sym]
        ksc = score_of.get(sym, {}).get("score", 0)
        management.append({"symbol": sym, "action": "hold",
                           "note": f"keep — score {ksc:+.2f} still on the {direction} side "
                                   f"(min-rebalance hysteresis)."})
        allocations.append({"symbol": sym, "direction": direction, "desk": "blended",
                            "conviction": _conv(sym), "risk_budget_frac": 0.9,
                            "entry_style": "market",
                            "thesis": f"HOLD {direction} — blended score keeps it on its sleeve.",
                            "falsifiable_prediction": "score holds its sleeve."})

    for sym in plan["close"]:
        csc = score_of.get(sym, {}).get("score", 0)
        management.append({"symbol": sym, "action": "close",
                           "note": f"rotate out — score {csc:+.2f} crossed off the "
                                   f"{holdings.get(sym, '?')} side."})

    n_long = len(plan["keep_long"]) + len(plan["open_long"])
    n_short = len(plan["keep_short"]) + len(plan["open_short"])
    flat_verdicts = []
    booked = set(plan["keep_long"] + plan["keep_short"] + plan["open_long"] + plan["open_short"])
    for s in scored:
        if s["symbol"] not in booked:
            flat_verdicts.append({"symbol": s["symbol"],
                                  "reason": f"mid-rank score {s['score']:+.2f} (neither sleeve).",
                                  "edge_aligned": False, "favored_side": "none"})

    cio = {"allocations": allocations, "intraday_budget_frac": 0.0, "hot_list": [],
           "flat_verdicts": flat_verdicts}
    props = {"proposals": proposals, "management": management,
             "triggers": [], "cancel_triggers": []}
    json.dump(cio, open(os.path.join(cdir, "cio.json"), "w"), indent=2)
    json.dump(props, open(os.path.join(cdir, "proposals.json"), "w"), indent=2)

    print(json.dumps({
        "weights": {k: round(v, 2) for k, v in weights.items()},
        "ranking": [{"sym": s["symbol"], "score": round(s["score"], 2)} for s in scored],
        "plan": plan,
        "target_book": {"n_long": n_long, "n_short": n_short,
                        "long": plan["keep_long"] + plan["open_long"],
                        "short": plan["keep_short"] + plan["open_short"]},
        "wrote": [f"{cdir}/cio.json", f"{cdir}/proposals.json"],
    }, indent=2))


if __name__ == "__main__":
    main()
