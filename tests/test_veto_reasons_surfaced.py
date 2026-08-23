"""When the gate vetoes a leg, print WHY on the tick that did it.

`cycle.py` records every veto to the shadow ledger with its reason, but the cycle report keeps only
an integer count (`report["vetoed"] = len(vetoed)`) and `drop_reasons` stays empty. So the tick
prints `opened 0 closed 2` and the operator has no idea why two legs vanished.

cy325 made that expensive. Both planned longs were vetoed, the long sleeve collapsed to one leg,
and the book ended at tilt 0.4230 — ~$1,023 net short, 10% of equity. Diagnosing it took reproducing
the gate offline, checking the breaker, the period returns and the heat caps, before finding the
answer already written in state/shadow-ledger.jsonl:

    "no heat headroom (used 0.043 >= cap 0.040)"

(The book had been sized at the healthy-tier cap of 0.08; drawdown crossed 5%, the tier flipped to
caution, and the 0.04 cap put the EXISTING book over budget.)

`cycle.py` and `risk_gate.py` are PROTECTED, so this reads what they already write rather than
changing what they record.
"""
import json

from scripts.auto_cycle import _veto_reasons


def _write(p, rows):
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_reads_the_live_cy325_vetoes(tmp_path):
    _write(tmp_path / "shadow-ledger.jsonl", [
        {"symbol": "BTWUSDT", "direction": "long", "reason": "cannot satisfy liq-distance rule "
         "within leverage cap", "cycle": 305},
        {"symbol": "PUMPUSDT", "direction": "long",
         "reason": "no heat headroom (used 0.043 >= cap 0.040)", "cycle": 325},
        {"symbol": "ZECUSDT", "direction": "long",
         "reason": "no heat headroom (used 0.043 >= cap 0.040)", "cycle": 325},
    ])
    out = _veto_reasons(tmp_path, 325)
    assert "PUMPUSDT" in out and "ZECUSDT" in out
    assert "no heat headroom (used 0.043 >= cap 0.040)" in out
    assert "BTWUSDT" not in out, "must not leak vetoes from other cycles"


def test_identical_reasons_are_grouped_not_repeated(tmp_path):
    _write(tmp_path / "shadow-ledger.jsonl", [
        {"symbol": "A", "reason": "same", "cycle": 9},
        {"symbol": "B", "reason": "same", "cycle": 9},
    ])
    out = _veto_reasons(tmp_path, 9)
    assert out.count("same") == 1, out
    assert "A" in out and "B" in out


def test_no_vetoes_prints_nothing(tmp_path):
    _write(tmp_path / "shadow-ledger.jsonl", [{"symbol": "A", "reason": "x", "cycle": 1}])
    assert _veto_reasons(tmp_path, 2) == ""


def test_a_missing_or_corrupt_ledger_is_not_fatal(tmp_path):
    assert _veto_reasons(tmp_path, 1) == ""
    (tmp_path / "shadow-ledger.jsonl").write_text("not json\n{}\n")
    assert _veto_reasons(tmp_path, 1) == ""


def test_the_tick_prints_it_when_the_gate_vetoed():
    import inspect

    from scripts import auto_cycle
    src = inspect.getsource(auto_cycle.main)
    assert "_veto_reasons(_state, cycle)" in src
    tail = src[src.index("gate: opened"):]
    assert "_veto_reasons" in tail, "must print alongside the gate line, not somewhere unrelated"
