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
                     keep_buffer: int = 2, swap_margin: float = 0.5) -> dict:
    """Minimum-rebalance rotation with a SWAP MARGIN (the core anti-churn mechanism).

    The target sleeves are the top-N (long) / bottom-N (short) by score, but a HELD leg is only
    rotated OUT when a challenger beats it by `swap_margin` in side-score terms (long: +score,
    short: −score) — so a compressed cross-section of near-tied scores does NOT churn the book. A
    held leg that has crossed to the OTHER sleeve is closed (it cannot same-cycle flip — the gate
    can't reliably close+reopen the same symbol opposite in one pass, so it re-enters next cycle).
    `keep_buffer` is reserved (the swap margin now provides stickiness, not a rank band).
    """
    _ = keep_buffer
    score = {s["symbol"]: s["score"] for s in scored}
    present = set(score)

    def side_val(sym: str, direction: str) -> float:
        return score[sym] if direction == "long" else -score[sym]

    close = [s for s in holdings if s not in present]      # left the universe

    def build(direction: str) -> list[str]:
        # candidates exclude names held on the OPPOSITE sleeve (no same-cycle flip)
        opp = "short" if direction == "long" else "long"
        cands = sorted([s for s in present if holdings.get(s) != opp],
                       key=lambda s: side_val(s, direction), reverse=True)
        held = sorted([s for s, d in holdings.items()
                       if d == direction and s in present and s not in close],
                      key=lambda s: side_val(s, direction), reverse=True)
        book = held[:n_per_side]                            # keep held legs first
        for c in cands:                                     # fill empty slots with the best unheld
            if len(book) >= n_per_side:
                break
            if c not in book and holdings.get(c) != direction:
                book.append(c)
        # improvement swaps: a non-held challenger displaces the weakest HELD member only if it
        # beats it by swap_margin (otherwise the near-tie held leg stays -> minimum rebalance)
        improved = True
        while improved:
            improved = False
            held_in_book = [s for s in book if holdings.get(s) == direction]
            if not held_in_book:
                break
            weakest = min(held_in_book, key=lambda s: side_val(s, direction))
            for c in cands:
                if c in book or holdings.get(c) == direction:
                    continue
                if side_val(c, direction) > side_val(weakest, direction) + swap_margin:
                    book.remove(weakest)
                    book.append(c)
                    improved = True
                    break
        return book

    long_book, short_book = build("long"), build("short")
    for s in set(long_book) & set(short_book):              # rare: resolve a both-sides name
        (short_book if side_val(s, "long") >= side_val(s, "short") else long_book).remove(s)

    keep_long = [s for s in long_book if holdings.get(s) == "long"]
    open_long = [s for s in long_book if holdings.get(s) != "long"]
    keep_short = [s for s in short_book if holdings.get(s) == "short"]
    open_short = [s for s in short_book if holdings.get(s) != "short"]
    booked = set(long_book) | set(short_book)
    for s in holdings:                                      # close any held leg not re-kept
        if s not in booked and s not in close:
            close.append(s)
    return {"keep_long": keep_long, "keep_short": keep_short,
            "open_long": open_long, "open_short": open_short, "close": close}
