"""Phase 2 — Tier-1 EPISODIC recall (anti-press tail-risk brake). Descriptive, not gated: it
surfaces the WORST realised outcomes per (regime x desk x direction) fingerprint so the desk sees
the downside before it presses. The gate never reads it (see test_protected_modules...)."""
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from futures_fund.episodic import (  # noqa: E402
    episodes_by_fingerprint,
    episodic_summary,
    recall_for_context,
    trimmed_mean,
    worst_episodes,
)
from futures_fund.fingerprint import (  # noqa: E402
    describe_fingerprint,
    episode_fingerprint,
    fingerprint_of,
)
from futures_fund.journal import append_decision, patch_outcome  # noqa: E402

_T0 = datetime(2026, 6, 1, tzinfo=UTC)


def _closed(mem, *, cycle, sym, direction, regime, desk, r):
    did = append_decision(mem, {"cycle": cycle, "symbol": sym, "direction": direction,
                                "entry": 100.0, "stop": 110.0, "size": 1.0, "ts": _T0})
    patch_outcome(mem, did, {"exit_ts": _T0, "realized_pnl": r * 10.0, "r_multiple": r,
                             "regime": regime, "desk": desk,
                             "close_reason": "stop" if r < 0 else "tp"})
    return did


def _short(mem, c, r, sym=None, regime="risk_off", desk="momentum"):
    return _closed(mem, cycle=c, sym=sym or f"S{c}", direction="short", regime=regime,
                   desk=desk, r=r)


def test_fingerprint_is_canonical_and_collapses_missing():
    assert episode_fingerprint("risk_off", "momentum", "short") == "risk_off|momentum|short"
    assert episode_fingerprint(None, "", "LONG") == "any|any|long"     # None/'' -> 'any', lower
    rec = {"regime": "risk_on", "desk": "carry", "direction": "long"}
    assert fingerprint_of(rec) == "risk_on|carry|long"
    assert "SHORT" in describe_fingerprint("risk_off|momentum|short")


def test_trimmed_mean_drops_both_tails():
    # one fat winner and one fat loser must not dominate the central read
    assert trimmed_mean([-9.0, 0.1, 0.2, 0.3, 9.0], trim=0.2) == 0.2   # drops -9 and +9
    assert trimmed_mean([]) == 0.0


def test_worst_episodes_returns_most_negative_first(tmp_path):
    mem = tmp_path / "memory"
    _short(mem, 1, -0.2)
    _short(mem, 2, -0.9)
    _short(mem, 3, +0.5)
    eps = episodes_by_fingerprint(mem)["risk_off|momentum|short"]
    worst = worst_episodes(eps, k=2)
    assert [round(w["r_multiple"], 2) for w in worst] == [-0.9, -0.2]   # worst first


def test_episodic_summary_surfaces_the_tail(tmp_path):
    mem = tmp_path / "memory"
    for c, r in [(1, -0.4), (2, -0.5), (3, -1.0), (4, +0.6)]:
        _short(mem, c, r)
    s = episodic_summary(episodes_by_fingerprint(mem)["risk_off|momentum|short"])
    assert s["n"] == 4 and s["wins"] == 1 and abs(s["win_rate"] - 0.25) < 1e-9
    assert abs(s["worst_r"] - (-1.0)) < 1e-9 and abs(s["best_r"] - 0.6) < 1e-9
    assert s["worst_examples"][0]["r_multiple"] == -1.0                 # most-negative first


def test_recall_block_is_anti_press_and_thresholded(tmp_path):
    mem = tmp_path / "memory"
    # a fingerprint with 3 closed (above min_n) -> a recall block; a singleton -> withheld
    for c, r in [(1, -0.4), (2, -0.5), (3, +0.3)]:
        _short(mem, c, r)
    _closed(mem, cycle=9, sym="LONELY", direction="long", regime="risk_on", desk="carry", r=-0.7)
    blocks = recall_for_context(mem, min_n=2)
    fps = {b["fingerprint"] for b in blocks}
    assert "risk_off|momentum|short" in fps             # n=3 >= min_n
    assert "risk_on|carry|long" not in fps              # singleton withheld
    blk = next(b for b in blocks if b["fingerprint"] == "risk_off|momentum|short")
    assert "PRESS" in blk["text"] and "TAIL-RISK" in blk["text"] and "-0.50R" in blk["text"]


def test_recall_orders_most_dangerous_first(tmp_path):
    mem = tmp_path / "memory"
    for c in (1, 2):
        _closed(mem, cycle=c, sym="A", direction="long", regime="risk_on", desk="momentum", r=-0.3)
    for c in (3, 4):
        _closed(mem, cycle=c, sym="B", direction="short", regime="risk_on", desk="carry", r=-1.5)
    blocks = recall_for_context(mem, min_n=2)
    assert blocks[0]["fingerprint"] == "risk_on|carry|short"   # worst_r -1.5 ranks first


def test_recall_is_failsafe_on_empty(tmp_path):
    assert recall_for_context(tmp_path / "memory") == []
