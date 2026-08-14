"""The monthly review must measure whether the EDGE covers its own TURNOVER COST.

HARD RULE 6 says every leg must clear its round-trip cost. Nothing was actually checking that: the
churn section keyed off a `plan.json` the deterministic path never writes, so it printed
"No rotation data found" and the review passed a book whose price-selection edge covered only part
of its fees. The decisions journal records every closed trade's realized_pnl / fees / funding, so
the review decomposes lifetime PnL into:

    gross price PnL - entry fees - exit fees + funding carry  ==  net

Sign convention (futures_fund/exits.py): `funding` is POSITIVE when we PAID it and NEGATIVE when we
RECEIVED a credit, and `realized = gross - exit_fee - funding`. The journal stores it as
`funding_paid`, so a carry-collecting desk shows funding_paid < 0.

Adversarial-review fixes covered here:
  1. PARTIAL REDUCES never reach the journal (`reduce.py` banks a slice via close_at_mark and the
     gate credits the wallet, but no path calls patch_outcome). Sourced instead from the cycle
     reports' `{"reduce": SYM, "pnl": ...}` actions — a reporting-layer read, no trading-path edit.
  2. The "carry is doing the work" verdict must not fire when carry is a net COST.
  3. A record with no size/entry contributes ZERO entry fee, understating fees and biasing the
     ratio toward a FALSE PASS — so a pass is only trustworthy when every notional is known.
  5. The decomposition must respect the review window, else the flag ratchets and never clears.
"""
import json

import pytest

from scripts.monthly_review import (
    carry_is_income,
    cost_decomposition,
    edge_covers_costs,
    filter_to_window,
    load_reduce_realized,
)


def _trade(realized, fees, funding_paid, size, entry, exit_ts=None):
    t = {"realized_pnl": realized, "fees": fees, "funding_paid": funding_paid,
         "size": size, "entry": entry}
    if exit_ts:
        t["exit_ts"] = exit_ts
    return t


# ---------------------------------------------------------------- core identity

def test_decomposition_reconstructs_gross_from_realized():
    """gross = realized + exit_fee + funding (the exits.py identity, inverted)."""
    d = cost_decomposition([_trade(10.0, 2.0, -3.0, 1.0, 100.0)])
    assert d["gross_price_pnl"] == pytest.approx(9.0)
    assert d["exit_fees"] == pytest.approx(2.0)
    assert d["funding_carry"] == pytest.approx(3.0)      # reported as a positive CREDIT


def test_funding_paid_is_reported_as_a_cost_not_a_credit():
    d = cost_decomposition([_trade(10.0, 2.0, 4.0, 1.0, 100.0)])
    assert d["funding_carry"] == pytest.approx(-4.0)


def test_entry_fees_are_included_even_though_realized_pnl_excludes_them():
    """realized_pnl nets only the EXIT fee; counting one side halves the true turnover cost."""
    d = cost_decomposition([_trade(0.0, 1.0, 0.0, 10.0, 100.0)])
    assert d["entry_fees"] > 0
    assert d["total_fees"] == pytest.approx(d["entry_fees"] + d["exit_fees"])


def test_net_equals_gross_minus_fees_plus_carry():
    trades = [_trade(10.0, 2.0, -3.0, 1.0, 100.0), _trade(-5.0, 1.5, 1.0, 2.0, 50.0)]
    d = cost_decomposition(trades)
    assert d["net"] == pytest.approx(
        d["gross_price_pnl"] - d["total_fees"] + d["funding_carry"])
    assert d["trades"] == 2


# ------------------------------------------------- finding 1: partial reduces

def test_reduce_slices_are_added_to_the_edge(tmp_path):
    """69 live reduces banked +$107.69 that the journal never saw — omitting them understated
    the measured edge by ~42% and flipped `net` negative."""
    d = cost_decomposition([_trade(10.0, 2.0, -3.0, 1.0, 100.0)], reduce_realized=107.69)
    assert d["reduce_realized"] == pytest.approx(107.69)
    assert d["gross_price_pnl"] == pytest.approx(9.0 + 107.69)


def test_reduce_realized_defaults_to_zero_when_absent():
    d = cost_decomposition([_trade(10.0, 2.0, -3.0, 1.0, 100.0)])
    assert d["reduce_realized"] == 0.0


def test_load_reduce_realized_sums_only_reduce_actions(tmp_path):
    cd = tmp_path / "cycle"
    for n, actions in ((1, [{"reduce": "BNBUSDT", "fraction": 0.4, "pnl": 16.66, "full": False}]),
                       (2, [{"close": "ETHUSDT", "reason": "holdings_close", "pnl": 99.0}]),
                       (3, [{"reduce": "XRPUSDT", "fraction": 0.3, "pnl": -4.0, "full": False},
                            {"open": "SOLUSDT", "direction": "long"}])):
        (cd / str(n)).mkdir(parents=True)
        (cd / str(n) / "report.json").write_text(json.dumps({"cycle": n, "actions": actions}))
    assert load_reduce_realized(tmp_path) == pytest.approx(12.66)   # 16.66 - 4.0, close ignored


def test_load_reduce_realized_tolerates_missing_or_corrupt_reports(tmp_path):
    cd = tmp_path / "cycle" / "1"
    cd.mkdir(parents=True)
    (cd / "report.json").write_text("{not json")
    assert load_reduce_realized(tmp_path) == 0.0
    assert load_reduce_realized(tmp_path / "nope") == 0.0


# ------------------------------------- finding 3: unknown notional must not pass

def test_unknown_notional_records_are_counted():
    d = cost_decomposition([_trade(5.0, 1.0, 0.0, 0.0, 0.0), _trade(5.0, 1.0, 0.0, 1.0, 10.0)])
    assert d["unknown_notional"] == 1


def test_pass_is_withheld_when_a_notional_is_unknown():
    """Missing size => entry fee undercounted => fees understated => ratio OVERSTATED. A 'pass'
    on incomplete data is exactly the false green the docstring forbids."""
    d = {"gross_price_pnl": 900.0, "total_fees": 100.0, "unknown_notional": 3}
    assert edge_covers_costs(d) is None


def test_failure_verdict_is_still_trustworthy_with_unknown_notional():
    """Understated fees only flatter the ratio — so if it is STILL below 1, that holds."""
    d = {"gross_price_pnl": 50.0, "total_fees": 100.0, "unknown_notional": 3}
    assert edge_covers_costs(d) is False


def test_pass_is_reported_when_every_notional_is_known():
    assert edge_covers_costs(
        {"gross_price_pnl": 500.0, "total_fees": 465.96, "unknown_notional": 0}) is True


def test_edge_covers_costs_false_when_fees_exceed_gross_edge():
    assert edge_covers_costs(
        {"gross_price_pnl": 149.39, "total_fees": 465.96, "unknown_notional": 0}) is False


def test_empty_journal_does_not_divide_by_zero():
    d = cost_decomposition([])
    assert d["trades"] == 0
    assert d["edge_cover_ratio"] is None
    assert edge_covers_costs(d) is None      # unknown, not a false pass


# ------------------------------------------- finding 2: carry sign in the verdict

def test_carry_is_income_only_when_positive():
    assert carry_is_income({"funding_carry": 294.35}) is True
    assert carry_is_income({"funding_carry": -50.0}) is False
    assert carry_is_income({"funding_carry": 0.0}) is False


# --------------------------------------------- finding 5: respect review window

def test_filter_to_window_keeps_only_recent_exits():
    trades = [_trade(1.0, 0.0, 0.0, 1.0, 1.0, exit_ts="2026-08-10T00:00:00+00:00"),
              _trade(2.0, 0.0, 0.0, 1.0, 1.0, exit_ts="2026-06-01T00:00:00+00:00")]
    kept = filter_to_window(trades, days=30, now="2026-08-14T00:00:00+00:00")
    assert len(kept) == 1
    assert kept[0]["realized_pnl"] == 1.0


def test_filter_to_window_keeps_records_with_unparseable_exit_ts():
    """Dropping them would silently shrink the measured cost base."""
    kept = filter_to_window([_trade(1.0, 0.0, 0.0, 1.0, 1.0, exit_ts="garbage")],
                            days=30, now="2026-08-14T00:00:00+00:00")
    assert len(kept) == 1


# ------------------------------------------------------------------ robustness

def test_trades_missing_optional_fields_are_tolerated():
    d = cost_decomposition([{"realized_pnl": 5.0}])
    assert d["trades"] == 1
    assert isinstance(d["gross_price_pnl"], float)


def test_non_numeric_field_does_not_raise():
    """finding 4: a corrupt record must not wedge the driver into a re-failing review loop."""
    d = cost_decomposition([{"realized_pnl": "oops", "size": None, "fees": "x"}])
    assert d["trades"] == 1
