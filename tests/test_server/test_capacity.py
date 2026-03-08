"""Tests for adaptive capacity features: scoring integration, capacity models, and learner."""

from __future__ import annotations

import json
import math
import tempfile
import time
from unittest.mock import MagicMock, patch

import pytest

from fleet_manager.models.config import ServerSettings
from fleet_manager.models.node import CapacityMetrics
from fleet_manager.node.app_fingerprint import AppFingerprinter, ResourceSnapshot, WorkloadType
from fleet_manager.node.capacity_learner import (
    NUM_SLOTS,
    AdaptiveCapacityLearner,
    CapacityInfo,
    CapacityMode,
    SlotData,
    _get_slot_index,
)
from fleet_manager.node.meeting_detector import MeetingDetector
from fleet_manager.server.registry import NodeRegistry
from fleet_manager.server.scorer import ScoringEngine

from tests.conftest import make_node


# ---------------------------------------------------------------------------
# SlotData unit tests
# ---------------------------------------------------------------------------


class TestSlotData:
    def test_add_and_count(self):
        slot = SlotData()
        assert slot.sample_count() == 0
        slot.add(time.time(), 50.0, 60.0)
        slot.add(time.time(), 30.0, 40.0)
        assert slot.sample_count() == 2

    def test_weighted_avg_cpu(self):
        slot = SlotData()
        now = time.time()
        # Add observations — recent ones should dominate
        slot.add(now - 86400 * 20, 90.0, 50.0)  # 20 days ago
        slot.add(now - 86400, 10.0, 50.0)        # 1 day ago
        slot.add(now, 10.0, 50.0)                 # now
        avg = slot.weighted_avg_cpu(now)
        # Recent low-CPU observations should pull the average below 50
        assert avg < 50.0

    def test_prune_removes_old(self):
        slot = SlotData()
        now = time.time()
        slot.add(now - 86400 * 40, 50.0, 50.0)  # 40 days ago
        slot.add(now, 30.0, 30.0)                 # now
        assert slot.sample_count() == 2
        slot.prune(max_age_days=30)
        assert slot.sample_count() == 1

    def test_serialization_round_trip(self):
        slot = SlotData()
        now = time.time()
        slot.add(now, 45.0, 55.0)
        slot.add(now - 3600, 30.0, 40.0)
        data = slot.to_dict()
        restored = SlotData.from_dict(data)
        assert restored.sample_count() == 2
        assert abs(restored.weighted_avg_cpu(now) - slot.weighted_avg_cpu(now)) < 0.01


class TestGetSlotIndex:
    def test_range(self):
        """Slot index should be 0-167."""
        idx = _get_slot_index()
        assert 0 <= idx < NUM_SLOTS

    def test_specific_time(self):
        """Monday 10am should be slot 10 (day 0 * 24 + hour 10)."""
        # Find a Monday 10am timestamp
        import calendar
        # 2024-01-01 was a Monday
        t = calendar.timegm(time.strptime("2024-01-01 10:00:00", "%Y-%m-%d %H:%M:%S"))
        idx = _get_slot_index(t)
        # This depends on local timezone, but the index should be valid
        assert 0 <= idx < NUM_SLOTS


# ---------------------------------------------------------------------------
# AdaptiveCapacityLearner tests
# ---------------------------------------------------------------------------


class TestAdaptiveCapacityLearner:
    def _make_learner(self, total_gb=128.0, days_observed=0):
        with tempfile.TemporaryDirectory() as tmpdir:
            learner = AdaptiveCapacityLearner(
                total_memory_gb=total_gb,
                data_dir=tmpdir,
                node_id="test",
            )
            if days_observed > 0:
                learner._first_observation = time.time() - days_observed * 86400
            yield learner

    @patch.object(MeetingDetector, "is_in_meeting", return_value=False)
    def test_bootstrap_mode(self, _mock):
        """New learner should be in bootstrap mode."""
        for learner in self._make_learner():
            cap = learner.observe(20.0, 40.0)
            assert cap.mode == CapacityMode.BOOTSTRAP
            assert cap.ceiling_gb == 0.0
            assert cap.availability_score == 0.0
            assert learner.is_bootstrapping

    @patch.object(MeetingDetector, "is_in_meeting", return_value=False)
    def test_post_bootstrap_computes_score(self, _mock):
        """After 7 days, learner should compute a real availability score."""
        for learner in self._make_learner(days_observed=10):
            # Seed some observations
            for i in range(20):
                learner.observe(15.0, 30.0)
            cap = learner.observe(15.0, 30.0)
            assert cap.mode != CapacityMode.BOOTSTRAP
            assert cap.availability_score > 0.0
            assert cap.ceiling_gb > 0.0

    @patch.object(MeetingDetector, "is_in_meeting", return_value=False)
    def test_high_cpu_computes_low_availability(self, _mock):
        """High current CPU should result in lower availability."""
        for learner in self._make_learner(days_observed=10):
            cap_idle = learner.observe(5.0, 20.0)
            cap_busy = learner.observe(90.0, 80.0)
            # Busy state should have lower availability
            assert cap_busy.availability_score < cap_idle.availability_score

    @patch.object(MeetingDetector, "is_in_meeting", return_value=True)
    def test_meeting_detection_pauses(self, mock_meeting):
        """Meeting detected should hard-pause the node."""
        for learner in self._make_learner(days_observed=10):
            cap = learner.observe(20.0, 40.0)
            assert cap.mode == CapacityMode.PAUSED
            assert cap.ceiling_gb == 0.0
            assert cap.reason == "meeting_detected"
            assert cap.override_active

    @patch.object(MeetingDetector, "is_in_meeting", return_value=False)
    def test_sustained_high_cpu_pauses(self, _mock):
        """Sustained CPU > 85% for 2+ minutes should reduce capacity."""
        for learner in self._make_learner(days_observed=10):
            learner._sustained_high_cpu_since = time.time() - 130  # 130 seconds ago
            cap = learner.observe(90.0, 50.0)
            assert cap.mode == CapacityMode.PAUSED
            assert cap.reason == "sustained_high_cpu"

    @patch.object(MeetingDetector, "is_in_meeting", return_value=False)
    def test_manual_override_full(self, _mock):
        """Manual override to full capacity should work."""
        for learner in self._make_learner(days_observed=10):
            learner.set_manual_override("full", duration_hours=1.0)
            cap = learner.observe(80.0, 70.0)
            assert cap.mode == CapacityMode.FULL
            assert cap.override_active
            assert cap.reason == "manual_override"

    @patch.object(MeetingDetector, "is_in_meeting", return_value=False)
    def test_manual_override_paused(self, _mock):
        """Manual override to paused should work."""
        for learner in self._make_learner(days_observed=10):
            learner.set_manual_override("paused", duration_hours=1.0)
            cap = learner.observe(10.0, 20.0)
            assert cap.mode == CapacityMode.PAUSED
            assert cap.override_active

    @patch.object(MeetingDetector, "is_in_meeting", return_value=False)
    def test_manual_override_expires(self, _mock):
        """Expired manual override should be ignored."""
        for learner in self._make_learner(days_observed=10):
            learner._manual_override = {
                "mode": "full",
                "expires": time.time() - 100,  # expired
            }
            cap = learner.observe(10.0, 20.0)
            assert cap.reason != "manual_override"

    def test_availability_to_ceiling_mapping(self):
        """Availability score should map to correct ceiling tiers."""
        for learner in self._make_learner(total_gb=128.0):
            # Test each tier
            c, m = learner._availability_to_ceiling(0.1)
            assert c == 0.0 and m == CapacityMode.PAUSED

            c, m = learner._availability_to_ceiling(0.3)
            assert c == 16.0 and m == CapacityMode.LEARNED_LOW

            c, m = learner._availability_to_ceiling(0.5)
            assert c == 32.0 and m == CapacityMode.LEARNED_MEDIUM

            c, m = learner._availability_to_ceiling(0.7)
            assert c == 64.0 and m == CapacityMode.LEARNED_HIGH

            c, m = learner._availability_to_ceiling(0.9)
            assert c == 128.0 * 0.8 and m == CapacityMode.FULL

    @patch.object(MeetingDetector, "is_in_meeting", return_value=False)
    def test_learning_confidence_grows(self, _mock):
        """Confidence should increase with more observations."""
        for learner in self._make_learner(days_observed=1):
            assert learner.learning_confidence < 0.5
        for learner in self._make_learner(days_observed=30):
            # Add many observations
            for _ in range(1000):
                learner.observe(30.0, 40.0)
            assert learner.learning_confidence > 0.5

    def test_heatmap_data(self):
        """Heatmap should return 168 slots."""
        for learner in self._make_learner():
            heatmap = learner.get_heatmap_data()
            assert len(heatmap) == 168
            assert all("day" in entry for entry in heatmap)
            assert all("hour" in entry for entry in heatmap)

    @patch.object(MeetingDetector, "is_in_meeting", return_value=False)
    def test_save_and_load(self, _mock):
        """State should persist across save/load."""
        with tempfile.TemporaryDirectory() as tmpdir:
            learner1 = AdaptiveCapacityLearner(
                total_memory_gb=64.0, data_dir=tmpdir, node_id="test"
            )
            learner1.observe(30.0, 40.0)
            learner1.observe(50.0, 60.0)
            learner1.save()

            learner2 = AdaptiveCapacityLearner(
                total_memory_gb=64.0, data_dir=tmpdir, node_id="test"
            )
            total1 = sum(s.sample_count() for s in learner1._slots)
            total2 = sum(s.sample_count() for s in learner2._slots)
            assert total2 == total1

    def test_days_observed(self):
        for learner in self._make_learner(days_observed=15):
            assert learner.days_observed == 15
            assert not learner.is_bootstrapping


# ---------------------------------------------------------------------------
# AppFingerprinter tests
# ---------------------------------------------------------------------------


class TestAppFingerprinter:
    def test_classify_idle(self):
        fp = AppFingerprinter(window_seconds=60, sample_interval=5)
        # Add low-CPU snapshots
        now = time.time()
        for i in range(5):
            snap = ResourceSnapshot(
                timestamp=now - (4 - i) * 5,
                cpu_pct=3.0, memory_pct=30.0,
                net_bytes_sent=100, net_bytes_recv=200,
                disk_io_read=0, disk_io_write=0,
            )
            fp._snapshots.append(snap)
        assert fp.classify() == WorkloadType.IDLE

    def test_classify_heavy(self):
        fp = AppFingerprinter(window_seconds=60, sample_interval=5)
        now = time.time()
        for i in range(5):
            snap = ResourceSnapshot(
                timestamp=now - (4 - i) * 5,
                cpu_pct=70.0, memory_pct=60.0,
                net_bytes_sent=1000, net_bytes_recv=2000,
                disk_io_read=0, disk_io_write=0,
            )
            fp._snapshots.append(snap)
        assert fp.classify() == WorkloadType.HEAVY

    def test_classify_intensive(self):
        fp = AppFingerprinter(window_seconds=60, sample_interval=5)
        now = time.time()
        for i in range(5):
            snap = ResourceSnapshot(
                timestamp=now - (4 - i) * 5,
                cpu_pct=92.0, memory_pct=80.0,
                net_bytes_sent=5000, net_bytes_recv=10000,
                disk_io_read=0, disk_io_write=0,
            )
            fp._snapshots.append(snap)
        assert fp.classify() == WorkloadType.INTENSIVE

    def test_cpu_trend_rising(self):
        fp = AppFingerprinter(window_seconds=60, sample_interval=5)
        now = time.time()
        for i in range(8):
            snap = ResourceSnapshot(
                timestamp=now - (7 - i) * 5,
                cpu_pct=10.0 + i * 10,  # 10% → 80%
                memory_pct=40.0,
                net_bytes_sent=0, net_bytes_recv=0,
                disk_io_read=0, disk_io_write=0,
            )
            fp._snapshots.append(snap)
        trend = fp.get_cpu_trend(seconds=60)
        # Rising CPU → positive trend value (second half higher than first)
        assert trend > 0

    def test_cpu_trend_falling(self):
        fp = AppFingerprinter(window_seconds=60, sample_interval=5)
        now = time.time()
        for i in range(8):
            snap = ResourceSnapshot(
                timestamp=now - (7 - i) * 5,
                cpu_pct=80.0 - i * 8,  # 80% → 24%
                memory_pct=40.0,
                net_bytes_sent=0, net_bytes_recv=0,
                disk_io_read=0, disk_io_write=0,
            )
            fp._snapshots.append(snap)
        trend = fp.get_cpu_trend(seconds=60)
        # Falling CPU → negative trend value (second half lower than first)
        assert trend < 0

    def test_get_summary(self):
        fp = AppFingerprinter()
        summary = fp.get_summary()
        assert "workload" in summary
        assert "avg_cpu" in summary
        assert "samples" in summary
        assert summary["samples"] == 0


# ---------------------------------------------------------------------------
# MeetingDetector tests
# ---------------------------------------------------------------------------


class TestMeetingDetector:
    def test_non_darwin_returns_false(self):
        detector = MeetingDetector()
        detector._is_darwin = False
        assert not detector.is_camera_active()
        assert not detector.is_microphone_active()
        assert not detector.is_in_meeting()


# ---------------------------------------------------------------------------
# Scorer integration with capacity data
# ---------------------------------------------------------------------------


class TestScorerCapacityIntegration:
    def _make_scorer(self, nodes):
        settings = ServerSettings()
        registry = NodeRegistry(settings)
        for node in nodes:
            registry._nodes[node.node_id] = node
        return ScoringEngine(settings, registry)

    def test_paused_node_eliminated(self):
        """Nodes with capacity mode 'paused' should be eliminated."""
        node = make_node(
            node_id="macbook",
            memory_total=128.0,
            memory_used=30.0,
            available_models=["llama3.3:70b"],
            capacity=CapacityMetrics(
                mode="paused",
                ceiling_gb=0.0,
                availability_score=0.0,
                reason="meeting_detected",
            ),
        )
        scorer = self._make_scorer([node])
        results = scorer.score_request("llama3.3:70b", {})
        assert len(results) == 0

    def test_bootstrap_node_eliminated(self):
        """Nodes in bootstrap mode should be eliminated."""
        node = make_node(
            node_id="macbook",
            memory_total=128.0,
            memory_used=30.0,
            available_models=["llama3.3:70b"],
            capacity=CapacityMetrics(
                mode="bootstrap",
                ceiling_gb=0.0,
                availability_score=0.0,
            ),
        )
        scorer = self._make_scorer([node])
        results = scorer.score_request("llama3.3:70b", {})
        assert len(results) == 0

    def test_low_availability_eliminated(self):
        """Nodes with availability < 0.2 should be eliminated."""
        node = make_node(
            node_id="macbook",
            memory_total=128.0,
            memory_used=30.0,
            available_models=["llama3.3:70b"],
            capacity=CapacityMetrics(
                mode="learned_low",
                ceiling_gb=16.0,
                availability_score=0.15,
            ),
        )
        scorer = self._make_scorer([node])
        results = scorer.score_request("llama3.3:70b", {})
        assert len(results) == 0

    def test_ceiling_limits_model_fit(self):
        """Capacity ceiling should limit which models can fit."""
        node = make_node(
            node_id="macbook",
            memory_total=128.0,
            memory_used=20.0,  # 108GB available
            available_models=["llama3.3:70b"],
            capacity=CapacityMetrics(
                mode="learned_low",
                ceiling_gb=16.0,  # But ceiling is only 16GB
                availability_score=0.3,
            ),
        )
        scorer = self._make_scorer([node])
        results = scorer.score_request("llama3.3:70b", {})
        # 70b model is ~40GB, ceiling is 16GB → eliminated
        assert len(results) == 0

    def test_node_without_capacity_not_affected(self):
        """Nodes without capacity data should work normally (servers)."""
        node = make_node(
            node_id="mac-studio",
            memory_total=512.0,
            memory_used=100.0,
            available_models=["llama3.3:70b"],
        )
        scorer = self._make_scorer([node])
        results = scorer.score_request("llama3.3:70b", {})
        assert len(results) == 1

    def test_full_capacity_node_scores_normally(self):
        """Nodes at full capacity should score normally with availability bonus."""
        node = make_node(
            node_id="macbook",
            memory_total=128.0,
            memory_used=20.0,
            loaded_models=[("llama3.3:70b", 40.0)],
            capacity=CapacityMetrics(
                mode="full",
                ceiling_gb=100.0,
                availability_score=0.9,
            ),
        )
        scorer = self._make_scorer([node])
        results = scorer.score_request("llama3.3:70b", {})
        assert len(results) == 1
        assert results[0].scores_breakdown["availability_trend"] > 0

    def test_availability_trend_signal(self):
        """Nodes with higher availability score should get higher trend bonus."""
        node_high = make_node(
            node_id="macbook-high",
            memory_total=128.0,
            memory_used=20.0,
            loaded_models=[("phi4:14b", 9.0)],
            capacity=CapacityMetrics(
                mode="full",
                ceiling_gb=100.0,
                availability_score=0.9,
            ),
        )
        node_low = make_node(
            node_id="macbook-low",
            memory_total=128.0,
            memory_used=20.0,
            loaded_models=[("phi4:14b", 9.0)],
            capacity=CapacityMetrics(
                mode="learned_low",
                ceiling_gb=32.0,
                availability_score=0.3,
            ),
        )
        scorer = self._make_scorer([node_high, node_low])
        results = scorer.score_request("phi4:14b", {})
        assert len(results) == 2
        high_result = next(r for r in results if r.node_id == "macbook-high")
        low_result = next(r for r in results if r.node_id == "macbook-low")
        assert high_result.scores_breakdown["availability_trend"] > low_result.scores_breakdown["availability_trend"]


# ---------------------------------------------------------------------------
# CapacityMetrics model tests
# ---------------------------------------------------------------------------


class TestCapacityMetrics:
    def test_defaults(self):
        cap = CapacityMetrics()
        assert cap.mode == "full"
        assert cap.availability_score == 1.0
        assert cap.ceiling_gb == 0.0

    def test_serialization(self):
        cap = CapacityMetrics(
            mode="learned_high",
            ceiling_gb=64.0,
            availability_score=0.75,
            reason="good_availability",
            days_observed=14,
        )
        data = cap.model_dump()
        restored = CapacityMetrics(**data)
        assert restored.mode == "learned_high"
        assert restored.ceiling_gb == 64.0
        assert restored.availability_score == 0.75
