"""The monthly review must measure ONE window: the equity curve, the partial-reduce PnL and the
closed-trade decomposition all cover the last `--days`.

Live defect (2026-09-29, cy533): `load_cycle_equity_curve(days, ...)` ignored `days` and loaded all
533 cycles, so the "last 60 days" performance block was all-time. `reduce_realized` is anchored to
that curve's first cycle, so it summed ALL-TIME reduces ($229.28) into a 60-day trade window whose
own reduces were $144.21, overstating the selection edge by ~$85.
"""

import json

from scripts.monthly_review import (
    CYCLES_PER_DAY,
    load_cycle_equity_curve,
    load_reduce_realized,
)


def _state(tmp_path, n_cycles, reduces=None):
    state = tmp_path / "state"
    (state / "cycle").mkdir(parents=True)
    (state / "account.json").write_text(json.dumps({"balance": 10000.0}))
    for c in range(1, n_cycles + 1):
        d = state / "cycle" / str(c)
        d.mkdir()
        (d / "context.json").write_text(json.dumps({"cycle": c, "equity": 10000.0 + c}))
        actions = [{"reduce": "XUSDT", "fraction": 0.5, "pnl": p}
                   for p in (reduces or {}).get(c, [])]
        (d / "report.json").write_text(json.dumps({"cycle": c, "actions": actions}))
    return state


def test_equity_curve_is_limited_to_the_review_window(tmp_path):
    state = _state(tmp_path, 20)
    cycles = load_cycle_equity_curve(2, state)
    assert [c["cycle"] for c in cycles] == list(range(20 - 2 * CYCLES_PER_DAY + 1, 21))


def test_non_positive_days_keeps_the_whole_history(tmp_path):
    state = _state(tmp_path, 20)
    assert len(load_cycle_equity_curve(0, state)) == 20


def test_a_missing_cycle_folder_does_not_widen_the_window(tmp_path):
    state = _state(tmp_path, 20)
    (state / "cycle" / "18" / "context.json").unlink()
    cycles = load_cycle_equity_curve(1, state)
    assert [c["cycle"] for c in cycles] == [15, 16, 17, 19, 20]


def test_the_equity_log_path_is_windowed_too(tmp_path):
    state = _state(tmp_path, 1)
    log = [{"cycle": c, "equity": 10000.0 + c, "time": None} for c in range(1, 21)]
    (state / "equity_log.json").write_text(json.dumps(log))
    cycles = load_cycle_equity_curve(1, state)
    assert [c["cycle"] for c in cycles] == list(range(15, 21))


def test_reduce_pnl_is_anchored_to_the_same_window(tmp_path):
    # main() anchors reduce_realized to the equity curve's first cycle; an unwindowed curve made
    # that anchor cycle 1, pulling out-of-window reduces into the window's edge.
    state = _state(tmp_path, 20, reduces={2: [100.0], 19: [7.5]})
    cycles = load_cycle_equity_curve(1, state)
    first = min(c["cycle"] for c in cycles)
    assert load_reduce_realized(state, since_cycle=first) == 7.5
