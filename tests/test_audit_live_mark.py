"""The anti-hallucination audit must measure an entry against the LIVE market, not a stale close.

`gate_execute_cli` built the audit's ground truth as `{"mark": brief["last_close"]}`: the close of
the last CLOSED 4h candle. The factor planner prices its entries from the live tape. So a name that
moved more than 5% inside the forming candle was dropped as a "fabricated" entry, though its entry
sat within ~0.5% of the brief's own live `mark_price`. Six factor-era rebalances lost a leg this way
(cy462 AKE, cy474 AR, cy510 ONE, cy540 ZRO, cy558 AT, cy585 MAGIC), each one leaving the book one
leg short for a day. Five of the six were momentum longs, the names the factor exists to buy.

The tolerance stays 5%. Only the reference changes, to the live mark, with `last_close` as the
fallback. A fabricated price is still caught.
"""
from __future__ import annotations

import json
import os

import pytest

from futures_fund.proposal_audit import audit_batch, ground_truth_from_briefs

# (cycle, symbol, direction, entry, brief last_close, brief mark_price) - live, from state/cycle/*
LIVE_DROPS = [
    (462, "AKEUSDT", "long", 0.020651, 0.019506, 0.020563),
    (474, "ARUSDT", "long", 3.694, 4.046, 3.72213121),
    (510, "ONEUSDT", "long", 0.0023744, 0.0021876, 0.0023267),
    (540, "ZROUSDT", "long", 1.7748, 1.6645, 1.77854943),
    (558, "ATUSDT", "short", 0.1321, 0.1252, 0.1312),
    (585, "MAGICUSDT", "long", 0.12532, 0.11877, 0.12463131),
]


def _brief(sym, last_close, mark_price=None, atr=0.01):
    b = {"exchange_id": sym, "last_close": last_close, "atr": atr}
    if mark_price is not None:
        b["mark_price"] = mark_price
    return b


def test_ground_truth_uses_the_live_mark():
    gt = ground_truth_from_briefs([_brief("MAGICUSDT", 0.11877, 0.12463131, atr=0.0145)])
    assert gt == {"MAGICUSDT": {"mark": 0.12463131, "atr": 0.0145}}


@pytest.mark.parametrize("bad", [None, 0, -1.0, float("nan"), "n/a"])
def test_a_missing_or_bad_mark_falls_back_to_last_close(bad):
    b = _brief("XUSDT", 2.0)
    b["mark_price"] = bad
    assert ground_truth_from_briefs([b])["XUSDT"]["mark"] == 2.0


def test_briefs_without_an_exchange_id_are_skipped():
    assert ground_truth_from_briefs([{"last_close": 1.0, "mark_price": 1.0}, {}]) == {}
    assert ground_truth_from_briefs(None) == {}


@pytest.mark.parametrize("cycle,sym,direction,entry,last_close,mark", LIVE_DROPS)
def test_the_six_live_drops_now_pass(cycle, sym, direction, entry, last_close, mark):
    gt = ground_truth_from_briefs([_brief(sym, last_close, mark)])
    kept, dropped = audit_batch([{"symbol": sym, "direction": direction, "entry": entry}], gt,
                                is_trigger=False)
    assert [k["symbol"] for k in kept] == [sym], dropped


@pytest.mark.parametrize("cycle,sym,direction,entry,last_close,mark", LIVE_DROPS)
def test_a_fabricated_entry_is_still_dropped(cycle, sym, direction, entry, last_close, mark):
    """The audit's job is unchanged: an entry 10% off the LIVE mark is still a fantasy fill."""
    gt = ground_truth_from_briefs([_brief(sym, last_close, mark)])
    for fake in (mark * 1.10, mark * 0.90):
        kept, dropped = audit_batch([{"symbol": sym, "direction": direction, "entry": fake}], gt,
                                    is_trigger=False)
        assert not kept and dropped, (sym, fake)


def test_an_entry_matching_only_the_stale_close_is_now_dropped():
    """The old reference let a stale-close entry through even when the market had moved away; the
    live mark catches it. This tightens the check where it was loose."""
    gt = ground_truth_from_briefs([_brief("ARUSDT", 4.046, 3.72213121)])
    kept, dropped = audit_batch([{"symbol": "ARUSDT", "direction": "long", "entry": 4.046}], gt,
                                is_trigger=False)
    assert not kept and dropped


@pytest.mark.parametrize("cycle,sym,direction,entry,last_close,mark", LIVE_DROPS)
def test_the_recorded_briefs_match_this_table(cycle, sym, direction, entry, last_close, mark):
    """Pin the table to the real artifacts while they exist, so it cannot drift from evidence."""
    p = f"state/cycle/{cycle}/context.json"
    if not os.path.exists(p):
        pytest.skip("live cycle artifacts not present")
    b = next(b for b in json.load(open(p))["briefs"] if b.get("exchange_id") == sym)
    assert b["last_close"] == pytest.approx(last_close)
    assert b["mark_price"] == pytest.approx(mark)


def test_the_gate_cli_builds_its_ground_truth_with_the_helper():
    """Source guard (the CLI main is not unit-runnable): use the helper, never last_close."""
    import ast
    src = open("scripts/gate_execute_cli.py").read()
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "id", getattr(n.func, "attr", None)) == "ground_truth_from_briefs"]
    assert calls, "gate_execute_cli must build ground truth via ground_truth_from_briefs"
    assert '"mark": b.get("last_close")' not in src
