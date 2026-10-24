"""``common.stats.percentile`` -- one definition for the community telemetry
payload and the benchmark report.  Moved from ``test_daily_rollup`` when the
account pipeline was removed; ``test_community_payload_golden`` pins the
arithmetic end to end.
"""

from __future__ import annotations

from fleet_manager.common.stats import percentile


def test_single_sample_is_itself():
    assert percentile([500.0], 95) == 500.0


def test_empty_is_none_not_zero():
    """No data and zero latency are different facts; the payload sends null."""
    assert percentile([], 95) is None


def test_p95_lands_near_the_top():
    vals = [float(i * 10) for i in range(1, 11)]  # 10, 20, ..., 100
    assert 90 <= percentile(vals, 95) <= 100


def test_interpolates_between_samples():
    assert percentile([10.0, 20.0], 50) == 15.0


def test_input_order_does_not_matter():
    assert percentile([3.0, 1.0, 2.0], 50) == percentile([1.0, 2.0, 3.0], 50) == 2.0


def test_accepts_fractional_percentile():
    """The benchmark report asks for p99 and the payload for p95; nothing
    restricts callers to integers."""
    assert percentile([0.0, 100.0], 99.5) == 99.5
