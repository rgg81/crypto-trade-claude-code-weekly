"""Phase 1 — the reflect runner: corpus grows, recurrence is gated, NOTHING validates on a thin
record (DSR<0.95 below 10 trades), and a contradicted validated lesson is demoted."""
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from futures_fund.journal import append_decision, patch_outcome  # noqa: E402
from futures_fund.lessons import read_lessons  # noqa: E402
from futures_fund.reflect_runner import reflect_and_record  # noqa: E402

_T0 = datetime(2026, 6, 1, tzinfo=UTC)


def _closed(mem, *, cycle, sym, direction, regime, desk, r, entry=100.0, stop=110.0, size=1.0):
    did = append_decision(mem, {"cycle": cycle, "symbol": sym, "direction": direction,
                                "entry": entry, "stop": stop, "size": size, "ts": _T0})
    # r_multiple = realized/risk; pick realized so r_multiple == r (risk = |entry-stop|*size)
    risk = abs(entry - stop) * size
    patch_outcome(mem, did, {"exit_ts": _T0, "realized_pnl": r * risk, "r_multiple": r,
                             "regime": regime, "desk": desk, "prediction_correct": r > 0})
    return did


def _seed_losing_cohort(mem, n):
    for i in range(n):
        _closed(mem, cycle=10 + i, sym="SOLUSDT", direction="short", regime="risk_off",
                desk="momentum", r=-0.4)


def test_corpus_grows_and_recurrence_confirms_but_does_not_validate_thin(tmp_path):
    mem = tmp_path / "memory"
    _seed_losing_cohort(mem, 2)            # only 2 closed -> DSR returns 0.0
    s1 = reflect_and_record(mem, _T0, 10, dsr_pvalue=0.0)
    assert s1["new"] == 1 and s1["confirmed"] == 0           # brand-new candidate
    s2 = reflect_and_record(mem, _T0 + timedelta(hours=4), 11, dsr_pvalue=0.0)
    assert s2["confirmed"] == 1 and s2["validated"] == 0     # recurs -> confirmed, NOT validated
    lz = read_lessons(mem)[0]
    assert lz.state == "candidate" and lz.confirmations == 1 and lz.polarity == "restrictive"


def test_rerun_of_same_cycle_does_not_double_confirm(tmp_path):
    # a gate RETRY re-runs reflect for the SAME cycle. The candidate must NOT be confirmed twice
    # (confirmations count DISTINCT cycles). Minting stays idempotent; the count does not move.
    mem = tmp_path / "memory"
    _seed_losing_cohort(mem, 3)
    a = reflect_and_record(mem, _T0, 10, dsr_pvalue=0.99)
    assert a["new"] == 1
    b = reflect_and_record(mem, _T0, 10, dsr_pvalue=0.99)        # SAME cycle -> no-op confirm
    assert b["new"] == 0 and b["confirmed"] == 0
    lz = read_lessons(mem)[0]
    assert lz.confirmations == 0 and lz.state == "candidate"     # not advanced past mint
    # the NEXT distinct cycle DOES confirm
    c = reflect_and_record(mem, _T0 + timedelta(hours=4), 11, dsr_pvalue=0.99)
    assert c["confirmed"] == 1 and read_lessons(mem)[0].confirmations == 1


def test_never_validates_below_dsr_even_after_many_confirmations(tmp_path):
    mem = tmp_path / "memory"
    _seed_losing_cohort(mem, 3)
    for c in range(10):                                       # many cycles, but DSR stays < 0.95
        reflect_and_record(mem, _T0 + timedelta(hours=4 * c), 10 + c, dsr_pvalue=0.5)
    assert all(lz.state == "candidate" for lz in read_lessons(mem))


def test_validates_only_with_dsr_and_recurrence(tmp_path):
    mem = tmp_path / "memory"
    _seed_losing_cohort(mem, 12)                             # >=10 closed so DSR can clear
    states = []
    for c in range(7):                                       # 1 new + ~5 confirms -> validated
        reflect_and_record(mem, _T0 + timedelta(hours=4 * c), 30 + c, dsr_pvalue=0.99)
        states.append(read_lessons(mem)[0].state)
    assert states[-1] == "validated"
    assert "candidate" in states and states.index("validated") >= 5  # took >=5 distinct cycles


def test_contradiction_demotes_a_validated_lesson(tmp_path):
    mem = tmp_path / "memory"
    # validate a restrictive (risk_off, momentum, short) net-loss lesson
    _seed_losing_cohort(mem, 12)
    for c in range(7):
        reflect_and_record(mem, _T0 + timedelta(hours=4 * c), 30 + c, dsr_pvalue=0.99)
    assert read_lessons(mem)[0].state == "validated"
    # now the SAME cohort net-WINS for a SUSTAINED recent run (a real regime change, not a 3-trade
    # wobble): once the recency window fills with wins the cohort mints the opposite (enabling)
    # polarity -> the stale restrictive standing rule is demoted. (A robust learner must NOT flip a
    # 12-loss rule on a few wins; it MUST yield to a sustained reversal.)
    for i in range(10):
        _closed(mem, cycle=60 + i, sym="SOLUSDT", direction="short", regime="risk_off",
                desk="momentum", r=+1.0)
    out = reflect_and_record(mem, _T0 + timedelta(hours=400), 60, dsr_pvalue=0.99)
    assert out["demoted"] >= 1
    restr = next(lz for lz in read_lessons(mem) if lz.polarity == "restrictive")
    assert restr.state == "candidate" and restr.confirmations == 0   # demoted + reset


# ---- Phase 3: per-cell DSR gating + asymmetric TTL expiry ----

def test_percell_dsr_blocks_validation_until_the_cell_is_proven(tmp_path):
    # default dsr_pvalue=None -> gate on the CELL's own DSR. A 4-trade cell is below the 10-obs
    # floor (cell DSR 0.0), so the lesson confirms every cycle but NEVER validates.
    mem = tmp_path / "memory"
    _seed_losing_cohort(mem, 4)
    for c in range(7):
        reflect_and_record(mem, _T0 + timedelta(hours=4 * c), 30 + c)   # None -> per-cell
    lz = read_lessons(mem)[0]
    assert lz.state == "candidate" and lz.confirmations >= 5             # confirmed, cell unproven


def test_explicit_dsr_override_validates_the_same_thin_cell(tmp_path):
    # a caller passing an explicit DSR (a desk-wide gate / a test) bypasses the per-cell computation
    mem = tmp_path / "memory"
    _seed_losing_cohort(mem, 4)
    for c in range(7):
        reflect_and_record(mem, _T0 + timedelta(hours=4 * c), 30 + c, dsr_pvalue=0.99)
    assert any(lz.state == "validated" for lz in read_lessons(mem))


def test_ttl_retires_a_candidate_whose_cohort_went_silent(tmp_path):
    mem = tmp_path / "memory"
    for i in range(3):
        _closed(mem, cycle=10 + i, sym="SOLUSDT", direction="short", regime="risk_off",
                desk="momentum", r=-0.4)
    reflect_and_record(mem, _T0, 12, dsr_pvalue=0.0)                     # mint, last_seen=12
    assert read_lessons(mem)[0].state == "candidate"
    # advance FAR past the cohort-recency horizon: no re-mint -> TTL retires the stale candidate
    out = reflect_and_record(mem, _T0 + timedelta(hours=4 * 300), 300, dsr_pvalue=0.0)
    assert out["expired"] >= 1
    assert all(lz.state == "retired" for lz in read_lessons(mem))


def test_ttl_expiry_is_asymmetric(tmp_path):
    # directly exercise the staleness policy: stale CANDIDATE -> retired; stale VALIDATED ENABLING
    # 'press' rule -> demoted (re-prove a possibly-decayed edge); stale VALIDATED RESTRICTIVE brake
    # -> KEPT (its silence is the rule succeeding, not decaying); undated lesson -> left alone.
    from futures_fund.lessons import append_lesson
    from futures_fund.reflect_runner import TTL_CYCLES, _expire_stale
    mem = tmp_path / "memory"
    append_lesson(mem, {"text": "stale cand", "state": "candidate", "polarity": "restrictive",
                        "last_seen_cycle": 0}, ts=_T0)
    append_lesson(mem, {"text": "stale brake", "state": "validated", "polarity": "restrictive",
                        "last_seen_cycle": 0}, ts=_T0)
    append_lesson(mem, {"text": "stale press", "state": "validated", "polarity": "enabling",
                        "last_seen_cycle": 0}, ts=_T0)
    append_lesson(mem, {"text": "undated", "state": "candidate", "polarity": "restrictive",
                        "last_seen_cycle": -1}, ts=_T0)
    n = _expire_stale(mem, TTL_CYCLES + 100)
    by_text = {lz.text: lz for lz in read_lessons(mem)}
    assert by_text["stale cand"].state == "retired"
    assert by_text["stale press"].state == "candidate"         # demoted, re-prove
    assert by_text["stale brake"].state == "validated"         # working brake kept
    assert by_text["undated"].state == "candidate"             # no stamp -> untouched
    assert n == 2
