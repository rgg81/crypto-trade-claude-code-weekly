"""A submitted proposal that never opens must be REPORTED, not vanish.

cy290-292: the blended plan submitted 6 legs, the pre-sizer kept all 6 (`n_kept: 6, n_dropped: 0`,
`heat_dropped: []`), and the gate reported `dropped: 0`, `drop_reasons: []`, `vetoed: 0` — yet only
4 opened. BNBUSDT and XRPUSDT appeared nowhere at all. The loss happens inside PROTECTED code:

    # consolidation.consolidate(), after batch-scaling to the gross-heat cap
    return [t for t in trades if risk(t) >= min_risk_frac]

A leg scaled below the dust risk floor is simply absent from the returned list — the batch gets
shorter and nothing records why. That silent hole cost three cycles of investigation, left the book
stuck at L3/S1, and was only findable once the pre-guard snapshot preserved the evidence.

consolidate is protected and its dust drop is a real safety behaviour, so this does not change it —
it reconciles submitted-vs-opened afterwards so the disappearance is visible.
"""
from scripts.reconcile import unexplained_opens


def _rep(actions, dropped=(), vetoed=()):
    return {"actions": list(actions), "drop_reasons": list(dropped), "vetoed": list(vetoed)}


def test_the_cy292_silent_loss_is_surfaced():
    submitted = ["BTWUSDT", "SOLUSDT", "LINKUSDT", "BNBUSDT", "XRPUSDT", "HYPEUSDT"]
    rep = _rep([{"open": "BTWUSDT", "direction": "long"},
                {"open": "SOLUSDT", "direction": "long"},
                {"open": "LINKUSDT", "direction": "long"},
                {"open": "HYPEUSDT", "direction": "short"}])
    assert unexplained_opens(submitted, rep) == ["BNBUSDT", "XRPUSDT"]


def test_nothing_reported_when_every_leg_opens():
    submitted = ["AAAUSDT", "BBBUSDT"]
    rep = _rep([{"open": "AAAUSDT", "direction": "long"},
                {"open": "BBBUSDT", "direction": "short"}])
    assert unexplained_opens(submitted, rep) == []


def test_an_explained_drop_is_not_double_counted():
    """A leg the gate already accounted for is explained — only the SILENT ones matter."""
    submitted = ["AAAUSDT", "BBBUSDT"]
    rep = _rep([{"open": "AAAUSDT", "direction": "long"}],
               dropped=[{"symbol": "BBBUSDT", "reason": "RR 1.20 < min 2"}])
    assert unexplained_opens(submitted, rep) == []


def test_a_vetoed_leg_is_not_reported_as_silent():
    submitted = ["AAAUSDT", "BBBUSDT"]
    rep = _rep([{"open": "AAAUSDT", "direction": "long"}],
               vetoed=[{"symbol": "BBBUSDT", "reason": "liq distance"}])
    assert unexplained_opens(submitted, rep) == []


def test_plain_string_drop_entries_are_understood():
    submitted = ["AAAUSDT", "BBBUSDT"]
    rep = _rep([{"open": "AAAUSDT", "direction": "long"}], dropped=["BBBUSDT: heat cap"])
    assert unexplained_opens(submitted, rep) == []


def test_result_is_sorted_and_deduped():
    submitted = ["BBBUSDT", "AAAUSDT", "AAAUSDT"]
    assert unexplained_opens(submitted, _rep([])) == ["AAAUSDT", "BBBUSDT"]


def test_empty_and_malformed_inputs_are_safe():
    assert unexplained_opens([], _rep([])) == []
    assert unexplained_opens(["AAAUSDT"], {}) == ["AAAUSDT"]
    assert unexplained_opens(["AAAUSDT"], {"actions": [None, "junk", {}]}) == ["AAAUSDT"]
