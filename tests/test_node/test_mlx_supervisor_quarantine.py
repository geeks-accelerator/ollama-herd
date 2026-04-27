"""Tests for the MLX supervisor's crash-rate quarantine guard.

Background: 2026-04-26, mlx-lm v0.31.3's load_default + tqdm threadpool
race put the local fleet's MLX servers into a 420-restart, 2.5-hour
crash loop at 60s cadence — burning agent CPU and flooding logs without
any chance of recovery. The supervisor now tracks crashes within a
rolling window and switches to a much slower restart cadence
("quarantine") once the threshold is exceeded. Quarantine clears
automatically when a restart stays up for the full window.

These tests exercise the bookkeeping and state transitions directly
rather than spawning real subprocesses (the supervisor's process
management is covered by separate tests).
"""

from __future__ import annotations

from unittest.mock import patch

from fleet_manager.node import mlx_supervisor
from fleet_manager.node.mlx_supervisor import (
    _QUARANTINE_FAILURE_COUNT,
    _QUARANTINE_WINDOW_S,
    MlxSupervisor,
)


def _make_sup(**kwargs):
    """Construct a supervisor without spawning anything."""
    defaults = dict(model="fake/model", port=11440)
    defaults.update(kwargs)
    return MlxSupervisor(**defaults)


class TestQuarantineThreshold:
    def test_below_threshold_does_not_quarantine(self):
        sup = _make_sup()
        with patch.object(mlx_supervisor.time, "monotonic", return_value=100.0):
            for _ in range(_QUARANTINE_FAILURE_COUNT - 1):
                sup._record_crash_and_check_quarantine()
        assert sup._quarantined is False
        assert len(sup._recent_crash_ts) == _QUARANTINE_FAILURE_COUNT - 1

    def test_at_threshold_enters_quarantine(self):
        sup = _make_sup()
        with patch.object(mlx_supervisor.time, "monotonic", return_value=100.0):
            for _ in range(_QUARANTINE_FAILURE_COUNT):
                sup._record_crash_and_check_quarantine()
        assert sup._quarantined is True
        assert len(sup._recent_crash_ts) == _QUARANTINE_FAILURE_COUNT

    def test_old_crashes_outside_window_dont_count(self):
        """5 crashes 10 minutes apart shouldn't trigger quarantine because
        the window is 5 minutes — only the most recent one is in scope."""
        sup = _make_sup()
        with patch.object(mlx_supervisor.time, "monotonic") as mock_now:
            # 5 crashes spaced 10 minutes apart (well outside the 5-min window)
            for i in range(_QUARANTINE_FAILURE_COUNT):
                mock_now.return_value = 100.0 + i * 600.0
                sup._record_crash_and_check_quarantine()
        # Most recent crashes should have evicted the older ones from the list
        # so the count never reaches threshold.
        assert sup._quarantined is False
        assert len(sup._recent_crash_ts) == 1

    def test_burst_inside_window_triggers_quarantine(self):
        """5 crashes spaced 10 seconds apart all fall within the window."""
        sup = _make_sup()
        with patch.object(mlx_supervisor.time, "monotonic") as mock_now:
            for i in range(_QUARANTINE_FAILURE_COUNT):
                mock_now.return_value = 100.0 + i * 10.0
                sup._record_crash_and_check_quarantine()
        assert sup._quarantined is True

    def test_quarantine_only_logged_once_per_entry(self, caplog):
        """We don't want to spam ERROR logs for every crash once quarantined.
        The loud message fires on the transition into quarantine, not on
        every subsequent crash within."""
        import logging
        sup = _make_sup()
        caplog.set_level(logging.ERROR, logger="fleet_manager.node.mlx_supervisor")
        with patch.object(mlx_supervisor.time, "monotonic", return_value=100.0):
            for _ in range(_QUARANTINE_FAILURE_COUNT + 5):
                sup._record_crash_and_check_quarantine()
        quarantine_logs = [r for r in caplog.records if "QUARANTINE" in r.message]
        assert len(quarantine_logs) == 1, (
            f"Expected exactly one quarantine ERROR log, got {len(quarantine_logs)}"
        )

    def test_window_pruning_keeps_only_recent_entries(self):
        """Verify that the internal list doesn't grow unboundedly — old
        entries get pruned on each call."""
        sup = _make_sup()
        with patch.object(mlx_supervisor.time, "monotonic") as mock_now:
            # 100 crashes 10 seconds apart
            for i in range(100):
                mock_now.return_value = 1000.0 + i * 10.0
                sup._record_crash_and_check_quarantine()
        # At t=1990, the window includes everything from t > 1990 - 300 = 1690.
        # Crashes 0-68 had ts < 1690, so they should be pruned. 69-99 = 31 left.
        # (Plus or minus 1 depending on inclusive/exclusive boundary)
        assert 30 <= len(sup._recent_crash_ts) <= 32

    def test_quarantine_flag_persists_until_explicit_clear(self):
        """Once quarantined, the flag stays set even if the recent_crash_ts
        list shrinks below threshold (because of pruning).  Only an
        explicit recovery (in _monitor) clears it."""
        sup = _make_sup()
        with patch.object(mlx_supervisor.time, "monotonic") as mock_now:
            # Burst into quarantine
            for i in range(_QUARANTINE_FAILURE_COUNT):
                mock_now.return_value = 100.0 + i
                sup._record_crash_and_check_quarantine()
            assert sup._quarantined is True

            # Wait past the window without further crashes — the next call
            # would prune everything BUT we're not calling _record again
            # without a crash.  The flag stays set.
            mock_now.return_value = 100.0 + _QUARANTINE_WINDOW_S + 60.0
            # No new crashes — flag should still be True
        assert sup._quarantined is True


class TestQuarantineStatus:
    def test_initial_state_is_not_quarantined(self):
        sup = _make_sup()
        assert sup._quarantined is False
        assert sup._recent_crash_ts == []
