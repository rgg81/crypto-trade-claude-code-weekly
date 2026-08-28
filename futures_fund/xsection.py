"""Cross-sectional factor book (NON-PROTECTED). The desk's selection + weighting layer.

WHY THIS REPLACES THE 3-LEG BLENDED BOOK. Measured over 230 perps x 2190 4h bars
(research/README.md and research/xsection_lab.py), the old desk's problem was CONCENTRATION, not a
missing signal. Holding the top-3/bottom-3 of a small universe is a punt on the two most extreme
names — in crypto, the most volatile ones — which is why signals with clearly positive information
coefficient still produced losing books. Same momentum signal, only the leg count changed:

    legs/side    3      5      8     12     20     25     87
    sharpe      1.19   1.57   1.75   1.90   1.95   2.06   2.15
    max DD     35.3%  26.1%  21.4%  18.5%  13.6%  12.6%  12.8%

`IR = IC * sqrt(breadth)`, measured. The 3-leg book's 35% drawdown would trip the -15% force-flatten
repeatedly; at 20 legs/side the same signal draws down 13.6%.

CONSTRUCTION (each piece earns its place):
  * momentum over MOM_LOOKBACK bars, z-scored ACROSS the cross-section (clipped, so one lunatic
    name cannot dominate the book);
  * inverse-volatility leg scaling, so a leg's RISK contribution is what is equalised, not its
    dollars;
  * ITERATED beta-neutralisation — one projection pass is not enough because the re-centring that
    follows re-introduces beta (realised beta only fell from -0.271 to -0.052 once iterated);
  * a per-name weight cap;
  * truncation to the strongest N per side. This is what makes the book fit the gate's heat/dust
    budget AND, unexpectedly, it IMPROVES neutrality: net beta -0.010 at 20/side versus -0.140 for
    the untruncated book, because the extreme-signal names carry less beta than the long tail of
    small weights. So no separate market-hedge leg is needed and the book stays dollar-neutral.

Everything here is pure and offline: it takes closes, it returns weights. It never sizes, never
vetoes, and never talks to the exchange — the deterministic gate remains the sole risk authority.
"""
from __future__ import annotations

import statistics

# 150 x 4h = 25 days. A broad plateau (30..400 all work), not a knife edge.
MOM_LOOKBACK = 150
VOL_LOOKBACK = 30
BETA_LOOKBACK = 180
MAX_NAME_FRAC = 0.05
# TRADEABILITY FLOORS. Measured: applying both LIFTS the edge (Sharpe 3.24 -> 3.28 on the vol floor
# alone) — the factor does NOT depend on the names they remove, and the desk's doctrine refuses
# them anyway. Do not tighten further: vol 1.5% / pump 30% collapses it to Sharpe 1.93 with a 21.4%
# drawdown, because over-filtering starves the cross-section of breadth.
VOL_FLOOR = 0.01          # 1% per-4h realised vol. Drops gold-backed tokens (PAXG/XAUT ~1.3% ATR),
                          # which Binance tags underlyingType=COIN so is_crypto_perp passes them,
                          # and whose near-zero vol earns a HUGE inverse-vol weight.
PUMP_CAP = 0.50           # |20-bar move| >= 50% is a parabolic blow-off (matches the old desk's
                          # PUMP_MOM_HARD), untradeable at size and it distorts the z-scores.
PUMP_LOOKBACK = 20
N_PER_SIDE = 20           # 40 legs: inside the caution-tier heat/dust budget (see book CLI).
BETA_ITERS = 5
Z_CLIP = 3.0


def returns(closes: list[float]) -> list[float]:
    """Simple bar-to-bar returns. Non-positive prices break the ratio, so they end the series."""
    out = []
    for a, b in zip(closes, closes[1:], strict=False):
        if a and a > 0 and b and b > 0:
            out.append(b / a - 1.0)
    return out


def momentum(closes: list[float], lookback: int = MOM_LOOKBACK) -> float | None:
    """Return over `lookback` bars. None (never 0.0) when history is short — a missing signal must
    not masquerade as a neutral one, or short-history names get ranked mid-pack."""
    if not closes or len(closes) <= lookback:
        return None
    a, b = closes[-1 - lookback], closes[-1]
    if not a or a <= 0:
        return None
    return b / a - 1.0


def realized_vol(closes: list[float], lookback: int = VOL_LOOKBACK) -> float | None:
    if not closes or len(closes) < lookback + 1:
        return None
    r = returns(closes[-(lookback + 1):])
    if len(r) < 8:
        return None
    sd = statistics.pstdev(r)
    return sd if sd > 1e-9 else None


def market_returns(series_by_sym: dict[str, list[float]]) -> list[float]:
    """Equal-weight cross-sectional return per bar — the market proxy beta is measured against."""
    rets = {s: returns(px) for s, px in series_by_sym.items()}
    n = min((len(r) for r in rets.values()), default=0)
    if not n:
        return []
    out = []
    for i in range(n):
        bar = [r[len(r) - n + i] for r in rets.values()]
        out.append(statistics.mean(bar) if bar else 0.0)
    return out


def beta_to_market(closes: list[float], mkt_rets: list[float],
                   lookback: int = BETA_LOOKBACK) -> float:
    """OLS beta of the name against the market proxy over the trailing window. Defaults to 1.0 when
    it cannot be estimated — the neutral assumption, never 0.0 (which would look market-immune)."""
    r = returns(closes)
    n = min(len(r), len(mkt_rets), lookback)
    if n < 30:
        return 1.0
    y = r[-n:]
    x = mkt_rets[-n:]
    mx = statistics.mean(x)
    my = statistics.mean(y)
    var = statistics.pvariance(x)
    if var <= 1e-14:
        return 1.0
    cov = sum((a - mx) * (b - my) for a, b in zip(x, y, strict=False)) / n
    return cov / var


def zscore_map(vals: dict[str, float | None], clip: float = Z_CLIP) -> dict[str, float]:
    live = {k: v for k, v in vals.items() if v is not None}
    if len(live) < 8:
        return {}
    m = statistics.mean(live.values())
    sd = statistics.pstdev(live.values())
    if sd <= 1e-12:
        return {}
    return {k: max(-clip, min(clip, (v - m) / sd)) for k, v in live.items()}


def _demean(w: dict[str, float]) -> dict[str, float]:
    if not w:
        return w
    m = sum(w.values()) / len(w)
    return {k: v - m for k, v in w.items()}


def _beta_neutralise(w: dict[str, float], betas: dict[str, float]) -> dict[str, float]:
    if not w:
        return w
    den = sum(betas.get(k, 1.0) ** 2 for k in w)
    if den <= 1e-12:
        return w
    lam = sum(w[k] * betas.get(k, 1.0) for k in w) / den
    return {k: v - lam * betas.get(k, 1.0) for k, v in w.items()}


def _normalise(w: dict[str, float]) -> dict[str, float]:
    tot = sum(abs(v) for v in w.values())
    if tot <= 1e-12:
        return {}
    return {k: v / tot for k, v in w.items()}


def cross_sectional_weights(series_by_sym: dict[str, list[float]], *,
                            n_per_side: int = N_PER_SIDE,
                            mom_lookback: int = MOM_LOOKBACK,
                            vol_lookback: int = VOL_LOOKBACK,
                            beta_lookback: int = BETA_LOOKBACK,
                            max_name_frac: float = MAX_NAME_FRAC,
                            beta_iters: int = BETA_ITERS,
                            vol_floor: float | None = None,
                            pump_cap: float | None = None) -> dict[str, float]:
    """Signed weights summing to 0 (dollar-neutral) with sum|w| == 1.

    Returns {} rather than a lopsided book when the cross-section is too thin to fill both sides —
    a half-built factor book is a directional bet, which is the one thing this desk must never be.
    """
    usable = {}
    for s, px in series_by_sym.items():
        if momentum(px, mom_lookback) is None:
            continue
        v = realized_vol(px, vol_lookback)
        if not v or (vol_floor is not None and v < vol_floor):
            continue
        if pump_cap is not None:
            m = momentum(px, PUMP_LOOKBACK)
            if m is not None and abs(m) >= pump_cap:
                continue
        usable[s] = px
    if len(usable) < 2 * n_per_side:
        return {}
    z = zscore_map({s: momentum(px, mom_lookback) for s, px in usable.items()})
    if len(z) < 2 * n_per_side:
        return {}
    w = _demean(z)
    w = _demean({s: v / realized_vol(usable[s], vol_lookback) for s, v in w.items()})
    mkt = market_returns(usable)
    betas = {s: beta_to_market(usable[s], mkt, beta_lookback) for s in w}
    for _ in range(max(0, beta_iters)):
        w = _demean(_beta_neutralise(w, betas))
    # Select by RANK, not by sign. Requiring n strictly-positive and n strictly-negative weights is
    # brittle: after de-meaning and beta-neutralisation a narrow cross-section need not split
    # evenly,
    # and the book then silently comes back EMPTY (a 12-name universe at 6/side produced no book at
    # all). The n most-favoured names are the long sleeve and the n least-favoured are the short
    # sleeve whatever the sign happens to be at the boundary.
    ordered = sorted(w.items(), key=lambda kv: kv[1])
    if len(ordered) < 2 * n_per_side:
        return {}
    shorts = [(v, s) for s, v in ordered[:n_per_side]]
    longs = [(v, s) for s, v in ordered[-n_per_side:]]
    book = {s: v for v, s in longs + shorts}
    # Each sleeve carries exactly half the gross, so the book is EXACTLY dollar-neutral after
    # truncation (truncating a demeaned book does not preserve the zero sum), and the per-name cap
    # is applied by WATER-FILLING: capping then re-normalising would just push capped names back
    # over the cap, so the excess is redistributed among the uncapped legs until it settles.
    long_syms = {s for _, s in longs}
    out: dict[str, float] = {}
    for side, members in ((1.0, long_syms), (-1.0, set(book) - long_syms)):
        # rank within the sleeve carries the size; the raw sign at the boundary does not.
        legs = {s: abs(book[s]) for s in members}
        if not any(legs.values()):
            legs = dict.fromkeys(members, 1.0)      # degenerate: equal-weight rather than nothing
        out.update(_waterfill_side(legs, 0.5, max_name_frac, side))
    return out


def _waterfill_side(legs: dict[str, float], budget: float, cap: float,
                    side: float) -> dict[str, float]:
    """Distribute `budget` across `legs` proportionally, honouring a per-name `cap`."""
    if not legs:
        return {}
    cap = max(cap, budget / len(legs))     # an unreachable cap must not deadlock the fill
    free = dict(legs)
    fixed: dict[str, float] = {}
    for _ in range(len(legs) + 1):
        tot = sum(free.values())
        rem = budget - sum(fixed.values())
        if tot <= 1e-12 or rem <= 1e-12:
            break
        scaled = {s: v / tot * rem for s, v in free.items()}
        over = {s: v for s, v in scaled.items() if v > cap + 1e-12}
        if not over:
            fixed.update(scaled)
            break
        for s in over:
            fixed[s] = cap
            free.pop(s, None)
    return {s: v * side for s, v in fixed.items()}
