"""Dollar-neutral pre-size + balance pass (NON-protected). Runs PRE-gate in
orchestration.gate_execute_step on TradeProposal objects, BETWEEN trade_props assembly and the
protected execute_proposals — so cycle.py / risk_gate.py stay byte-stable.

ONE merged computation (pre-size and balance are the same step): bucket the new proposals
long/short, work out the balanced gross BOTH sides can muster toward ~equity/2 each (netting any
held notional), assign each leg a target notional, and stamp `risk_mult` so the gate's
fixed-fractional sizer lands the leg at that notional. The gate clamps risk_mult to (0,1] (so this
can only ever SHRINK), and choose_leverage searches leverage DOWN to the liq floor, so realized
leverage emerges at ~1x. Trim-not-veto: if one side is scarce we balance to the smaller achievable
gross (a 3-long/1-short book is fine — equal $, not equal count); we VETO (drop everything) only
when a side is truly empty AND unheld, so the desk never runs a one-sided book.

NOTE: balance equalizes TARGET notionals. When legs cap-clamp asymmetrically (very different stops
or 1-vs-many legs), realized gross can drift mildly off net~=0; the post-gate exposure telemetry
(_TILT_WARN=0.30) is the canary, and the next cycle rebalances. This layer never sizes or vetoes a
single trade's risk — the protected gate remains the sole per-trade risk authority.
"""
from __future__ import annotations

from futures_fund.notional_sizing import notional_to_risk_pct


def presize_and_balance(props, *, equity, per_trade_risk_pct, held_long=0.0, held_short=0.0,
                        gross_target=None, max_name_frac=0.25, risk_pct_by_symbol=None):
    """Stamp risk_mult on each new TradeProposal to target a dollar-neutral book.

    `props` — new TradeProposal objects (each carries direction/entry/stop/risk_mult).
    `gross_target` — total gross to deploy (default = equity, i.e. ~1x); each side targets half.
    `held_long`/`held_short` — current gross long/short $ already on the book (netted into targets).
    `max_name_frac` — PER-NAME CAP: no single leg's target may exceed this fraction of gross_target
    (default 0.25 = 25% of the book), so a scarce side can't be filled by one oversized leg into a
    squeeze-prone name (the N4 short-sleeve-concentration guard). A side with more legs than the cap
    allows simply equal-splits below it.
    Returns (kept_props, summary): kept_props are copies with risk_mult set (zero-target legs
    dropped); summary reports the per-side targets and the balanced gross.
    """
    gross_target = equity if gross_target is None else gross_target
    side_target = max(0.0, gross_target) / 2.0
    name_cap = max(0.0, max_name_frac) * gross_target
    longs = [p for p in props if p.direction == "long"]
    shorts = [p for p in props if p.direction == "short"]

    # SOFT neutrality (R-ARCH-2): size EACH side independently toward ~equity/2 (net of held), so a
    # BALANCED submission lands dollar-neutral by construction (gross_long$ == gross_short$). A
    # one-sided submission still deploys (the gate never blocks it) but the post-gate canary
    # (_TILT_WARN=0.30) flags the drift and the next cycle rebalances — the CIO owns submitting a
    # balanced book; the gate owns per-trade safety. No hard veto here.
    # a side with no NEW legs cannot add notional (its target is just its held gross)
    tgt_long_total = max(0.0, side_target - held_long) if longs else 0.0
    tgt_short_total = max(0.0, side_target - held_short) if shorts else 0.0
    # equal-split per side, then clamp each leg to the per-name cap (N4: no single name dominates)
    per_long = min(tgt_long_total / len(longs), name_cap) if longs else 0.0
    per_short = min(tgt_short_total / len(shorts), name_cap) if shorts else 0.0

    kept = []
    for p in longs:
        _stamp(kept, p, per_long, equity, _ptr(p, per_trade_risk_pct, risk_pct_by_symbol))
    for p in shorts:
        _stamp(kept, p, per_short, equity, _ptr(p, per_trade_risk_pct, risk_pct_by_symbol))

    # actual per-side targets AFTER the per-name cap (per_leg may be capped below the equal split)
    gross_long_target = held_long + per_long * len(longs)
    gross_short_target = held_short + per_short * len(shorts)
    balanced = min(gross_long_target, gross_short_target)
    summary = {
        "gross_long_target": gross_long_target,
        "gross_short_target": gross_short_target,
        "balanced_gross": balanced,
        "n_long": len(longs), "n_short": len(shorts),
        "n_kept": len(kept), "n_dropped": len(props) - len(kept),
    }
    return kept, summary


def _ptr(p, default_ptr, by_symbol):
    """Each leg's per_trade_risk_pct: the gate sizes every proposal with ITS OWN symbol's regime
    caps (cycle.execute_proposals: regime=simple_regime(frames[unified])), so the pre-sizer must use
    the same per-leg value or the risk_mult is mis-calibrated (a leg whose regime cap differs from
    the batch's sizes off-target and blows the per-name cap). Falls back to the uniform value."""
    if by_symbol:
        v = by_symbol.get(getattr(p, "symbol", None))
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return default_ptr


def _stamp(kept, p, per_leg_notional, equity, per_trade_risk_pct):
    if per_leg_notional <= 0 or per_trade_risk_pct <= 0:
        return  # zero-target (vetoed / scarce side) -> drop the leg, never submit a one-sided book
    rm = notional_to_risk_pct(per_leg_notional, p.entry, p.stop, equity) / per_trade_risk_pct
    rm = max(0.0, min(1.0, rm))
    if rm <= 0:
        return
    kept.append(p.model_copy(update={"risk_mult": rm}))
