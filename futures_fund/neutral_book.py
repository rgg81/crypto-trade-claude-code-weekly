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
                        gross_target=None, max_name_frac=0.25, risk_pct_by_symbol=None,
                        heat_headroom_by_symbol=None, dust_risk_frac=0.001):
    """Stamp risk_mult on each new TradeProposal to target a dollar-neutral book.

    `props` — new TradeProposal objects (each carries direction/entry/stop/risk_mult).
    `gross_target` — total gross to deploy (default = equity, i.e. ~1x); each side targets half.
    `held_long`/`held_short` — current gross long/short $ already on the book (netted into targets).
    `max_name_frac` — PER-NAME CAP: no single leg's target may exceed this fraction of gross_target
    (default 0.25 = 25% of the book), so a scarce side can't be filled by one oversized leg into a
    squeeze-prone name (the N4 short-sleeve-concentration guard). A side with more legs than the cap
    allows simply equal-splits below it.
    `heat_headroom_by_symbol` — {raw symbol: available heat FRACTION} = max_heat(leg's OWN regime) -
    used held heat, mirroring the gate's per-leg clamp (risk_gate.evaluate: effective_risk_pct =
    min(risk_pct, max_heat - used_heat)). When supplied, each leg's target notional is capped to
    what the gate will actually let it deploy, a leg whose deployable notional falls below the
    consolidate dust floor (`dust_risk_frac`) is DROPPED (counted in `heat_dropped`, never silent),
    and the OPPOSITE side is symmetrically TRIMMED so the realized gross_long$ == gross_short$ stays
    balanced. This stops the cycle-2 failure: a balanced ~1x book saturates the heat cap, the gate
    clamps a strict-regime long to dust while a loose-regime short opens full, and the book flips
    net-short. Heat awareness keeps the book dollar-neutral under the heat ceiling. None =>
    heat-blind (legacy behavior, unchanged).
    Returns (kept_props, summary): kept_props are copies with risk_mult set (zero-target / dust /
    heat-starved legs dropped); summary reports the per-side targets, the balanced gross, and any
    heat-forced drops.
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

    # HEAT-AWARE deployable notional per leg (None => heat-blind: no cap, no drop)
    def _deployable(p, per_side):
        n = per_side
        if heat_headroom_by_symbol is not None:
            n = min(n, _heat_notional(p, equity, heat_headroom_by_symbol))
        return n

    def _is_dust(p, n):
        return heat_headroom_by_symbol is not None and n < _dust_notional(p, equity, dust_risk_frac)

    heat_dropped: list[str] = []
    long_legs = [(p, _deployable(p, per_long)) for p in longs]
    short_legs = [(p, _deployable(p, per_short)) for p in shorts]
    # drop legs the heat ceiling starves below the consolidate dust floor (the gate would size them
    # to dust and consolidate would drop them SILENTLY — surface it here and exclude them upstream)
    def _viable(side_legs):
        out = []
        for p, n in side_legs:
            if _is_dust(p, n):
                heat_dropped.append(getattr(p, "symbol", "?"))
            else:
                out.append((p, n))
        return out
    long_legs, short_legs = _viable(long_legs), _viable(short_legs)

    # SYMMETRIC TRIM so the realized finals balance: each side can only ADD what its viable legs can
    # deploy; trim the side that could add more to keep held_long+L_add == held_short+S_add. Only
    # applies when BOTH sides are PRESENT (held>0 or a viable new leg) — a deliberately one-sided
    # submission still deploys (soft neutrality; the post-gate canary flags it), never trimmed to
    # zero against an absent side.
    l_ach = sum(n for _, n in long_legs)
    s_ach = sum(n for _, n in short_legs)
    long_present = held_long > 0 or l_ach > 0
    short_present = held_short > 0 or s_ach > 0
    if heat_headroom_by_symbol is not None and long_present and short_present:
        final = min(held_long + l_ach, held_short + s_ach)
        l_add, s_add = max(0.0, final - held_long), max(0.0, final - held_short)
        l_scale = (l_add / l_ach) if l_ach > 0 else 0.0
        s_scale = (s_add / s_ach) if s_ach > 0 else 0.0
        long_legs = [(p, n * l_scale) for p, n in long_legs]
        short_legs = [(p, n * s_scale) for p, n in short_legs]

    kept = []
    for p, n in long_legs:
        _stamp(kept, p, n, equity, _ptr(p, per_trade_risk_pct, risk_pct_by_symbol))
    for p, n in short_legs:
        _stamp(kept, p, n, equity, _ptr(p, per_trade_risk_pct, risk_pct_by_symbol))

    if heat_headroom_by_symbol is not None:
        # actual per-side targets after heat-cap + symmetric trim
        gross_long_target = held_long + sum(n for _, n in long_legs)
        gross_short_target = held_short + sum(n for _, n in short_legs)
    else:
        # heat-blind: per-side targets after the per-name cap (legacy)
        gross_long_target = held_long + per_long * len(longs)
        gross_short_target = held_short + per_short * len(shorts)
    balanced = min(gross_long_target, gross_short_target)
    summary = {
        "gross_long_target": gross_long_target,
        "gross_short_target": gross_short_target,
        "balanced_gross": balanced,
        "n_long": len(longs), "n_short": len(shorts),
        "n_kept": len(kept), "n_dropped": len(props) - len(kept),
        "heat_dropped": heat_dropped,
    }
    return kept, summary


def _heat_notional(p, equity, heat_headroom_by_symbol):
    """Max notional the gate's per-leg heat clamp will allow this leg = headroom_frac * equity *
    |entry| / |entry-stop| (inverse of position_risk = qty*|entry-stop|/equity at notional
    qty*entry). Falls back to +inf when the leg has no headroom entry (treated as unconstrained)."""
    h = heat_headroom_by_symbol.get(getattr(p, "symbol", None))
    if not isinstance(h, (int, float)):
        return float("inf")
    dist = abs(p.entry - p.stop)
    if dist <= 0 or equity <= 0:
        return 0.0
    return max(0.0, h) * equity * abs(p.entry) / dist


def _dust_notional(p, equity, dust_risk_frac):
    """The notional below which consolidate drops a leg as dust (position_risk < dust_risk_frac)."""
    dist = abs(p.entry - p.stop)
    if dist <= 0 or equity <= 0:
        return 0.0
    return dust_risk_frac * equity * abs(p.entry) / dist


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
