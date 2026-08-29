"""Cross-sectional factor book CLI (NON-PROTECTED) — the desk's selection + weighting stage.

Replaces the 3-leg blended book. Measured over 230 perps x 2190 4h bars, the old desk's defect was
CONCENTRATION: the same momentum signal returns Sharpe 1.19 with a 35% drawdown at 3 legs/side and
Sharpe 3.24 with a 13.5% drawdown at 20/side (research/README.md). This builds the wide book.

It is SELF-CONTAINED on purpose: it reads the scout universe and pulls its own 4h klines through the
local proxy (cached), computing momentum / ATR / vol / beta itself. That keeps it independent of the
12-name brief pipeline, which cannot supply 150 bars of history for 100 names.

It proposes in PRICE terms only. The deterministic gate remains the sole risk authority: it owns
sizing, leverage, liq distance, RR, heat and the breakers. `risk_mult` here is the gate's
shrink-only (0,1] multiplier, used to express each leg's RELATIVE weight — never to add risk.
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request

from futures_fund.blended_score import admissible_stop_frac
from futures_fund.xsection import (
    MOM_LOOKBACK,
    PUMP_CAP,
    VOL_FLOOR,
    cross_sectional_weights,
)

PROXY = os.environ.get("BINANCE_KLINES_PROXY", "http://127.0.0.1:8000")
BARS = 300                # >= MOM_LOOKBACK + BETA window headroom
ATR_LEN = 14
ATR_MULT = 2.0
# Take-profits sit FAR out (6R/10R). The gate demands RR >= 2 on every proposal, but a factor leg
# should be closed by the SIGNAL leaving its sleeve, not by a price target — a near TP would
# truncate winners and quietly convert the book into something else.
RR1, RR2 = 6.0, 10.0
# Stricter of long/short, derived from the gate itself — never hardcoded.
_STOP_CEILING = admissible_stop_frac(None)


# consolidate() drops any leg whose risk falls below this fraction of equity, SILENTLY.
DUST_FRAC = 0.001
MIN_N_PER_SIDE = 3
# Headroom over the dust floor. consolidate() scales the whole batch to the heat cap BEFORE the
# floor is applied, so sizing to exactly max_heat/dust legs leaves no margin and any scaling deletes
# the marginal ones. cy360 asked for 40 legs into a 40-leg budget and executed 31.
DUST_HEADROOM = 2.0


def safe_n_per_side(requested: int, *, max_heat: float, dust_frac: float = DUST_FRAC) -> int:
    """Largest legs-per-side the gate's heat budget can carry ABOVE the dust floor.

    The gate spreads `max_heat` of stop-risk across the whole book; consolidate() then deletes any
    leg under `dust_frac` of equity WITHOUT SAYING SO. So a book of more than heat/dust legs does
    not merely get trimmed, it silently loses legs and comes out lopsided — the dust-drop family
    that has bitten this desk repeatedly. Clamp to the budget, floor at MIN_N_PER_SIDE so the desk
    is never flat (mandate), and never exceed what was asked for.
    """
    if dust_frac <= 0:
        return max(MIN_N_PER_SIDE, requested)
    affordable = int(max(0.0, max_heat) / (dust_frac * DUST_HEADROOM)) // 2
    return max(MIN_N_PER_SIDE, min(int(requested), affordable))


def stop_ok(stop_frac: float, ceiling: float) -> bool:
    """Can the gate actually OPEN a leg with this stop width?

    `risk_gate.MIN_LIQ_DISTANCE_MULT` requires the liquidation price to sit at least 2.5 stops away,
    which caps the admissible stop at 40% long / 36.19% short at 1x. A wider leg is vetoed EVERY
    cycle, so the sleeve silently executes one leg short and the book goes lopsided. Reject it at
    selection instead — the next-ranked name takes the slot.
    """
    return 0.0 < stop_frac < ceiling


def fit_n_per_side(requested: int, *, priced: int, max_heat: float,
                   dust_frac: float = DUST_FRAC) -> int:
    """Legs per side that BOTH the gate's heat budget and the live universe can support.

    Two independent ceilings, and missing either one is a silent failure:
      * heat/dust  — too many legs and consolidate() deletes the small ones without a word;
      * universe   — cross_sectional_weights refuses to build a LOPSIDED book, so asking for more
        legs than the cross-section can fill returns nothing at all and the desk holds indefinitely.
    """
    by_heat = safe_n_per_side(requested, max_heat=max_heat, dust_frac=dust_frac)
    by_universe = max(MIN_N_PER_SIDE, int(priced) // 2)
    return max(1, min(int(requested), by_heat, by_universe))


def _raw(sym: str) -> str:
    return sym.replace("/", "").replace(":USDT", "")


def _klines(symbol: str, limit: int = BARS, timeout: float = 20.0) -> list[list]:
    q = urllib.parse.urlencode({"symbol": symbol, "interval": "4h", "limit": limit})
    req = urllib.request.Request(f"{PROXY}/fapi/v1/klines?{q}",
                                 headers={"User-Agent": "tempest-xsection/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def atr(rows: list[list], length: int = ATR_LEN) -> float | None:
    """Wilder-style ATR from raw klines (high=2, low=3, close=4)."""
    if len(rows) < length + 2:
        return None
    trs = []
    for i in range(1, len(rows)):
        h, low = float(rows[i][2]), float(rows[i][3])
        pc = float(rows[i - 1][4])
        trs.append(max(h - low, abs(h - pc), abs(low - pc)))
    window = trs[-length:]
    return sum(window) / len(window) if window else None


def structure(entry: float, atr_v: float, direction: str) -> dict:
    risk = ATR_MULT * atr_v
    if direction == "long":
        stop = entry - risk
        tps = [round(entry + RR1 * risk, 8), round(entry + RR2 * risk, 8)]
    else:
        stop = entry + risk
        tps = [round(entry - RR1 * risk, 8), round(entry - RR2 * risk, 8)]
    return {"entry": round(entry, 8), "stop": round(stop, 8), "take_profits": tps,
            "atr": round(atr_v, 8)}


def risk_mults(weights: dict[str, float], stop_frac: dict[str, float]) -> dict[str, float]:
    """Map target WEIGHTS to the gate's risk_mult.

    The gate sizes by RISK, not notional: notional = (equity * ptr * rm) / stop_frac. So to land a
    notional proportional to the target weight, rm must be proportional to weight * stop_frac.
    Normalised PER SLEEVE so the largest leg on EACH side is exactly 1.0. Normalising across the
    whole book couples the sleeves: momentum is right-skewed, so the long sleeve carries larger |z|,
    every short gets a proportionally smaller rm, and the shorts are the legs that fall under the
    dust floor — 7 of the 9 legs lost at cy360 were shorts. Per-sleeve budgets stop one side's
    signal strength from starving the other. rm is clamped to (0,1] by the gate, so scaling any
    other way would also silently truncate the book's biggest position.
    """
    out: dict[str, float] = {}
    for side in (1.0, -1.0):
        legs = {s: abs(w) * stop_frac.get(s, 0.0)
                for s, w in weights.items() if w * side > 0}
        top = max(legs.values(), default=0.0)
        if top <= 0:
            continue
        out.update({s: max(1e-4, min(1.0, v / top)) for s, v in legs.items()})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycle", type=int, required=True)
    ap.add_argument("--state", default="state")
    ap.add_argument("--n-per-side", type=int, default=20)
    ap.add_argument("--rebalance-every", type=int, default=6)   # 6 x 4h = daily
    ap.add_argument("--drift-band", type=float, default=0.25)
    args = ap.parse_args()

    cdir = os.path.join(args.state, "cycle", str(args.cycle))
    os.makedirs(cdir, exist_ok=True)
    uni = json.load(open(os.path.join(cdir, "universe.json")))
    symbols = [_raw(u["symbol"]) for u in uni.get("universe", uni.get("candidates", []))]

    ppath = os.path.join(args.state, "positions.json")
    positions = json.load(open(ppath)) if os.path.exists(ppath) else []
    if isinstance(positions, dict):
        positions = positions.get("positions", [])
    holdings = {p["symbol"]: p["direction"] for p in positions}

    series, atrs, last = {}, {}, {}
    failed = []
    for sym in symbols:
        try:
            rows = _klines(sym)
        except Exception as exc:                       # noqa: BLE001 - one bad symbol must not kill the book
            failed.append(f"{sym}:{type(exc).__name__}")
            continue
        if len(rows) < MOM_LOOKBACK + 2:
            continue
        closes = [float(r[4]) for r in rows]
        a = atr(rows)
        if not a or a <= 0 or closes[-1] <= 0:
            continue
        if not stop_ok(ATR_MULT * a / closes[-1], _STOP_CEILING):
            continue                      # gate would veto this leg every cycle -> lopsided sleeve
        series[sym], atrs[sym], last[sym] = closes, a, closes[-1]

    # Clamp the book to what the gate's CURRENT heat budget can carry above the dust floor.
    n_side = args.n_per_side
    ctx_path = os.path.join(cdir, "context.json")
    heat_note = "heat budget unknown - using requested n_per_side"
    if os.path.exists(ctx_path):
        try:
            ctx = json.load(open(ctx_path))
            from futures_fund.models import PortfolioHealth, RegimeState
            from futures_fund.policy import caps_for
            dd = float(ctx.get("drawdown_from_peak") or 0.0)
            eq = float(ctx.get("equity") or 0.0)
            health = PortfolioHealth(equity=eq, peak_equity=eq / (1 - dd) if dd < 1 else eq,
                                     drawdown_from_peak=dd)
            quad = (ctx.get("regime_state") or {}).get("quadrant") or "low_vol_range"
            caps = caps_for(RegimeState(quadrant=quad, trend="up", vol="low"), health)
            n_side = fit_n_per_side(args.n_per_side, priced=len(series),
                                    max_heat=caps.max_heat)
            heat_note = (f"max_heat {caps.max_heat:.4f} @ {health.tier}, priced {len(series)} -> "
                         f"{n_side} legs/side (asked {args.n_per_side})")
        except Exception as exc:  # noqa: BLE001 - never let a caps read stop the book
            n_side = max(MIN_N_PER_SIDE, min(args.n_per_side, len(series) // 2))
            heat_note = f"caps read failed ({type(exc).__name__}) - universe-fitted {n_side}/side"
    else:
        n_side = max(MIN_N_PER_SIDE, min(args.n_per_side, len(series) // 2))
        heat_note = f"no context - universe-fitted {n_side}/side"

    weights = cross_sectional_weights(series, n_per_side=n_side,
                                      vol_floor=VOL_FLOOR, pump_cap=PUMP_CAP)
    rebalancing = (args.cycle % max(1, args.rebalance_every)) == 0

    proposals, allocations, management = [], [], []
    plan = {"target_long": [], "target_short": [], "open": [], "close": [], "hold": []}

    if weights and rebalancing:
        stop_frac = {s: ATR_MULT * atrs[s] / last[s] for s in weights}
        rm = risk_mults(weights, stop_frac)
        for sym, w in sorted(weights.items(), key=lambda kv: -abs(kv[1])):
            direction = "long" if w > 0 else "short"
            (plan["target_long"] if w > 0 else plan["target_short"]).append(sym)
            if holdings.get(sym) == direction:
                plan["hold"].append(sym)
                management.append({"symbol": sym, "action": "hold",
                                   "note": f"factor weight {w:+.4f} still on the {direction} side"})
                allocations.append({"symbol": sym, "direction": direction, "desk": "xsection",
                                    "conviction": round(min(0.9, 0.4 + abs(w) * 8), 3),
                                    "risk_budget_frac": 0.9, "entry_style": "market",
                                    "thesis": f"held factor leg, weight {w:+.4f}",
                                    "falsifiable_prediction": "leg stays in its sleeve"})
                continue
            plan["open"].append(sym)
            st = structure(last[sym], atrs[sym], direction)
            thesis = (f"cross-sectional momentum({MOM_LOOKBACK} bars) factor leg, target weight "
                      f"{w:+.4f} of gross; inverse-vol scaled, beta-neutralised.")
            proposals.append({"symbol": sym, "direction": direction, **st,
                              "confidence": round(min(0.9, 0.4 + abs(w) * 8), 3),
                              "horizon_hours": 24, "confirmation": False,
                              "risk_mult": round(rm.get(sym, 1.0), 4),
                              "rationale": thesis,
                              "falsifiable_prediction": (
                                  f"{sym} out-performs the opposite sleeve while it stays in the "
                                  f"top/bottom {n_side} by {MOM_LOOKBACK}-bar momentum.")})
            allocations.append({"symbol": sym, "direction": direction, "desk": "xsection",
                                "conviction": round(min(0.9, 0.4 + abs(w) * 8), 3),
                                "risk_budget_frac": 0.9, "entry_style": "market",
                                "thesis": thesis,
                                "falsifiable_prediction": "leg holds its sleeve"})
        for sym, d in holdings.items():
            want = weights.get(sym)
            if want is None or (want > 0) != (d == "long"):
                plan["close"].append(sym)
                management.append({"symbol": sym, "action": "close",
                                   "note": "left the factor book (momentum sleeve exit)"})
    else:
        for sym, d in holdings.items():
            plan["hold"].append(sym)
            management.append({"symbol": sym, "action": "hold",
                               "note": "off-rebalance cycle — factor book held"})
            allocations.append({"symbol": sym, "direction": d, "desk": "xsection",
                                "conviction": 0.5, "risk_budget_frac": 0.9,
                                "entry_style": "market", "thesis": "held (off-rebalance cycle)",
                                "falsifiable_prediction": "held"})

    cio = {"allocations": allocations, "intraday_budget_frac": 0.0, "hot_list": [],
           "flat_verdicts": []}
    props = {"proposals": proposals, "management": management, "triggers": [],
             "cancel_triggers": []}
    json.dump(cio, open(os.path.join(cdir, "cio.json"), "w"), indent=2)
    json.dump(props, open(os.path.join(cdir, "proposals.json"), "w"), indent=2)
    print(json.dumps({
        "universe": len(symbols), "priced": len(series), "failed": failed[:5],
        "rebalancing": rebalancing, "n_per_side": n_side, "heat": heat_note,
        "book": {"long": len(plan["target_long"]), "short": len(plan["target_short"])},
        "open": len(plan["open"]), "close": len(plan["close"]), "hold": len(plan["hold"]),
        "wrote": [f"{cdir}/cio.json", f"{cdir}/proposals.json"]}, indent=2))


if __name__ == "__main__":
    main()
