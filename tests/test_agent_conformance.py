import json
from datetime import UTC
from pathlib import Path

import pytest

from futures_fund.contracts import (
    AgentProposal,
    AnalystReport,
    CIOOutput,
    ResearchPlan,
    ScalperOutput,
    WatcherOutput,
)
from futures_fund.lessons import Lesson

FIX = Path(__file__).parent / "fixtures" / "agent_examples"


def _load(name):
    return json.loads((FIX / name).read_text())


def test_watcher_example_conforms():
    WatcherOutput.model_validate(_load("watcher.json"))


@pytest.mark.parametrize(
    "name", ["technical.json", "derivatives.json", "news.json"]
)
def test_analyst_examples_conform(name):
    r = AnalystReport.model_validate(_load(name))
    assert r.stance in {"bullish", "bearish", "neutral"}


def test_research_plan_example_conforms():
    p = ResearchPlan.model_validate(_load("research_plan.json"))
    assert p.rating in {"strong_long", "long", "flat", "short", "strong_short"}
    assert p.falsifiable_prediction


def test_trader_example_conforms():
    ap = AgentProposal.model_validate(_load("trader.json"))
    assert ap.symbol == "BTCUSDT" and ap.direction == "long"


@pytest.mark.parametrize("name", ["momentum.json", "carry.json", "sentiment.json"])
def test_specialist_desk_examples_conform(name):
    data = _load(name)
    reports = [AnalystReport.model_validate(r) for r in data["reports"]]
    assert reports and all(r.stance in {"bullish", "bearish", "neutral"} for r in reports)
    assert reports[0].agent == name.removesuffix(".json")  # 'momentum' / 'carry' / 'sentiment'


def test_sentiment_surfaces_structured_signal():
    # the Sentiment desk must SURFACE its read (not keep it LLM-internal): a visible crowd-mood
    # + macro signal the CIO can weigh symmetrically.
    rep = AnalystReport.model_validate(_load("sentiment.json")["reports"][0])
    assert "social_tone" in rep.signals and "fear_greed" in rep.signals


def test_cio_example_conforms():
    out = CIOOutput.model_validate(_load("cio.json"))
    assert out.allocations and out.allocations[0].risk_budget_frac <= 1.0
    assert 0.0 <= out.intraday_budget_frac <= 1.0


def test_scalper_example_conforms():
    out = ScalperOutput.model_validate(_load("scalper.json"))
    # scalper emits gate-ready AgentProposals directly + management of open scalps
    assert all(p.direction in {"long", "short"} for p in out.proposals)
    assert out.management and out.management[0]["action"] in {"hold", "close", "reduce"}


def test_reflector_example_lessons_conform():
    data = _load("reflector.json")
    from datetime import datetime
    for lz in data["lessons"]:
        Lesson.model_validate({**lz, "ts": datetime(2026, 5, 1, tzinfo=UTC)})
