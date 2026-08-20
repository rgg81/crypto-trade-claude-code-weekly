"""A name the gate can never accept must not take a sleeve slot.

BTWUSDT was ranked into a sleeve at cy305 (long, 43.45% stop) and cy307 (short, 41.90% stop). Both
times `risk_gate` vetoed it and the book executed one leg short — L2/S3, then L3/S2. The pre-sizer
passed it through untouched each time (`n_dropped: 0`, `heat_dropped: []`), so nothing upstream
noticed; the slot was simply lost.

The veto is correct and non-negotiable: `risk_gate.MIN_LIQ_DISTANCE_MULT = 2.5` requires the
liquidation price to sit at least 2.5 stop-distances from entry. At the desk's 1x leverage cap the
liquidation sits 100.00% away for a long and 90.48% for a short, so

    max stop fraction = liq_distance_fraction / 2.5   ->   40.000% long, 36.190% short

Anything wider is structurally un-openable — not marginal, not size-dependent, never admissible at
any risk_mult. Ranking such a name costs a slot every cycle it appears.

This filters the SELECTION layer only. It never weakens the gate: the ceiling is DERIVED from the
gate's own constant and liquidation helpers, so if the gate tightens, this tightens with it.
"""
import pytest

from futures_fund.blended_score import admissible_stop_frac
from futures_fund.risk_gate import MIN_LIQ_DISTANCE_MULT

BTW_LONG = 0.4345          # live cy305
BTW_SHORT = 0.4190         # live cy307


def test_the_ceiling_matches_the_gates_own_arithmetic():
    assert admissible_stop_frac("long") == pytest.approx(0.40, abs=1e-6)
    assert admissible_stop_frac("short") == pytest.approx(0.361905, abs=1e-5)


def test_the_ceiling_is_derived_from_the_gate_constant_not_hardcoded():
    """If MIN_LIQ_DISTANCE_MULT ever tightens, the filter must tighten with it."""
    base = admissible_stop_frac("long")
    assert base * MIN_LIQ_DISTANCE_MULT == pytest.approx(1.0, abs=1e-6), (
        "long liq sits 100% away at 1x, so ceiling * mult must be exactly the liq distance")


def test_the_live_btw_proposals_are_both_rejected():
    assert BTW_LONG > admissible_stop_frac("long")
    assert BTW_SHORT > admissible_stop_frac("short")


def test_ordinary_names_are_untouched():
    """Every other leg the desk has traded sits far under the ceiling — this must not thin the
    universe. Live stop fractions from cy306/cy307."""
    for sf in (0.016441, 0.027779, 0.028160, 0.042299, 0.058398, 0.049188):
        assert sf < admissible_stop_frac("short") < admissible_stop_frac("long")


def test_shorts_are_held_to_the_tighter_ceiling():
    """A short liquidates nearer than a long at the same leverage, so its ceiling is lower. A name
    admissible long may be inadmissible short."""
    assert admissible_stop_frac("short") < admissible_stop_frac("long")
    between = (admissible_stop_frac("short") + admissible_stop_frac("long")) / 2
    assert between < admissible_stop_frac("long")
    assert between > admissible_stop_frac("short")


def test_an_unknown_direction_is_treated_conservatively():
    """Never fail open: an unrecognised direction must get the STRICTER ceiling, not a free pass."""
    assert admissible_stop_frac("sideways") == pytest.approx(admissible_stop_frac("short"))
    assert admissible_stop_frac(None) == pytest.approx(admissible_stop_frac("short"))


def test_a_higher_leverage_cap_would_relax_it_proportionally():
    """Sanity on the derivation: more leverage moves liquidation closer, tightening the ceiling."""
    assert admissible_stop_frac("long", leverage=2.0) < admissible_stop_frac("long", leverage=1.0)


def test_the_planner_never_opens_a_wide_stop_name():
    """BEHAVIOURAL. Run the real planner over the live cy307 artifacts and assert BTWUSDT cannot
    reach either sleeve's open list.

    An earlier version of this test only grepped `main`'s source for the helper name, which
    survived stubbing the filter out with `if False:` — the same source-inspection weakness that
    let the cy296 exit-code bug ship. This one executes the planner and fails when the filter is
    disabled.
    """
    import json
    import os
    import shutil
    import subprocess
    import sys
    import tempfile

    src = "state/cycle/307/context.json"
    if not os.path.exists(src):
        pytest.skip("live cycle artifacts not present")
    with tempfile.TemporaryDirectory() as td:
        cdir = os.path.join(td, "cycle", "307")
        os.makedirs(cdir)
        shutil.copy(src, cdir)
        for extra in ("universe.json",):
            p = os.path.join("state", "cycle", "307", extra)
            if os.path.exists(p):
                shutil.copy(p, cdir)
        shutil.copy("state/positions.json", td)
        r = subprocess.run([sys.executable, "scripts/blended_book_cli.py", "--cycle", "307",
                            "--state", td], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[-2000:]
        plan = json.loads(r.stdout)["plan"]
    opens = set(plan["open_long"]) | set(plan["open_short"])
    assert "BTWUSDT" not in opens, (
        f"planner still routes BTWUSDT (41.9-43.5% stop) into a sleeve the gate always vetoes: "
        f"{sorted(opens)}")


def test_the_structurer_and_the_filter_share_one_atr_multiple():
    """A mismatch would filter on a stop the desk never actually builds."""
    import inspect

    from scripts import blended_book_cli as cli
    assert cli._ATR_MULT == 2.0
    assert inspect.signature(cli._structure).parameters["atr_mult"].default == cli._ATR_MULT


def test_the_live_cy307_universe_would_have_dropped_only_btw():
    """Replay the real briefs: exactly one name is filtered, so the sleeve gains a slot rather
    than the universe being thinned."""
    import json
    import os

    p = "state/cycle/307/context.json"
    if not os.path.exists(p):
        pytest.skip("live cycle artifacts not present")
    from scripts.blended_book_cli import _ATR_MULT, _raw
    briefs = [{**b, "symbol": _raw(b["symbol"])} for b in json.load(open(p))["briefs"]]
    ceiling = admissible_stop_frac(None)
    wide = {b["symbol"] for b in briefs
            if b.get("atr") and b.get("last_close")
            and _ATR_MULT * float(b["atr"]) / float(b["last_close"]) > ceiling}
    assert wide == {"BTWUSDT"}, f"expected only BTW filtered, got {sorted(wide)}"
    assert len(briefs) - len(wide) >= 6, "enough names must remain to fill both sleeves"
