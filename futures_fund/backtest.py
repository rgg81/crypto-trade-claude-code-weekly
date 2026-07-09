"""Backtest engine for the blended all-weather strategy.

Simulates the dollar-neutral blended book over historical data to measure
performance, turnover, and parameter sensitivity. Used for monthly validation
and optimization of the blended score formula.

Key features:
- Fetches historical 4h OHLCV + funding rates
- Runs blended score ranking at each 4h step
- Simulates LONG top-N / SHORT bottom-N equal-$ book
- Measures: net %/month, turnover, variance, funding, max drawdown
- Tests different parameter combinations (weights, thresholds)

Usage:
    from futures_fund.backtest import BacktestEngine, BacktestConfig

    config = BacktestConfig(
        start_date="2026-05-01",
        end_date="2026-07-01",
        init_equity=10000.0,
        n_per_side=3,
        trend_weights={"mom": 0.55, "carry": 0.35, "mr": 0.10},
        range_weights={"mom": 0.40, "carry": 0.40, "mr": 0.20},
    )
    engine = BacktestEngine(config)
    results = engine.run()
    print(results.summary())
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

try:
    from ccxt import Exchange

    from . import exchange
except ImportError:
    # For standalone usage outside the package
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from futures_fund import exchange


@dataclass
class BacktestConfig:
    """Configuration for a blended strategy backtest."""
    start_date: str  # ISO date string, e.g., "2026-05-01"
    end_date: str    # ISO date string, e.g., "2026-07-01"
    init_equity: float = 10000.0
    n_per_side: int = 3

    # Blended score weights
    trend_weights: dict[str, float] = field(default_factory=lambda: {
        "mom": 0.55, "carry": 0.35, "mr": 0.10
    })
    range_weights: dict[str, float] = field(default_factory=lambda: {
        "mom": 0.40, "carry": 0.40, "mr": 0.20
    })

    # Thresholds
    dispersion_threshold: float = 0.05
    strong_mom_threshold: float = 0.06
    min_oi_usd: float = 75e6
    swap_margin: float = 0.5
    keep_buffer: int = 2

    # Cost model (per-fill basis points)
    taker_fee_bps: float = 5.0    # 0.05%
    slippage_bps: float = 2.0     # 0.02%


@dataclass
class BacktestResult:
    """Results from a backtest run."""
    config: BacktestConfig

    # Performance metrics
    total_return_pct: float
    monthly_return_pct: float
    sharpe_ratio: float
    max_drawdown_pct: float
    volatility_annual: float

    # Trading metrics
    total_cycles: int
    rotations_per_cycle: float
    turnover_monthly_pct: float
    avg_gross_exposure: float  # As fraction of equity

    # Funding metrics
    funding_collected_pct: float
    funding_flip_rate: float  # How often funding flips sign

    # Regime distribution
    trend_cycles_pct: float
    range_cycles_pct: float

    # Per-cycle equity curve
    equity_curve: list[float] = field(default_factory=list)

    def summary(self) -> str:
        """One-line summary of results."""
        return (
            f"Return: {self.monthly_return_pct:+.2f}%/mo | "
            f"Sharpe: {self.sharpe_ratio:.2f} | "
            f"MaxDD: {self.max_drawdown_pct:.1f}% | "
            f"Turnover: {self.turnover_monthly_pct:.1f}%/mo | "
            f"Funding: {self.funding_collected_pct:+.2f}% | "
            f"Rot/cyc: {self.rotations_per_cycle:.1f}"
        )


class BacktestEngine:
    """Backtest engine for the blended all-weather strategy."""

    def __init__(self, config: BacktestConfig, exchange_instance: Exchange | None = None):
        self.config = config
        self.exch = exchange_instance or exchange.get_exchange()
        self.state_dir = Path("state")

    def run(self) -> BacktestResult:
        """Run the backtest and return results."""
        print(f"Backtesting {self.config.start_date} to {self.config.end_date}")

        # Fetch historical data
        ohlcv_data = self._fetch_historical_ohlcv()
        funding_data = self._fetch_historical_funding()

        # Run simulation
        equity_curve, metrics = self._simulate(ohlcv_data, funding_data)

        return BacktestResult(
            config=self.config,
            equity_curve=equity_curve,
            **metrics
        )

    def _fetch_historical_ohlcv(self) -> dict[str, list[dict]]:
        """Fetch historical 4h OHLCV data for all symbols in the universe."""
        # This is expensive - cache it
        cache_file = (
            self.state_dir / "backtest" /
            f"ohlcv_{self.config.start_date}_{self.config.end_date}.json"
        )
        if cache_file.exists():
            print(f"Loading cached OHLCV from {cache_file}")
            return json.loads(cache_file.read_text())

        print("Fetching historical OHLCV (this may take a while)...")
        # TODO: Implement actual historical fetch from exchange.fetch_ohlcv
        # For now, return empty - this needs real implementation
        raise NotImplementedError("Historical OHLCV fetch not yet implemented")

    def _fetch_historical_funding(self) -> dict[str, list[dict]]:
        """Fetch historical funding rates for all symbols."""
        cache_file = (
            self.state_dir / "backtest" /
            f"funding_{self.config.start_date}_{self.config.end_date}.json"
        )
        if cache_file.exists():
            print(f"Loading cached funding from {cache_file}")
            return json.loads(cache_file.read_text())

        print("Fetching historical funding rates...")
        # TODO: Implement actual historical fetch
        raise NotImplementedError("Historical funding fetch not yet implemented")

    def _simulate(self, ohlcv_data: dict[str, list[dict]],
                  funding_data: dict[str, list[dict]]) -> tuple[list[float], dict]:
        """Run the blended strategy simulation.

        Args:
            ohlcv_data: Dict symbol -> list of OHLCV candles
                (timestamp, open, high, low, close, vol)
            funding_data: Dict symbol -> list of funding records
                (timestamp, rate)

        Returns:
            (equity_curve, metrics_dict)
        """
        # This is the core simulation logic
        # For each 4h candle:
        #   1. Compute blended scores for all symbols
        #   2. Select top-N long, bottom-N short
        #   3. Simulate equal-$ allocation
        #   4. Track PnL from price moves + funding
        #   5. Apply trading costs on rotations

        # TODO: Implement full simulation
        # For now, return mock results
        raise NotImplementedError("Full simulation not yet implemented")


def quick_validate(config: BacktestConfig, days: int = 30) -> BacktestResult:
    """Quick validation on recent data (lighter version for faster runs).

    Uses only the last N days and a simplified simulation (no slippage stress test).
    """
    end = datetime.now()
    start = end - timedelta(days=days)
    config.start_date = start.strftime("%Y-%m-%d")
    config.end_date = end.strftime("%Y-%m-%d")

    engine = BacktestEngine(config)
    return engine.run()


if __name__ == "__main__":
    # Example usage
    config = BacktestConfig(
        start_date="2026-06-01",
        end_date="2026-07-01",
        init_equity=10000.0,
    )
    engine = BacktestEngine(config)
    results = engine.run()
    print(results.summary())
