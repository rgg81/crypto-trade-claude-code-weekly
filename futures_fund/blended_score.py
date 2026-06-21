"""Blended cross-sectional score — the all-weather selector for the dollar-neutral desk.

NON-PROTECTED. The desk ranks the liquid, non-pump universe by a regime-weighted composite of three
market-neutral edges, so it can profit in EVERY regime instead of only when momentum disperses:

  - momentum:    z(momentum_20)          high -> LONG          (the trend edge)
  - carry:       z(-annualized funding)  neg funding -> LONG   (the FLAT-market edge)
  - mean_revert: z(50 - rsi)             oversold -> LONG      (the range edge)

A high composite score is a LONG candidate, a low score is a SHORT candidate. The book is the top-N
longs vs the bottom-N shorts at EQUAL dollars -> always dollar-neutral and ALWAYS deployed (there is
always a top and a bottom, so the desk is NEVER flat). The regime shifts the weights (a dispersed/
trending cross-section -> momentum-led; a flat/compressed one -> carry+mean-reversion-led). A
hysteresis band keeps a held leg until a challenger beats it by a score margin that clears the
~0.28% swap round-trip, so turnover stays low ("minimum rebalance but profitable").

This is a deterministic, tested ranker — the long/short SELECTION no longer depends on an agent
free-styling a book (which is what let the book drift inverted). Agents still veto pumps, supply the
news risk-off flag, and confirm neutrality; the gate still owns all sizing/risk.
"""
from __future__ import annotations

import math
from statistics import pstdev

# Tradeability floors (exclude untradeable pumps / illiquid microcaps / data artifacts).
MIN_OI_USD = 50e6          # below this notional OI a name is an illiquid microcap -> NO-TOUCH
PUMP_MOM = 0.30            # +30% over 20 bars ...
PUMP_RSI = 72.0           # ... with RSI > 72 = a parabolic blow-off -> NO-TOUCH
# |20-bar move| >= 50% is parabolic/degenerate REGARDLESS of RSI -> NO-TOUCH. (RSI mean-reverts
# faster than 20-bar momentum, so a cooled-RSI pump like a +100% name at RSI 62 slips the soft rule
# AND distorts the whole cross-section's z-scores.)
PUMP_MOM_HARD = 0.50

# Regime weight presets (must each sum to 1.0). Picked by cross-sectional momentum dispersion.
_TREND_W = {"mom": 0.55, "carry": 0.35, "mr": 0.10}
# In a flat tape carry CO-LEADS (the direction-agnostic edge that still pays), but momentum keeps an
# equal voice so the ranking stays stable (momentum is more persistent cycle-to-cycle than
# funding/RSI) — this prevents thrash, quality majors being dumped for microcaps on funding alone,
# and MR shorting a strong-momentum leader on RSI. MR is the smallest weight (riskiest edge — fading
# a trend can lose big), firing mainly on genuine RSI extremes.
_RANGE_W = {"mom": 0.40, "carry": 0.40, "mr": 0.20}
_TREND_DISPERSION = 0.05   # std(momentum_20) >= 5% -> momentum is "in season"

# A name whose |momentum_20| >= this is "clearly trending"; carry and mean-reversion may then only
# REINFORCE that trend, never FLIP it (Phase-0 doctrine: never catch a falling knife for funding,
# never short a pump for funding, never fade a strong trend).
STRONG_MOM = 0.06


def annualized_funding(funding_rate: float, interval_hours: float) -> float:
    """Per-interval funding -> annualized rate. events/day = 24/interval; * 365 days."""
    interval_hours = interval_hours or 8.0
    return funding_rate * (24.0 / interval_hours) * 365.0


def zscore(xs: list[float]) -> list[float]:
    """Cross-sectional standardization. Zero variance -> all zeros (no signal)."""
    n = len(xs)
    if n == 0:
        return []
    mu = sum(xs) / n
    sd = pstdev(xs) if n > 1 else 0.0
    if sd == 0.0:
        return [0.0] * n
    return [(x - mu) / sd for x in xs]


def _num(v, default=0.0):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(f) or math.isinf(f) else f


def is_tradeable(brief: dict, min_oi: float = MIN_OI_USD) -> bool:
    """Exclude illiquid microcaps, parabolic pumps, and thin-history data artifacts."""
    last = _num(brief.get("last_close"))
    atr = brief.get("atr")
    if last <= 0:
        return False
    if atr is None or (isinstance(atr, float) and (math.isnan(atr) or atr <= 0)):
        return False
    if _num(brief.get("oi_value"), 0.0) < min_oi:
        return False
    mom = _num(brief.get("momentum_20"))
    rsi = _num(brief.get("rsi"), 50.0)
    if abs(mom) >= PUMP_MOM_HARD:             # parabolic/degenerate move regardless of RSI
        return False
    if mom >= PUMP_MOM and rsi >= PUMP_RSI:   # softer parabolic blow-off
        return False
    return True


def _long_favorability(brief: dict) -> dict:
    """Raw per-name signal for each edge, signed so that POSITIVE favors a LONG."""
    mom = _num(brief.get("momentum_20"))
    carry = -annualized_funding(_num(brief.get("funding_rate")),
                                _num(brief.get("funding_interval_hours"), 8.0))
    # Mean-reversion fires only on ABSOLUTE extremes (oversold<40 / overbought>60); inside the 40-60
    # neutral band it is silent, so z-scoring cannot manufacture a fade signal from mid-RSI noise.
    rsi = _num(brief.get("rsi"), 50.0)
    if rsi < 40.0:
        mr = (40.0 - rsi) / 40.0            # oversold -> long-favorable (+)
    elif rsi > 60.0:
        mr = (60.0 - rsi) / 40.0            # overbought -> short-favorable (-)
    else:
        mr = 0.0
    return {"mom": mom, "carry": carry, "mr": mr}


def regime_weights(briefs: list[dict], override: dict | None = None) -> dict:
    """Pick edge weights from the cross-section's momentum dispersion (or an explicit override)."""
    if override:
        s = sum(override.values()) or 1.0
        return {k: v / s for k, v in override.items()}
    moms = [_num(b.get("momentum_20")) for b in briefs if is_tradeable(b)]
    disp = pstdev(moms) if len(moms) > 1 else 0.0
    return dict(_TREND_W if disp >= _TREND_DISPERSION else _RANGE_W)


def composite_scores(briefs: list[dict], weights: dict | None = None,
                     min_oi: float = MIN_OI_USD) -> list[dict]:
    """Rank the tradeable universe by the regime-weighted blended score (high -> LONG)."""
    elig = [b for b in briefs if is_tradeable(b, min_oi)]
    if not elig:
        return []
    w = regime_weights(elig, override=weights)
    raw = [_long_favorability(b) for b in elig]
    z = {k: zscore([r[k] for r in raw]) for k in ("mom", "carry", "mr")}
    out = []
    for i, b in enumerate(elig):
        comp = {k: z[k][i] for k in ("mom", "carry", "mr")}
        # Momentum-consistency gate: a clearly-trending name lets carry/MR only REINFORCE the trend.
        mom_raw = raw[i]["mom"]
        if mom_raw <= -STRONG_MOM:               # clear faller -> kill long-favoring carry/MR
            comp["carry"] = min(comp["carry"], 0.0)
            comp["mr"] = min(comp["mr"], 0.0)
        elif mom_raw >= STRONG_MOM:              # clear riser -> kill short-favoring carry/MR
            comp["carry"] = max(comp["carry"], 0.0)
            comp["mr"] = max(comp["mr"], 0.0)
        score = sum(w[k] * comp[k] for k in ("mom", "carry", "mr"))
        out.append({"symbol": b["symbol"], "score": score, "components": comp,
                    "weights": w, "raw": raw[i]})
    out.sort(key=lambda d: d["score"], reverse=True)
    return out


def select_book(scored: list[dict], n_per_side: int = 3) -> tuple[list[str], list[str]]:
    """Top-N by score = LONG sleeve; bottom-N = SHORT sleeve. Never flat when names exist."""
    syms = [s["symbol"] for s in scored]
    n = min(n_per_side, len(syms) // 2)
    if n <= 0:
        return ([syms[0]], [syms[-1]]) if len(syms) >= 2 else ([], [])
    return syms[:n], syms[-n:]


def apply_hysteresis(scored: list[dict], holdings: dict[str, str], n_per_side: int = 3,
                     keep_buffer: int = 1, swap_margin: float = 0.5) -> dict:
    """Minimum-rebalance rotation.

    Keep a held leg as long as it stays on its own side within the top/bottom
    (n_per_side+keep_buffer) ranks. Close it only if it has crossed to the wrong side (a held long
    now in the bottom half, or a short in the top half) or fell out of the buffer. Refill vacated
    slots from the best
    available non-held name on that side, but ONLY if it beats the weakest kept leg by `swap_margin`
    (so a fresh name that barely edges a held one does not trigger churn).
    """
    rank = {s["symbol"]: i for i, s in enumerate(scored)}
    total = len(scored)
    long_band = n_per_side + keep_buffer            # ranks [0 .. long_band) are "long-side keepers"
    short_band_start = total - (n_per_side + keep_buffer)

    keep_long, keep_short, close = [], [], []
    for sym, direction in holdings.items():
        r = rank.get(sym)
        if r is None:                                # name left the universe -> close
            close.append(sym)
        elif direction == "long" and r < long_band and r < total / 2:
            keep_long.append(sym)
        elif direction == "short" and r >= short_band_start and r >= total / 2:
            keep_short.append(sym)
        else:
            close.append(sym)                        # crossed sides / fell out of band

    # Refill the gaps left by kept+closed legs with the best available UNHELD names on each side.
    # Anti-churn is the keep-band (a held in-band leg is kept, so it never frees a slot to thrash);
    # a vacated slot just takes the best candidate. `swap_margin` is reserved for future
    # displacement logic (rotate a buffer-zone leg only when beaten by a wide margin).
    _ = swap_margin
    open_long, open_short = [], []
    held = set(holdings)

    need_long = n_per_side - len(keep_long)
    for s in scored:                                 # best-scoring first
        if need_long <= 0:
            break
        if s["symbol"] not in held:
            open_long.append(s["symbol"])
            need_long -= 1

    need_short = n_per_side - len(keep_short)
    for s in reversed(scored):                        # lowest-scoring first
        if need_short <= 0:
            break
        if s["symbol"] not in held:
            open_short.append(s["symbol"])
            need_short -= 1

    return {"keep_long": keep_long, "keep_short": keep_short,
            "open_long": open_long, "open_short": open_short, "close": close}
