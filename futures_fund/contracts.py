from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from futures_fund.models import Direction, TradeProposal

Lean = Literal["long", "short", "watch"]
Rating = Literal["strong_long", "long", "flat", "short", "strong_short"]
Stance = Literal["bullish", "bearish", "neutral"]


class Candidate(BaseModel):
    symbol: str                       # ccxt unified symbol, e.g. BTC/USDT:USDT
    lean: Lean
    rationale: str
    score: float = Field(ge=0.0, le=1.0)
    correlation_group: str | None = None


class WatcherOutput(BaseModel):
    candidates: list[Candidate] = Field(default_factory=list)


class AnalystReport(BaseModel):
    model_config = ConfigDict(extra="allow")  # tolerate agent-specific signal fields
    agent: str                        # e.g. 'technical', 'derivatives', 'news', 'sentiment'
    symbol: str
    stance: Stance
    confidence: float = Field(ge=0.0, le=1.0)
    key_points: list[str] = Field(default_factory=list)
    signals: dict = Field(default_factory=dict)


class ResearchPlan(BaseModel):
    symbol: str
    rating: Rating
    confidence: float = Field(ge=0.0, le=1.0)
    thesis: str
    falsifiable_prediction: str


class AgentProposal(BaseModel):
    symbol: str                       # raw exchange id, e.g. BTCUSDT (matches SymbolSpec.symbol)
    direction: Direction
    entry: float
    stop: float
    take_profits: list[float]
    atr: float
    confidence: float = Field(ge=0.0, le=1.0)
    horizon_hours: float = 4.0
    rationale: str = ""
    falsifiable_prediction: str = ""  # from the RM plan -> journaled -> tested at HOLD/CLOSE
    confirmation: bool = True         # QuantAgent-style confirmation trigger
    risk_mult: float = 1.0            # optional per-trade risk REDUCTION; gate clamps to (0,1]


Desk = Literal["momentum", "carry", "news"]
EntryStyle = Literal["market", "trigger"]


class CIOAllocation(BaseModel):
    """One trade the CIO funds: direction + a share of the weekly risk budget + entry style. The
    `risk_budget_frac` becomes the Trader's `risk_mult` (gate clamps to (0,1] — reduction-only)."""
    symbol: str                       # raw exchange id, e.g. BTCUSDT
    direction: Direction
    desk: Desk
    conviction: float = Field(ge=0.0, le=1.0)
    risk_budget_frac: float = Field(gt=0.0, le=1.0)
    entry_style: EntryStyle = "market"
    thesis: str = ""
    falsifiable_prediction: str = ""


class CIOOutput(BaseModel):
    """The CIO/Allocator's verdict: ranked allocations + the fast loop's intraday budget/hot-list +
    declined edge-aligned setups (mined by the Reflector for enabling lessons)."""
    allocations: list[CIOAllocation] = Field(default_factory=list)
    intraday_budget_frac: float = Field(default=0.0, ge=0.0, le=1.0)
    hot_list: list[str] = Field(default_factory=list)
    flat_verdicts: list[dict] = Field(default_factory=list)


class ScalperOutput(BaseModel):
    """The fast-loop Scalper's batch: new gate-ready scalp proposals + management of open scalps."""
    proposals: list[AgentProposal] = Field(default_factory=list)
    management: list[dict] = Field(default_factory=list)


class PaceDirective(BaseModel):
    """The Pace Officer's posture, injected verbatim into the CIO/Trader/Scalper prompts."""
    mode: Literal["soft", "normal", "press", "throttle"]
    suggested_risk_mult: float = Field(gt=0.0, le=1.0)
    step_down_active: bool = False
    directive: str = ""


_RATING_DIRECTION: dict[str, Direction] = {
    "strong_long": "long", "long": "long", "short": "short", "strong_short": "short",
}


def rating_to_direction(rating: Rating) -> Direction | None:
    """5-tier research rating -> trade direction. 'flat' -> None (no trade)."""
    return _RATING_DIRECTION.get(rating)


def to_trade_proposal(ap: AgentProposal, funding_rate: float) -> TradeProposal:
    """Convert an agent's structured proposal into the A1 TradeProposal the risk gate consumes."""
    return TradeProposal(
        symbol=ap.symbol, direction=ap.direction, entry=ap.entry, stop=ap.stop,
        take_profits=ap.take_profits, atr=ap.atr, confidence=ap.confidence,
        horizon_hours=ap.horizon_hours, funding_rate=funding_rate, risk_mult=ap.risk_mult,
    )
