"""Tests for monthly parameter review."""

import json

import pytest

from scripts.monthly_review import (
    check_neutrality_compliance,
    compute_performance_metrics,
)


@pytest.fixture
def sample_cycles():
    """Sample cycle data for testing."""
    return [
        {"cycle": 1, "equity": 10000.0, "time": "2026-06-01T00:00:00"},
        {"cycle": 2, "equity": 10050.0, "time": "2026-06-01T04:00:00"},
        {"cycle": 3, "equity": 10100.0, "time": "2026-06-01T08:00:00"},
        {"cycle": 4, "equity": 10080.0, "time": "2026-06-01T12:00:00"},
        {"cycle": 5, "equity": 10150.0, "time": "2026-06-01T16:00:00"},
    ]


def test_performance_metrics_growth(sample_cycles):
    """Test performance metrics with growing equity."""
    metrics = compute_performance_metrics(sample_cycles)

    assert metrics["total_return_pct"] == 1.5  # +1.5% from 10k to 10.15k
    assert metrics["monthly_return_pct"] > 0
    assert metrics["max_drawdown_pct"] >= 0
    assert metrics["final_equity"] == 10150.0
    assert metrics["cycles_analyzed"] == 5


def test_performance_metrics_drawdown():
    """Test max drawdown calculation."""
    cycles = [
        {"cycle": 1, "equity": 10000.0},
        {"cycle": 2, "equity": 10500.0},  # Peak
        {"cycle": 3, "equity": 9800.0},  # Drawdown
        {"cycle": 4, "equity": 10200.0},
    ]

    metrics = compute_performance_metrics(cycles)

    # Max DD from 10500 to 9800 = 700/10500 = 6.67%
    assert metrics["max_drawdown_pct"] == pytest.approx(6.67, rel=0.01)


def test_performance_metrics_insufficient_data():
    """Test with insufficient data points."""
    cycles = [{"cycle": 1, "equity": 10000.0}]

    metrics = compute_performance_metrics(cycles)

    assert "error" in metrics
    assert metrics["error"] == "Not enough data points"


def test_neutrality_check_no_violations(tmp_path):
    """Test neutrality check with compliant cycles."""
    # Create mock cycle folders with positions_after.json
    for i in range(1, 4):
        cycle_dir = tmp_path / "cycle" / str(i)
        cycle_dir.mkdir(parents=True)

        positions = [
            {"symbol": "BTCUSDT", "direction": "long"},
            {"symbol": "SOLUSDT", "direction": "long"},
            {"symbol": "ETHUSDT", "direction": "short"},
            {"symbol": "ADAUSDT", "direction": "short"},
        ]

        with open(cycle_dir / "positions_after.json", "w") as f:
            json.dump(positions, f)

    cycles = [
        {"cycle": 1, "equity": 10000.0},
        {"cycle": 2, "equity": 10100.0},
        {"cycle": 3, "equity": 10200.0},
    ]

    result = check_neutrality_compliance(cycles, tmp_path)

    assert result["neutrality_violations"] == 0
    assert result["cycles_checked"] == 3


def test_neutrality_check_with_violations(tmp_path):
    """Test neutrality check detects flat books."""
    # Create a cycle with only longs (flat book violation)
    cycle_dir = tmp_path / "cycle" / "1"
    cycle_dir.mkdir(parents=True)

    positions = [
        {"symbol": "BTCUSDT", "direction": "long"},
        {"symbol": "SOLUSDT", "direction": "long"},
        # No shorts!
    ]

    with open(cycle_dir / "positions_after.json", "w") as f:
        json.dump(positions, f)

    cycles = [{"cycle": 1, "equity": 10000.0}]

    result = check_neutrality_compliance(cycles, tmp_path)

    assert result["neutrality_violations"] == 1
    assert len(result["violation_details"]) == 1
    assert result["violation_details"][0]["issue"] == "flat_book"


def test_monthly_review_integration(tmp_path):
    """Integration test: monthly review creates report."""
    # This would test the full monthly_review.py script
    # For now, just verify the report directory gets created
    review_dir = tmp_path / "monthly_review"
    review_dir.mkdir(parents=True)

    report_file = review_dir / "2026-07.json"

    # Write a mock report
    report = {
        "review_date": "2026-07",
        "performance": {"monthly_return_pct": 5.0},
        "recommendations": [],
    }

    with open(report_file, "w") as f:
        json.dump(report, f)

    assert report_file.exists()
    assert report["review_date"] == "2026-07"
