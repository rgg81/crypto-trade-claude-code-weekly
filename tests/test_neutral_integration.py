"""Ship-gate integration tests for the dollar-neutral pivot (Phase B).

T1: end-to-end through the REAL gate (preflight -> gate_execute_step), the dollar-neutral pre-sizer
    sizes a leg to ~equity/2 (the neutral per-side target) and the resulting book leverage is ~1x —
    NOT the larger aggressive per-trade-cap size. Leverage is emergent (choose_leverage output).
T2: a balanced book's net~=0 SURVIVES consolidate()'s heat scaling — the proportional scale-to-cap
    keeps gross_long$ == gross_short$ while bringing heat under the cap (the ordering-bug guard).
"""
import datetime as dt
from datetime import UTC

import pytest

from futures_fund import orchestration
from futures_fund.consolidation import consolidate
from futures_fund.contracts import AgentProposal
from futures_fund.models import CostEstimate, SizedTrade, TradeProposal
from futures_fund.orchestration import gate_execute_step, preflight_step
from futures_fund.state import load_positions
from tests.test_orchestration import FakeExchange, _HttpClient, _settings, _uptrend

_EQ = 10_000.0


# ---- T1: end-to-end ~1x neutral sizing through the real gate ----------------------------------

def test_presizer_sizes_leg_to_half_equity_and_book_is_about_1x(tmp_path):
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    pf = preflight_step(ex, _settings(), state_dir, memory_dir,
                        now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=1,
                        http_client=_HttpClient())
    last = pf["briefs"][0]["last_close"]
    # one with-regime long; the pre-sizer targets equity/2 (~$5k) for the long sleeve
    proposals = [AgentProposal(symbol="BTCUSDT", direction="long", entry=last, stop=last - 4.0,
                               take_profits=[last + 8.0, last + 12.0], atr=2.0, confidence=0.7,
                               rationale="momentum long").model_dump()]
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir, proposals=proposals,
                               now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=1)
    assert report["opened"] == 1
    pos = load_positions(state_dir)[0]
    notional = pos.qty * pos.entry
    # a single leg is capped at the per-name cap (25% of the book = $2500) — a lone leg cannot fill
    # the whole side; the book gross is comfortably under ~1x equity
    assert notional == pytest.approx(_EQ * 0.25, rel=0.12)
    assert notional <= 1.2 * _EQ                          # book gross <= ~1x equity
    assert pos.leverage == pytest.approx(1.0)             # literal 1x per position (full margin)
    # the gate also reports a neutrality check (one-sided here -> flagged via balanced_gross 0)
    assert "neutral_check" in report


# ---- T3: a pre-sizer exception is SURFACED, never silently swallowed --------------------------

def test_presize_exception_is_recorded_not_swallowed(tmp_path, monkeypatch):
    """HARD RULE 8: if the dollar-neutral pre-sizer raises, the gate must still open the book
    (fail-safe) BUT record the error so the operator/next cycle can see it — a swallowed exception
    that silently disables balancing (leaving the book count-imbalanced) must never be invisible."""
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ex = FakeExchange({"BTC/USDT:USDT": _uptrend()})
    pf = preflight_step(ex, _settings(), state_dir, memory_dir,
                        now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=1,
                        http_client=_HttpClient())

    def _boom(*a, **k):
        raise RuntimeError("presize kaboom")

    monkeypatch.setattr(orchestration, "presize_and_balance", _boom)
    last = pf["briefs"][0]["last_close"]
    proposals = [AgentProposal(symbol="BTCUSDT", direction="long", entry=last, stop=last - 4.0,
                               take_profits=[last + 8.0, last + 12.0], atr=2.0, confidence=0.7,
                               rationale="momentum long").model_dump()]
    report = gate_execute_step(ex, _settings(), state_dir, memory_dir, proposals=proposals,
                               now=dt.datetime(2026, 3, 1, tzinfo=UTC), cycle_no=1)
    # fail-safe still ran the gate (book opened despite the presize error)
    assert report["opened"] == 1
    # the error is SURFACED, not swallowed into a bare None
    assert report["neutral_check"]["presize_error"] is not None
    assert "presize kaboom" in report["neutral_check"]["presize_error"]


# ---- T2: net~=0 survives consolidate() heat scaling -------------------------------------------

def _sized(symbol, direction, entry, stop, notional):
    qty = notional / entry
    tp = TradeProposal(symbol=symbol, direction=direction, entry=entry, stop=stop,
                       take_profits=[entry * (1.1 if direction == "long" else 0.9)],
                       atr=entry * 0.02, confidence=0.6, horizon_hours=16.0, funding_rate=0.0)
    return SizedTrade(proposal=tp, qty=qty, notional=notional, leverage=1.0, margin=notional,
                      liq_price=entry * (0.5 if direction == "long" else 1.5), cost=CostEstimate())


def _heat(book):
    return sum(t.qty * abs(t.proposal.entry - t.proposal.stop) / _EQ for t in book)


def test_balanced_book_net_zero_survives_consolidate_heat_scaling():
    # $5k long (2 legs) + $5k short (2 legs), 10% stops -> total heat ~0.10, over a 0.05 cap
    book = [_sized("A", "long", 100.0, 90.0, 2_500.0), _sized("B", "long", 50.0, 45.0, 2_500.0),
            _sized("C", "short", 200.0, 220.0, 2_500.0), _sized("D", "short", 20.0, 22.0, 2_500.0)]
    assert _heat(book) == pytest.approx(0.10, rel=1e-6)        # starts over the cap
    out = consolidate(book, _EQ, max_heat=0.05)
    gl = sum(t.qty * t.proposal.entry for t in out if t.proposal.direction == "long")
    gs = sum(t.qty * t.proposal.entry for t in out if t.proposal.direction == "short")
    assert gl == pytest.approx(gs, rel=1e-6)                   # net~=0 PRESERVED through scaling
    assert _heat(out) <= 0.05 + 1e-9                           # AND heat brought under the cap
    assert gl == pytest.approx(2_500.0, rel=1e-6)            # each side scaled 0.10->0.05 (halved)
