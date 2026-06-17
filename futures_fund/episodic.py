"""TIER-1 EPISODIC RECALL — the anti-press tail-risk brake.

Distinct from the Tier-2 lesson corpus (statistically-gated cohort RULES), episodic memory is
DESCRIPTIVE: it recalls the specific WORST outcomes the desk has actually suffered on a fingerprint
('SHORT / risk_off / momentum desk') and lays them in front of the agent BEFORE it presses. The
desk's structural failure mode is OVER-deployment ('always press'); making the realized tail vivid
('the last time you pressed this, you lost -1.0R') is the cheapest brake there is. It is NOT a rule,
carries NO promotion/DSR gate, and the deterministic gate NEVER reads it — it only shapes the
agent's judgement about how hard to lean in.
"""
from __future__ import annotations

from statistics import mean

from futures_fund.fingerprint import describe_fingerprint, fingerprint_of
from futures_fund.journal import read_all_decisions

MIN_EPISODES = 2     # need >=2 realised outcomes on a fingerprint before recall says anything


def closed_episodes(memory_dir) -> list[dict]:
    """All CLOSED, attributed journal records (realised_pnl AND r_multiple present)."""
    return [d for d in read_all_decisions(memory_dir)
            if d.get("realized_pnl") is not None and d.get("r_multiple") is not None]


def episodes_by_fingerprint(memory_dir) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for d in closed_episodes(memory_dir):
        out.setdefault(fingerprint_of(d), []).append(d)
    return out


def trimmed_mean(rs: list[float], trim: float = 0.2) -> float:
    """Symmetric trimmed mean — drop the top & bottom `trim` fraction (>=1 each end once big enough)
    so neither a fat-tail winner NOR a fat-tail loser dominates the central read."""
    xs = sorted(r for r in rs if r is not None)
    if not xs:
        return 0.0
    k = int(len(xs) * trim)
    core = xs[k:len(xs) - k] if 2 * k < len(xs) else xs
    return mean(core)


def worst_episodes(eps: list[dict], k: int = 3) -> list[dict]:
    """The k worst (most negative r_multiple) episodes, worst first."""
    return sorted(eps, key=lambda d: d.get("r_multiple", 0.0))[:k]


def episodic_summary(eps: list[dict], k_worst: int = 3) -> dict:
    rs = [d["r_multiple"] for d in eps if d.get("r_multiple") is not None]
    n = len(rs)
    wins = sum(1 for r in rs if r > 0)
    worst = worst_episodes(eps, k_worst)
    return {
        "n": n,
        "wins": wins,
        "win_rate": (wins / n) if n else 0.0,
        "worst_r": min(rs) if rs else 0.0,
        "best_r": max(rs) if rs else 0.0,
        "mean_r": (sum(rs) / n) if n else 0.0,
        "trimmed_mean_r": trimmed_mean(rs),
        "worst_examples": [
            {"symbol": d.get("symbol"), "cycle": d.get("cycle"),
             "r_multiple": round(float(d.get("r_multiple", 0.0)), 2),
             "close_reason": d.get("close_reason")}
            for d in worst],
    }


def recall_for_context(memory_dir, *, min_n: int = MIN_EPISODES, k_worst: int = 3) -> list[dict]:
    """The anti-press episodic block for context.json. One descriptive entry per fingerprint with
    >= min_n realised outcomes, MOST-DANGEROUS first (worst realised R). Fail-safe: returns [] if
    the journal is unreadable (the caller also wraps this)."""
    try:
        groups = episodes_by_fingerprint(memory_dir)
    except Exception:  # noqa: BLE001 — descriptive recall must never break the cycle
        return []
    blocks: list[dict] = []
    for fp, eps in groups.items():
        if len(eps) < min_n:
            continue
        s = episodic_summary(eps, k_worst)
        ex = ", ".join(f"{w['symbol']} {w['r_multiple']:+.2f}R" for w in s["worst_examples"])
        text = (f"TAIL-RISK [{describe_fingerprint(fp)}]: {s['n']} closed, "
                f"{s['wins']}/{s['n']} won, worst {s['worst_r']:+.2f}R, "
                f"trimmed-mean {s['trimmed_mean_r']:+.2f}R. "
                f"Before PRESSING this setup, weight the downside — worst were: {ex}.")
        blocks.append({"fingerprint": fp, "label": describe_fingerprint(fp), "text": text, **s})
    blocks.sort(key=lambda b: b["worst_r"])   # most dangerous (deepest realised loss) first
    return blocks
