"""Regime-routed strategy playbook (Pillar 2 — ADAPT: win in ALL market conditions).

Maps a symbol's regime quadrant (from its brief's `regime` field) to the IN-SEASON specialist desks
and strategies, so the CIO/Trader switch playbook WITH the tape instead of forcing one edge
everywhere. Pure/advisory: it shapes which DESK leads and which setups they hunt; the deterministic
gate still owns all risk/sizing.

DIRECTIONAL doctrine (TEMPEST-WEEKLY): this is an aggressive, NOT market-neutral desk — it runs a
one-sided book when a regime pays. All-weather means PROFIT IN ALL CONDITIONS by routing the right
desk to the right tape: Momentum leads trends, Carry + Scalper lead ranges, Scalper leads madness.
"""
from __future__ import annotations

# quadrant -> (in-season desks/strategies, one-line guidance)
_PLAYBOOK: dict[str, tuple[list[str], str]] = {
    "low_vol_trend": (
        ["momentum:trend-follow", "momentum:breakout", "squeeze-long/flush-short", "carry-overlay"],
        "Clean trend: MOMENTUM desk LEADS — ride with-trend pullback/breakout entries full size; "
        "carry as a steady overlay."),
    "high_vol_trend": (
        ["momentum:trend-follow", "breakout-trigger", "scalper-swings"],
        "Strong volatile trend: Momentum continuation WITH-regime; Scalper works the swings; gate "
        "only the counter-trend knife."),
    "low_vol_range": (
        ["carry-harvest", "mean-reversion:fade-edges", "scalper", "relative-value"],
        "Quiet range: CARRY + SCALPER LEAD — fade band edges (RR>=2) and harvest funding; Momentum "
        "stands down."),
    "high_vol_range": (
        ["scalper-small", "mean-reversion-small", "relative-value", "reduce-size"],
        "Choppy/madness: SCALPER only — smallest size, fastest exits; Momentum/Carry stand down."),
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
