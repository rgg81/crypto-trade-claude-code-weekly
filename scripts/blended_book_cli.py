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
    # DEPLOYMENT top-up: a kept leg more than this fraction below its per-leg target notional is
    # fraction below the achievable book gross B that triggers a COORDINATED refill. 1.0 disables.
    ap.add_argument("--resize-band", type=float, default=0.15)
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

    # DEPLOYMENT TOP-UP: grow frozen-undersized KEPT legs toward the per-leg target (~equity/2 per
    # side) by CLOSE+REOPEN — held legs can't pyramid, so the only gate-respecting way to enlarge a
    # leg is to close it and reopen it fresh at target (cycle.py's explicit-review path opens a
    # re-proposal on a force-closed symbol). The pre-sizer then sizes the reopen to fill the target.
    equity = float(ctx.get("equity") or 0.0)
    notional_by_sym, stop_frac_by_sym = {}, {}
    for p in positions:
        b = by_sym.get(p["symbol"])
        if b and b.get("last_close"):
            notional_by_sym[p["symbol"]] = p["qty"] * float(b["last_close"])
            if p.get("entry"):
                stop_frac_by_sym[p["symbol"]] = abs(p["entry"] - p["stop"]) / p["entry"]
    # per_trade_risk cap for the leg's risk-mult=1 ceiling — book-level regime quadrant (healthy
    # tier) is a good proxy; the gate still re-clamps each leg by its own regime, this only decides
    # which legs are worth a resize (skip those already at their wide-stop ceiling -> no churn).
    quad = (ctx.get("regime_state") or {}).get("quadrant")
    ptr = {"low_vol_trend": 0.015, "high_vol_trend": 0.010, "low_vol_range": 0.010,
           "high_vol_range": 0.005, "transition": 0.005}.get(quad, 0.010)
    kept_now = {s: holdings[s] for s in plan["keep_long"] + plan["keep_short"]}
    resize = bs.deployment_resizes(kept_now, notional_by_sym, equity, args.n_per_side,
                                   band=args.resize_band, per_trade_risk_pct=ptr,
                                   stop_frac_by_sym=stop_frac_by_sym)
    for sym in resize:                                  # kept-but-undersized -> close + reopen
        (plan["keep_long"] if holdings[sym] == "long" else plan["keep_short"]).remove(sym)
        (plan["open_long"] if holdings[sym] == "long" else plan["open_short"]).append(sym)
        plan["close"].append(sym)

    # MAKE ROOM: a side gaining a net-new leg must re-water-fill (else the new leg dust-drops -> the
    # persistent L2/S3). Close+reopen the kept legs on that side so all its legs share the budget.
    resize |= bs.make_room_for_adds(plan, holdings)

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
        if sym in resize:
            note = (f"RESIZE {holdings.get(sym, '?')} — kept by score {csc:+.2f} but undersized; "
                    f"close+reopen at the per-leg target to fill toward ~1x deployment.")
        else:
            note = (f"rotate out — score {csc:+.2f} crossed off the {holdings.get(sym, '?')} side.")
        management.append({"symbol": sym, "action": "close", "note": note})

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
        "resize": sorted(resize),
        "target_book": {"n_long": n_long, "n_short": n_short,
                        "long": plan["keep_long"] + plan["open_long"],
                        "short": plan["keep_short"] + plan["open_short"]},
        "wrote": [f"{cdir}/cio.json", f"{cdir}/proposals.json"],
    }, indent=2))


if __name__ == "__main__":
    main()
