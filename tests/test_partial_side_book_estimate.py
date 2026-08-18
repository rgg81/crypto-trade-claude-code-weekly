"""A mid-rotation side must not collapse the achievable book.

Adversarial-review finding (HIGH), reproduced from `state/cycle/295/*`. `deployment_resizes`'s
plan-aware branch deliberately reasons per SLOT:

    per_slot = book / n_per_side
    incoming = {d: planned_opens_by_side.get(d, 0) * per_slot ...}

but `book` itself is still summed over KEPT legs only:

    present = [sum(_ceil(s) for s in legs) for legs in sides.values() if legs]
    book    = min([equity / 2.0, *present])

So on a rotation cycle a side holding k < n_per_side legs contributes only k slots' worth of
ceiling, while every other term is measured against a full n_per_side book. At cy295 the short
sleeve was mid-rotation with one kept leg (XRP, ceiling $1745.57), which pinned

    book = 1745.57   (should be ~2492, the LONG sleeve's true ceiling sum)

and the band test then read the long sleeve's $1481.85 as "already at 85% of target — no churn".
`deployment_resizes` returned set(), the long sleeve stayed frozen at 0.147x deployment, and the
symmetric trim in `neutral_book` consequently starved the incoming shorts into the dust floor.
That is the UPSTREAM cause of the cy295 FLAT violation; the pre-sizer fix only stopped it from
becoming a naked book.

The estimate for an unopened slot has to come from somewhere. Using the kept legs' AVERAGE ceiling
is data-driven rather than assumed, and `book` stays capped by equity/2 and by the opposite side,
so a partial side can never inflate the book beyond what the weaker side truly supports.
"""
import pytest

from futures_fund.blended_score import deployment_resizes

EQ = 10069.89
N = 3
PTR = 0.0025                     # caution tier x -5% step-down breaker x 0.010 quadrant
BAND = 0.30

# The live cy295 book: three kept longs, one kept short mid-rotation, two shorts rotating in.
HOLDINGS = {"BTWUSDT": "long", "LINKUSDT": "long", "SOLUSDT": "long", "XRPUSDT": "short"}
STOP_FRAC = {"BTWUSDT": 0.21438, "LINKUSDT": 0.02995, "SOLUSDT": 0.01641, "XRPUSDT": 0.014421}
NOTIONAL = {"BTWUSDT": 78.90, "LINKUSDT": 563.72, "SOLUSDT": 839.23, "XRPUSDT": 700.00}
PLANNED = {"long": 0, "short": 2}


def _resize(**kw):
    kw.setdefault("holdings", HOLDINGS)
    kw.setdefault("notional_by_sym", NOTIONAL)
    kw.setdefault("planned_opens_by_side", PLANNED)
    return deployment_resizes(
        kw.pop("holdings"), kw.pop("notional_by_sym"), EQ, N, band=BAND,
        per_trade_risk_pct=PTR, stop_frac_by_sym=STOP_FRAC,
        planned_opens_by_side=kw.pop("planned_opens_by_side"), **kw)


def test_the_frozen_long_sleeve_is_flagged_for_top_up():
    """THE REGRESSION. cy295 returned set() and left the longs at 0.147x."""
    resize = _resize()
    assert resize, "mid-rotation short side collapsed `book` -> long sleeve frozen forever"
    assert "LINKUSDT" in resize and "BTWUSDT" in resize, sorted(resize)


def test_a_leg_already_at_its_own_ceiling_is_never_flagged():
    """No churn for a leg that physically cannot grow: BTW's 21.4% stop pins it at $117.42, so
    once it is there it must stop being flagged (this is what makes the top-up converge)."""
    at_ceiling = {**NOTIONAL, "BTWUSDT": PTR * EQ / STOP_FRAC["BTWUSDT"]}
    assert "BTWUSDT" not in _resize(notional_by_sym=at_ceiling)


def test_the_top_up_converges_rather_than_churning():
    """Reopening the flagged legs at their landed size must clear the flag — otherwise this is a
    permanent 4h close+reopen loop paying 0.14% a lap (HARD RULE 2)."""
    name_cap = 0.25 * EQ
    filled = dict(NOTIONAL)
    for _ in range(6):
        resize = _resize(notional_by_sym=filled)
        if not resize:
            break
        for s in resize:                      # reopen at the size the pre-sizer would land
            ceil = min(name_cap, PTR * EQ / STOP_FRAC[s])
            filled[s] = min(ceil, _per_slot(filled))
    else:
        pytest.fail(f"never converged; still flagging {sorted(_resize(notional_by_sym=filled))}")
    assert filled["LINKUSDT"] > NOTIONAL["LINKUSDT"], "top-up must actually grow the sleeve"


def _per_slot(notional):
    """book/n_per_side, mirroring the function under test."""
    name_cap = 0.25 * EQ
    sides = {d: [s for s in HOLDINGS if HOLDINGS[s] == d] for d in ("long", "short")}
    present = []
    for d, legs in sides.items():
        if not legs:
            continue
        csum = sum(min(name_cap, PTR * EQ / STOP_FRAC[s]) for s in legs)
        slots = min(N, len(legs) + PLANNED.get(d, 0))
        present.append(csum / len(legs) * slots)
    return min([EQ / 2.0, *present]) / N


def test_a_full_book_is_unchanged():
    """Both sides at n_per_side: no scaling applies, so behaviour matches the old code exactly."""
    holdings = {**HOLDINGS, "ADAUSDT": "short", "DOTUSDT": "short"}
    stop = {**STOP_FRAC, "ADAUSDT": 0.02, "DOTUSDT": 0.03}
    notional = {**NOTIONAL, "ADAUSDT": 800.0, "DOTUSDT": 800.0}
    full = deployment_resizes(holdings, notional, EQ, N, band=BAND, per_trade_risk_pct=PTR,
                              stop_frac_by_sym=stop,
                              planned_opens_by_side={"long": 0, "short": 0})
    # every long is far under its share -> flagged; nothing about the scaling changed that
    assert "BTWUSDT" in full and "LINKUSDT" in full


def test_a_well_deployed_book_still_refuses_to_churn():
    """The band must still protect a book that is genuinely near target."""
    name_cap = 0.25 * EQ
    near = {s: min(name_cap, PTR * EQ / STOP_FRAC[s]) for s in HOLDINGS}
    assert _resize(notional_by_sym=near) == set()


@pytest.mark.parametrize("notional,flagged", [(1500.0, True), (1520.0, False)])
def test_the_book_never_exceeds_the_neutral_half(notional, flagged):
    """A partial side is scaled UP by this fix — pin that it can never inflate `book` past
    equity/2, which would size the desk above 1x gross.

    Every leg has a near-zero stop, so each ceiling is the $2517.47 per-name cap. The long side
    holds all three slots ($7552.42); the short side holds one and plans two, so the average-ceiling
    scaling also wants $7552.42. `book` must clamp to equity/2 = $5034.95, giving per_slot $1678.32
    and a flag threshold of $1510.48. The lone $10 short keeps `deployed` at $3366.63 — under the
    $3524.46 band — so the band cannot short-circuit and the threshold is what decides.

    Bracketing $1510.48 is what pins the clamp: an UNCLAMPED book ($7552.42) would put per_slot at
    $2517.47 and a threshold at $2265.72, flagging both notionals.
    """
    holdings = {"AAAUSDT": "long", "BBBUSDT": "long", "CCCUSDT": "long", "SSSUSDT": "short"}
    sf = dict.fromkeys(holdings, 0.0001)                 # near-zero stops -> ceiling == name_cap
    notional_by_sym = {s: notional for s in holdings if holdings[s] == "long"}
    notional_by_sym["SSSUSDT"] = 10.0
    resize = deployment_resizes(holdings, notional_by_sym, EQ, N, band=BAND,
                                per_trade_risk_pct=PTR, stop_frac_by_sym=sf,
                                planned_opens_by_side={"long": 0, "short": 2})
    threshold = min((EQ / 2.0) / N, 0.25 * EQ) * 0.90
    assert threshold == pytest.approx(1510.48, abs=0.01)
    assert ("AAAUSDT" in resize) is flagged, (
        f"notional {notional} vs clamped threshold {threshold:.2f}; got {sorted(resize)}")
