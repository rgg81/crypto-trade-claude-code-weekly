"""Deterministic, two-sided candidate-lesson MINER (no LLM).

Groups CLOSED trades into (regime x desk x direction) cohorts and emits DATA-SUMMARY candidate
lessons — NOT causal claims. The agents read these as priors (e.g. "your risk_off shorts have
net-lost over n=4") and do the causal interpretation themselves; only DSR-validated cohorts ever
become standing rules. Two-sidedness with an anti-always-press asymmetry (the desk's failure mode
is OVER-deployment): a restrictive (brake) candidate surfaces from a small losing cohort, but an
enabling ('DO press') candidate needs a LARGER sample AND a positive MEDIAN R (so a single
fat-tail winner can't mint a press rule — adversarial must-fix #2).
"""
from __future__ import annotations

from collections import defaultdict
from statistics import median

from futures_fund.fingerprint import episode_fingerprint

MIN_N_RESTRICTIVE = 2   # a losing pattern surfaces fast — the brake is the cheap side
MIN_N_ENABLING = 3      # a 'DO press' rule needs more support (anti-always-press ratchet)
LOSS_R = -0.10          # cohort MEAN R <= this -> restrictive candidate
WIN_R = 0.10            # cohort MEDIAN R >= this -> enabling candidate (median, never mean)
RECENCY_WINDOW = 10     # a cohort is judged on its LAST N closes, not all history. This is what
#                         makes the desk anti-ossifying: a standing rule built on an old losing
#                         streak is demoted once the cohort's RECENT window robustly flips sign (a
#                         real regime change) — while a few-trade wobble can't. The DSR>=0.95 +
#                         >=5-distinct-cycle promotion gate remains the sole anti-overfit authority;
#                         the window only decides WHICH evidence is current, never what validates.
RECENCY_CYCLES = 60     # AND a cohort is only 'current' if it has traded within this many cycles
#                         (~10 days at 4h). A cohort gone silent past this stops minting its lesson,
#                         so the reflect-runner's TTL sweep can age the lesson out (regime-recency
#                         expiry, must-fix #3). Append-only journal + count-only window would
#                         otherwise re-mint a frozen cohort's lesson forever, and nothing expires.


def _cohort_key(rec: dict) -> tuple:
    # Normalise EXACTLY like fingerprint.episode_fingerprint (strip/lower/"any") so the miner's
    # cohort bucketing matches the episodic + per-cell DSR cells — a 'Risk_Off' / ' risk_off '
    # variant can't split one cohort into two thin ones that miss the mint thresholds.
    return tuple(episode_fingerprint(rec.get("regime"), rec.get("desk"),
                                     rec.get("direction")).split("|"))


def _recency_key(rec: dict):
    # newest-first: highest cycle (then ts) is most recent. Missing keys sort oldest (stable).
    return (rec.get("cycle") or 0, str(rec.get("ts") or ""))


def _rs(recs: list[dict]) -> list[float]:
    return [r["r_multiple"] for r in recs if r.get("r_multiple") is not None]


def _mean_excluding_best(rs: list[float]) -> float:
    """Mean with the single LARGEST R removed — the anti-fat-tail-winner test (must-fix #2). 'Even
    WITHOUT your luckiest trade, is this still a winning setup?' One outsized winner can drag a mean
    (and even a marginal median) positive; dropping it before the enabling floor stops one jackpot
    from minting a standing 'DO press' rule. With <=1 sample there is nothing to exclude."""
    if not rs:
        return 0.0
    if len(rs) == 1:
        return rs[0]
    xs = sorted(rs)
    return sum(xs[:-1]) / (len(xs) - 1)


def mine_candidates(payload: dict, now_cycle: int | None = None) -> list[dict]:
    """Return a list of candidate-lesson dicts {text, regime, tags, importance, polarity,
    provenance, n_support}. `payload` is `reflect.reflection_payload(...)`. Each cohort is judged
    on its most-recent RECENCY_WINDOW closes so the corpus tracks the CURRENT regime. When
    `now_cycle` is given, trades older than RECENCY_CYCLES are dropped FIRST, so a cohort that has
    gone silent produces no candidate (letting its lesson age out via the runner's TTL sweep)."""
    closed = list(payload.get("winners") or []) + list(payload.get("losers") or [])
    by_cohort: dict[tuple, list[dict]] = defaultdict(list)
    for r in closed:
        if r.get("r_multiple") is None:
            continue
        if now_cycle is not None and r.get("cycle") is not None \
                and int(r["cycle"]) < now_cycle - RECENCY_CYCLES:
            continue  # stale: outside the cohort-recency horizon
        by_cohort[_cohort_key(r)].append(r)
    # keep only the most-recent window per cohort (anti-ossification — recent evidence wins)
    for k in by_cohort:
        by_cohort[k] = sorted(by_cohort[k], key=_recency_key, reverse=True)[:RECENCY_WINDOW]

    out: list[dict] = []
    for (regime, desk, direction), recs in sorted(by_cohort.items()):
        rs = _rs(recs)
        if not rs:
            continue
        n, mean_r, med_r = len(rs), sum(rs) / len(rs), median(rs)
        wins = sum(1 for r in rs if r > 0)
        prov = [r["id"] for r in recs if r.get("id")][:10]
        rtag = None if regime == "any" else regime
        tags = [f"regime:{regime}", f"desk:{desk}", f"dir:{direction}"]
        if mean_r <= LOSS_R and n >= MIN_N_RESTRICTIVE:
            out.append({
                "text": (f"{direction.upper()} trades in {regime} (desk: {desk}) have NET-LOST: "
                         f"{wins}/{n} winners, mean {mean_r:+.2f}R. Demand stronger confirmation, "
                         f"size smaller, or cut faster on this setup."),
                "regime": rtag, "tags": [*tags, "cohort:net-loss"], "polarity": "restrictive",
                "importance": min(9, 4 + n), "provenance": prov, "n_support": n})
        elif med_r >= WIN_R and _mean_excluding_best(rs) >= WIN_R and n >= MIN_N_ENABLING:
            out.append({
                "text": (f"{direction.upper()} trades in {regime} (desk: {desk}) have NET-WON: "
                         f"{wins}/{n} winners, median {med_r:+.2f}R. A proven edge — press it when "
                         f"it sets up (still inside the gate's risk caps)."),
                "regime": rtag, "tags": [*tags, "cohort:net-win"], "polarity": "enabling",
                "importance": min(8, 3 + n), "provenance": prov, "n_support": n})

    # ENABLING from edge-aligned FLATs that cost us (the 'standing aside hurt' DO-rule source).
    # Same anti-press floor: needs >= MIN_N_ENABLING occasions before it can mint a 'DO take it'.
    by_regime: dict[str, list[dict]] = defaultdict(list)
    for f in payload.get("missed_opportunities") or []:
        by_regime[f.get("regime") or "any"].append(f)
    for regime, flats in sorted(by_regime.items()):
        if len(flats) >= MIN_N_ENABLING:
            prov = [f["id"] for f in flats if f.get("id")][:10]
            out.append({
                "text": (f"Standing aside on edge-aligned setups in {regime} cost the desk on "
                         f"{len(flats)} occasions (they moved our way). DO take them when the edge "
                         f"is present, not stand flat."),
                "regime": None if regime == "any" else regime,
                "tags": [f"regime:{regime}", "flat:cost-us"], "polarity": "enabling",
                "importance": min(8, 3 + len(flats)), "provenance": prov, "n_support": len(flats)})
    return out
