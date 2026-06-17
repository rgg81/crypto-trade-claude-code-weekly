"""HARD INVARIANT (TEMPEST-LEARN): the deterministic gate is the sole risk authority and must NEVER
read the self-learning corpus. Lessons/episodic memory shape AGENT JUDGMENT only; if a protected
module ever IMPORTED the learning layer it could let a 'press' lesson tug sizing or a 'don't' lesson
veto an exit — collapsing the gate's independence. This guard inspects IMPORT statements (not local
names like `audit_and_reflect`, which is the exit sweep, nor benign comments) and fails loudly the
moment any protected module takes a dependency on the learning layer."""
import re
from pathlib import Path

import pytest

_PROTECTED = ["risk_gate", "sizing", "liquidation", "consolidation",
              "executor", "exits", "policy", "cycle"]
# learning-layer modules the gate must never depend on
_FORBIDDEN = ["lessons", "reflect", "reflect_miner", "reflect_runner",
              "attribution", "episodic", "graduation"]
_IMPORT_RE = re.compile(r"^\s*(?:from\s+\S+\s+import\b|import\b)(.*)$")


@pytest.mark.parametrize("mod", _PROTECTED)
def test_protected_module_does_not_import_learning_layer(mod):
    src = Path(__file__).resolve().parents[1] / "futures_fund" / f"{mod}.py"
    bad: list[str] = []
    for line in src.read_text().splitlines():
        m = _IMPORT_RE.match(line)
        if not m:
            continue
        # match the imported module path, e.g. `from futures_fund.lessons import ...`
        for tok in _FORBIDDEN:
            if re.search(rf"\b{tok}\b", line):
                bad.append(line.strip())
                break
    assert not bad, (f"PROTECTED module {mod}.py IMPORTS the learning layer: {bad} — the gate must "
                     f"never read lessons/episodic memory (they shape agent judgment only).")
