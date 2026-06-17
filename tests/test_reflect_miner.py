"""Phase 1 — the two-sided candidate miner. Restrictive lessons mint from small losing cohorts;
enabling ('press') lessons need a larger sample AND a positive MEDIAN (one fat-tail winner must
NOT mint a press rule — the anti-always-press ratchet, adversarial must-fix #2)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from futures_fund.reflect_miner import mine_candidates  # noqa: E402


def _c(sym, regime, desk, direction, r):
    return {"id": f"{sym}-{r}", "symbol": sym, "regime": regime, "desk": desk,
            "direction": direction, "r_multiple": r}


def test_losing_cohort_mints_a_restrictive_candidate():
    payload = {"losers": [_c("SOL", "risk_off", "momentum", "short", -0.4),
                          _c("BTC", "risk_off", "momentum", "short", -0.5)],
               "winners": [], "missed_opportunities": []}
    cands = mine_candidates(payload)
    r = [c for c in cands if c["polarity"] == "restrictive"]
    assert len(r) == 1
    assert r[0]["regime"] == "risk_off" and r[0]["n_support"] == 2
    assert "NET-LOST" in r[0]["text"]


def test_one_fat_tail_winner_does_NOT_mint_an_enabling_press_rule():
    # 1 huge winner + 2 small losers: mean is positive but MEDIAN is negative AND n<MIN_ENABLING
    # for the win path; the miner must NOT emit an enabling 'press' candidate (anti-always-press).
    payload = {"winners": [_c("WLD", "risk_on", "carry", "long", +6.0)],
               "losers": [_c("WLD", "risk_on", "carry", "long", -0.5),
                          _c("WLD", "risk_on", "carry", "long", -0.6)],
               "missed_opportunities": []}
    cands = mine_candidates(payload)
    assert not [c for c in cands if c["polarity"] == "enabling"]


def test_marginal_median_propped_by_one_jackpot_does_NOT_mint_enabling():
    # 5 trades whose MEDIAN clears WIN_R but only because one +9R jackpot lifts the cohort: the rest
    # are ~breakeven/negative. Drop-the-best mean is negative -> the trimmed-mean floor BLOCKS the
    # press rule (adversarial must-fix #2: one fat-tail winner can't mint a 'DO press' rule).
    rs = [-1.0, 0.15, 0.2, 0.25, 9.0]   # median 0.2 >= WIN_R; mean-excl-best (-1+.15+.2+.25)/4 < 0
    payload = {"winners": [_c("X", "risk_on", "carry", "long", r) for r in rs if r > 0],
               "losers": [_c("X", "risk_on", "carry", "long", r) for r in rs if r <= 0],
               "missed_opportunities": []}
    assert not [c for c in mine_candidates(payload) if c["polarity"] == "enabling"]


def test_robust_winning_cohort_mints_an_enabling_candidate():
    # >= MIN_N_ENABLING (3) winners with a clearly positive median -> enabling is allowed.
    payload = {"winners": [_c("WLD", "risk_on", "carry", "long", +1.2),
                           _c("WLD", "risk_on", "carry", "long", +0.8),
                           _c("WLD", "risk_on", "carry", "long", +1.5)],
               "losers": [], "missed_opportunities": []}
    cands = mine_candidates(payload)
    e = [c for c in cands if c["polarity"] == "enabling"]
    assert len(e) == 1 and e[0]["n_support"] == 3 and "NET-WON" in e[0]["text"]


def test_missed_flats_mint_enabling_only_above_the_floor():
    flats = [{"id": f"f{i}", "regime": "risk_on", "edge_aligned": True} for i in range(3)]
    cands = mine_candidates({"winners": [], "losers": [], "missed_opportunities": flats})
    e = [c for c in cands if "flat:cost-us" in c["tags"]]
    assert len(e) == 1 and e[0]["polarity"] == "enabling"
    # only 2 missed flats -> below the floor -> no enabling lesson
    cands2 = mine_candidates({"winners": [], "losers": [], "missed_opportunities": flats[:2]})
    assert not [c for c in cands2 if "flat:cost-us" in c["tags"]]


def test_empty_payload_is_safe():
    assert mine_candidates({}) == []
    assert mine_candidates({"winners": [], "losers": [], "missed_opportunities": []}) == []
