"""simple_regime must be robust to SHORT history (a freshly-listed symbol with <6 4h bars).

Regression: a brand-new low-cap listing entered the scout universe with fewer than 6 candles;
`simple_regime` indexed `ema.iloc[-6]` and raised IndexError, which aborted the ENTIRE preflight
(no context.json -> the whole 4h cycle bricked). The slope must be measured over the AVAILABLE
lookback instead of a fixed -6.
"""
import pandas as pd

from futures_fund.baseline import simple_regime

_QUADRANTS = {"high_vol_trend", "low_vol_trend", "high_vol_range", "low_vol_range"}
_DIRS = {"up", "down", "neutral"}


def _df(closes):
    n = len(closes)
    return pd.DataFrame({"open": closes, "high": closes, "low": closes,
                         "close": closes, "volume": [1.0] * n})


def test_simple_regime_handles_short_history():
    # 1..5 bars (all < 6) must classify a regime, never crash
    for k in (1, 2, 3, 4, 5):
        r = simple_regime(_df([100.0 + i for i in range(k)]))
        assert r.quadrant in _QUADRANTS
        assert r.trend_direction in _DIRS


def test_simple_regime_normal_history_still_classifies_trend():
    # a clean rising 12-bar series is still an uptrend (fix didn't change normal behavior)
    r = simple_regime(_df([100.0 * (1.02 ** i) for i in range(12)]))
    assert r.trend_direction == "up"
    assert r.quadrant in _QUADRANTS
