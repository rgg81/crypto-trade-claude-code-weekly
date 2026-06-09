from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class ExchangeSettings(BaseModel):
    testnet: bool = True
    key_env: str = "BINANCE_KEY"
    secret_env: str = "BINANCE_SECRET"

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.key_env)

    @property
    def api_secret(self) -> str | None:
        return os.environ.get(self.secret_env)


class DataSettings(BaseModel):
    # Keyless crypto-news RSS feeds (each degrades independently; a dead/blocked source is skipped).
    # Broadened beyond coindesk+cointelegraph so the News analyst sees more of the tape.
    news_rss_sources: list[str] = Field(default_factory=lambda: [
        "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml",
        "https://cointelegraph.com/rss",
        "https://decrypt.co/feed",
        "https://www.cryptoslate.com/feed/",
        "https://bitcoinmagazine.com/feed",
        "https://cryptopotato.com/feed/",
    ])
    # Keyless reddit social-sentiment scrape (public /hot.json; degrades to empty if blocked).
    reddit_subreddits: list[str] = Field(
        default_factory=lambda: ["CryptoCurrency", "CryptoMarkets"])
    fred_key_env: str = "FRED_API_KEY"
    fred_series: list[str] = Field(
        default_factory=lambda: ["DTWEXBGS", "DGS10", "FEDFUNDS", "CPIAUCSL"]
    )
    archive_dir: str = "state/archive"

    @property
    def fred_api_key(self) -> str | None:
        return os.environ.get(self.fred_key_env)


class LoopSettings(BaseModel):
    """Per-loop cadence + model tier for the dual-loop desk (TEMPEST-WEEKLY).

    `timeframe` is the loop's decision candle; `regime_timeframe` (strategic only) is the slower
    anchor the authoritative regime read is taken on (the fast loop READS that read, never
    re-derives it). `poll_minutes` is how often the serialized runner polls the due-gate for it.
    """
    timeframe: str = "1h"
    regime_timeframe: str | None = None
    quick_model: str = "sonnet"
    deep_model: str = "opus"
    poll_minutes: int = 5


def _default_loops() -> dict[str, LoopSettings]:
    return {
        "fast": LoopSettings(timeframe="15m", quick_model="haiku", deep_model="sonnet",
                             poll_minutes=5),
        "strategic": LoopSettings(timeframe="4h", regime_timeframe="4h", quick_model="sonnet",
                                  deep_model="opus", poll_minutes=5),
    }


def _default_agent_models() -> dict[str, str]:
    """Authoritative per-AGENT model assignment, resolved at dispatch time (overrides the loop's
    coarse deep/quick tiers). Rule: every agent that DECIDES money runs on OPUS; only genuinely
    operational agents (that narrate deterministic logic) run cheaper.

    Note: `scalper` is OPUS but its dispatch is GATED by the runner — it is only invoked when the
    CIO granted intraday budget AND a non-empty hot-list, so Opus does not fire on every quiet 15m
    candle (the deterministic exit-sweep still runs every fire, for free)."""
    return {
        # OPUS — agents that decide money (alpha theses, allocation, trade geometry, learning)
        "cio": "opus",
        "trader": "opus",
        "momentum": "opus",
        "carry": "opus",
        "news": "opus",
        "sentiment": "opus",
        "scalper": "opus",          # dispatch GATED (see docstring)
        "reflector": "opus",
        # SONNET — operational (narrates deterministic pacing; the press/anti-martingale logic is
        # computed in futures_fund.pacing, not by the LLM)
        "pace_officer": "sonnet",
    }


class Settings(BaseModel):
    account_size_usdt: float = 10_000.0
    timeframe: str = "4h"               # legacy/strategic regime-anchor default (single-loop paths)
    symbol_count: int = 12
    symbols: list[str] = Field(default_factory=lambda: ["BTC/USDT:USDT", "ETH/USDT:USDT"])
    deep_model: str = "opus"
    quick_model: str = "haiku"
    verdict_horizon_weeks: int = 8
    target_weekly: float = 0.05         # the 5%/WEEK mandate driving the pacing engine
    max_drawdown_tolerance: float = 0.50  # the -50% hard force-flatten breaker level
    loops: dict[str, LoopSettings] = Field(default_factory=_default_loops)
    agent_models: dict[str, str] = Field(default_factory=_default_agent_models)
    live: bool = False  # PAPER-ONLY desk: MUST stay false (live needs a 'graduated' verdict too)
    exchange: ExchangeSettings = Field(default_factory=ExchangeSettings)
    data: DataSettings = Field(default_factory=DataSettings)

    def model_for(self, role: str, *, loop: str | None = None) -> str:
        """Resolve the model an agent role is dispatched with. Per-agent `agent_models` wins; else
        falls back to the loop's `deep_model` (or the global `deep_model`). Used by the orchestrator
        so every DECIDING agent runs on opus and only operational ones run cheaper."""
        if role in self.agent_models:
            return self.agent_models[role]
        if loop and loop in self.loops:
            return self.loops[loop].deep_model
        return self.deep_model


def load_env_file(path: str | Path = ".env") -> dict[str, str]:
    """Load KEY=VALUE pairs from a .env file into os.environ WITHOUT overriding existing env
    vars. Returns the parsed dict; no-op if the file is absent. So that secrets placed in
    .env (gitignored) are actually available to the cycle, which reads keys from os.environ."""
    p = Path(path)
    loaded: dict[str, str] = {}
    if not p.exists():
        return loaded
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if not k:
            continue
        loaded[k] = v
        os.environ.setdefault(k, v)  # real env wins over the file
    return loaded


def load_settings(path: str | Path = "config.yaml") -> Settings:
    """Load non-secret config from YAML (defaults if file absent). Secrets come from env;
    a .env file beside the config is auto-loaded into the environment first."""
    p = Path(path)
    load_env_file(p.parent / ".env")
    raw = yaml.safe_load(p.read_text()) if p.exists() else {}
    return Settings(**(raw or {}))
