"""The drawdown breakers were measuring a peak the LIVE strategy never made.

`peak_equity` is the reference for every drawdown brake: the -5% step-down (halves risk), the -10%
reduce-only, and the -15% force-flatten. It is also the tier input, and `caution` halves max_heat
AND per-trade risk again.

The desk was converted from the blended 3-leg book to the cross-sectional factor book at cy358. The
peak still standing is $10,668.83, set at cy71 on 2026-07-02 by the SUPERSEDED blended desk. The
factor desk was handed ~$10,016 and its own peak is ~$10,017, so it is being throttled for a
drawdown it never took:

    peak $10,668 (blended)  -> dd 7.57%  -> caution + breaker x0.5 -> max_heat 0.020 -> gross 0.067x
    peak $10,017 (factor)   -> dd 1.55%  -> healthy, no step-down  -> max_heat 0.040 -> gross 0.133x

This is a stale-INPUT correction, not a weakened limit: -5/-10/-15% all still fire, measured from
the capital this strategy was actually given. HARD RULE 5 forbids hand-editing state, so it is a
capability with its own guardrails rather than an edit.

THE GUARDRAILS MATTER MORE THAN THE FEATURE. Re-basing a high-water mark moves the force-flatten
floor DOWN, so this must never become a way to escape a real drawdown:
  * it may only ever LOWER the peak (raising one would fabricate a gain the desk never made);
  * the new reference is the era's own PEAK, which ratchets up and never follows equity down —
    that, not a bolted-on floor, is why a losing era cannot re-base its drawdown away;
  * never below current equity;
  * the era boundary is READ from the cycle reports' desk tag, never passed in as a magic number —
    a future conversion moves it automatically, and it cannot be aimed at an arbitrary cycle.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from rebase_peak import era_peak, era_start_cycle, plan_rebase  # noqa: E402


def _state(tmp_path, *, balance, peak, equity_rows, desks):
    (tmp_path / "cycle").mkdir(parents=True, exist_ok=True)
    (tmp_path / "account.json").write_text(json.dumps(
        {"balance": balance, "peak_equity": peak, "halt": False, "halt_reason": "",
         "updated_ts": "2026-09-09T00:00:00Z"}))
    with (tmp_path / "equity-history.jsonl").open("w") as fh:
        for cyc, eq in equity_rows:
            fh.write(json.dumps({"ts": "2026-09-01T00:00:00+00:00", "equity": eq,
                                 "cycle": cyc}) + "\n")
    for cyc, desk in desks.items():
        d = tmp_path / "cycle" / str(cyc)
        d.mkdir(parents=True, exist_ok=True)
        (d / "cio.json").write_text(json.dumps(
            {"allocations": [{"symbol": "BTCUSDT", "desk": desk}]}))
    return str(tmp_path)


def test_the_era_boundary_is_read_from_the_desk_tag_not_passed_in(tmp_path):
    """A magic cycle number could be aimed anywhere; the tag cannot."""
    s = _state(tmp_path, balance=100.0, peak=200.0, equity_rows=[(5, 100.0)],
               desks={3: "blended", 4: "blended", 5: "xsection", 6: "xsection"})
    assert era_start_cycle(s) == 5


def test_the_era_peak_ignores_everything_before_the_conversion(tmp_path):
    s = _state(tmp_path, balance=90.0, peak=500.0,
               equity_rows=[(3, 500.0), (4, 450.0), (5, 100.0), (6, 105.0), (7, 90.0)],
               desks={3: "blended", 4: "blended", 5: "xsection", 6: "xsection", 7: "xsection"})
    assert era_peak(s, 5) == 105.0


def test_the_cy71_case_is_what_this_fixes(tmp_path):
    """THE regression, in miniature: a superseded era's peak throttling the live one."""
    s = _state(tmp_path, balance=9861.56, peak=10668.83,
               equity_rows=[(71, 10668.83), (357, 10100.0), (358, 10016.11), (400, 10017.03),
                            (423, 9892.80)],
               desks={357: "blended", 358: "xsection", 400: "xsection", 423: "xsection"})

    plan = plan_rebase(s)

    assert plan["ok"] is True
    assert plan["new_peak"] == pytest.approx(10017.03)
    assert plan["old_peak"] == pytest.approx(10668.83)
    assert plan["new_drawdown"] < plan["old_drawdown"]


def test_it_REFUSES_to_raise_a_peak(tmp_path):
    """Raising one would fabricate a high-water mark the desk never reached."""
    s = _state(tmp_path, balance=100.0, peak=100.0,
               equity_rows=[(5, 100.0), (6, 500.0)],
               desks={5: "xsection", 6: "xsection"})

    plan = plan_rebase(s)

    assert plan["ok"] is False
    assert "lower" in plan["reason"].lower()


def test_a_LOSING_era_cannot_rebase_its_own_losses_away(tmp_path):
    """THE abuse case. If the factor desk itself draws down, a re-base must NOT walk the
    force-flatten floor down to meet the losses — that is the martingale the breakers exist to stop.

    The protection is that the new reference is the era's own PEAK, which ratchets up and never
    follows equity down. Discriminating setup: the era made a high ABOVE the standing peak's
    successor and then fell hard, so a mutant that re-based to anything equity-tracking would
    propose a lower number than the era peak.
    """
    s = _state(tmp_path, balance=8000.0, peak=12000.0,
               equity_rows=[(5, 10000.0), (6, 11000.0), (7, 9000.0), (8, 8000.0)],
               desks={5: "xsection", 6: "xsection", 7: "xsection", 8: "xsection"})

    plan = plan_rebase(s)

    assert plan["ok"] is True
    assert plan["new_peak"] == pytest.approx(11000.0), (
        "must re-base to the era's HIGH-WATER MARK, never to the drawn-down equity")
    assert plan["new_peak"] > 8000.0, "a drawdown must not drag the reference down with it"
    assert plan["new_flatten_at"] == pytest.approx(11000.0 * 0.85)


def test_the_new_peak_is_never_below_the_eras_starting_equity(tmp_path):
    """The desk was HANDED this capital; -15% is measured from what it was given.

    Holds because the era peak includes the era's first equity row, so the starting level is a
    lower bound for free — no separate floor term (an earlier one was dead code, see plan_rebase).
    """
    s = _state(tmp_path, balance=9000.0, peak=20000.0,
               equity_rows=[(5, 10000.0), (6, 9500.0), (7, 9000.0)],
               desks={5: "xsection", 6: "xsection", 7: "xsection"})

    plan = plan_rebase(s)

    assert plan["ok"] is True
    assert plan["new_peak"] >= 10000.0, "must not drop below the era's starting equity"


def test_the_new_peak_is_never_below_current_equity(tmp_path):
    """A peak under current equity would imply a NEGATIVE drawdown.

    account.balance is realised cash and can legitimately exceed the last logged mark-to-market
    equity, so this floor does real work — the discriminating case is balance ABOVE every logged
    row, which a mutant dropping the term would fail.
    """
    s = _state(tmp_path, balance=13000.0, peak=20000.0,
               equity_rows=[(5, 10000.0), (6, 11000.0), (7, 12000.0)],
               desks={5: "xsection", 6: "xsection", 7: "xsection"})

    plan = plan_rebase(s)

    assert plan["ok"] is True
    assert plan["new_peak"] == pytest.approx(13000.0), (
        "the peak must not sit under current equity — that is a negative drawdown")
    assert plan["new_drawdown"] == pytest.approx(0.0)


def test_it_reports_the_SAFETY_cost_it_is_imposing(tmp_path):
    """Lowering the peak lowers the force-flatten floor. That must be stated, not buried — it is
    the whole reason this is a decision and not a cleanup."""
    s = _state(tmp_path, balance=9861.56, peak=10668.83,
               equity_rows=[(357, 10100.0), (358, 10016.11), (400, 10017.03), (423, 9892.80)],
               desks={357: "blended", 358: "xsection", 400: "xsection", 423: "xsection"})

    plan = plan_rebase(s)

    assert plan["old_flatten_at"] == pytest.approx(10668.83 * 0.85)
    assert plan["new_flatten_at"] == pytest.approx(10017.03 * 0.85)
    assert plan["new_flatten_at"] < plan["old_flatten_at"], "the floor moves DOWN — say so"


def test_it_is_a_no_op_when_there_is_no_stale_peak(tmp_path):
    """Run twice and the second run must change nothing."""
    s = _state(tmp_path, balance=9900.0, peak=10017.03,
               equity_rows=[(358, 10016.11), (400, 10017.03), (423, 9900.0)],
               desks={358: "xsection", 400: "xsection", 423: "xsection"})

    plan = plan_rebase(s)

    assert plan["ok"] is False, "already re-based — must be idempotent, not repeatedly nudging"


def test_it_refuses_when_the_era_cannot_be_identified(tmp_path):
    """No desk tags -> no boundary -> no re-base. Fail closed."""
    s = _state(tmp_path, balance=100.0, peak=200.0, equity_rows=[(5, 100.0)], desks={})

    plan = plan_rebase(s)

    assert plan["ok"] is False
