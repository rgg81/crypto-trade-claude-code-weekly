"""Regime-routed strategy playbook (Pillar 2 — ADAPT: win in ALL market conditions).

Maps a symbol's regime quadrant (from its brief's `regime` field) to the IN-SEASON specialist desks
and strategies, so the CIO/Trader switch playbook WITH the tape instead of forcing one edge
everywhere. Pure/advisory: it shapes which DESK leads and which setups they hunt; the deterministic
gate still owns all risk/sizing.

DOLLAR-NEUTRAL doctrine (TEMPEST-NEUTRAL): the book is ALWAYS balanced long/short (gross long$ ==
gross short$, ~1x). "All-weather" here means pairing the right EDGE on both sleeves: the dominant
neutral edge is cross-sectional MOMENTUM DISPERSION (long relative-strength / short relative-
weakness) with funding CARRY as a secondary tiebreaker — NEVER short a hot high-funding name just to
harvest carry (Phase-0 lesson: that sleeve net-loses). The desk routes which edge leads per regime
but NEVER runs one-sided; the deterministic gate owns all risk/sizing.
"""
from __future__ import annotations

# quadrant -> (in-season neutral edges/strategies, one-line guidance). ALL balanced long/short.
_PLAYBOOK: dict[str, tuple[list[str], str]] = {
    "low_vol_trend": (
        ["rv-momentum:long-strong/short-weak", "carry-tiebreaker", "relative-value"],
        "Clean trend: MOMENTUM DISPERSION leads — long the relative-strength names, short the "
        "relative-weakness names to equal $; carry only as a tiebreaker, never short a pumping "
        "high-funding name."),
    "high_vol_trend": (
        ["rv-momentum:long-strong/short-weak", "relative-value", "reduce-size"],
        "Strong volatile trend: momentum dispersion WITH tighter stops and smaller per-leg size; "
        "keep the book balanced — RV blow-outs lose BOTH sleeves."),
    "low_vol_range": (
        ["carry-neutral:long-neg/short-pos-funding", "mean-reversion:fade-edges", "relative-value"],
        "Quiet range: CARRY-NEUTRAL + RELATIVE-VALUE lead — pair neg-funding longs vs pos-funding "
        "shorts (collect both legs) and fade band-edge spreads to equal $."),
    "high_vol_range": (
        ["relative-value", "mean-reversion-small", "reduce-size"],
        "Choppy/madness: smallest balanced relative-value only — reduce gross, widen the no-trade "
        "band; do not chase dispersion into a whipsaw."),
    "transition": (
        ["confirmation-only", "reduce-size"],
        "Regime unclear: confirmation-gated entries only, reduced size; no directional knife."),
}

_DEFAULT: tuple[list[str], str] = (
    ["confirmation-only"], "Unknown quadrant: confirmation-gated entries only.")


def playbook_for(quadrant: str) -> dict:
    """In-season strategies + guidance for a quadrant. Unknown -> confirmation-only default."""
    strategies, guidance = _PLAYBOOK.get(quadrant, _DEFAULT)
    return {"quadrant": quadrant, "strategies": list(strategies), "guidance": guidance}


def is_range(quadrant: str) -> bool:
    """True for range quadrants where MEAN-REVERSION (not trend-follow) is the in-season edge."""
    return quadrant in ("low_vol_range", "high_vol_range")
