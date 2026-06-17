"""Episode FINGERPRINT — the stable similarity key that lets the desk recall 'what happened the
last N times I took something like THIS'. It is the same (regime, desk, direction) identity the
Tier-2 cohort miner uses, rendered as one canonical string, so episodic recall (Tier-1, descriptive)
and lesson cohorts (Tier-2, statistically-gated) speak the same language and never drift apart.

Deliberately COARSE: regime x desk x direction. A finer key (per setup_type / crowding bucket) would
fragment the already-thin history into singletons that can teach nothing. Coarse-but-populated beats
precise-but-empty for a desk doing ~0.4 closes/cycle.
"""
from __future__ import annotations


def episode_fingerprint(regime, desk, direction) -> str:
    """Canonical (regime, desk, direction) key. None/'' collapse to 'any' so a legacy record with a
    missing desk still groups, rather than spawning a singleton no recall can use."""
    def _norm(v) -> str:
        s = str(v).strip().lower() if v not in (None, "") else "any"
        return s or "any"
    return f"{_norm(regime)}|{_norm(desk)}|{_norm(direction)}"


def fingerprint_of(rec: dict) -> str:
    """The fingerprint of a journal decision record (uses its OPEN-time desk/regime/direction)."""
    return episode_fingerprint(rec.get("regime"), rec.get("desk"), rec.get("direction"))


def describe_fingerprint(fp: str) -> str:
    """Human-readable rendering, e.g. 'SHORT / risk_off / momentum desk' for a prompt line."""
    regime, desk, direction = (fp.split("|") + ["any", "any", "any"])[:3]
    return f"{direction.upper()} / {regime} / {desk} desk"
