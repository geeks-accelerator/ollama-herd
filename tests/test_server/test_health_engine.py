"""Tests for the HealthEngine — fleet health analysis and recommendations."""

from __future__ import annotations

import time

import pytest
import pytest_asyncio

from fleet_manager.models.node import MemoryPressure, NodeStatus
from fleet_manager.server.health_engine import HealthEngine, Severity
from fleet_manager.server.trace_store import TraceStore
from tests.conftest import make_node


class FakeRegistry:
    """Minimal registry wrapper for testing."""

    def __init__(self, nodes):
        self._nodes = nodes

    def get_all_nodes(self):
        return self._nodes


class TestRegistryChecks:
    """Tests for checks that only use in-memory registry state."""

    @pytest.mark.asyncio
    async def test_healthy_fleet_no_recommendations(self):
        from fleet_manager import __version__

        engine = HealthEngine()
        node = make_node("studio", memory_total=128.0, memory_used=100.0, agent_version=__version__)
        report = await engine.analyze(FakeRegistry([node]), None)
        assert report.vitals.health_score == 100
        assert len(report.recommendations) == 0

    @pytest.mark.asyncio
    async def test_offline_node_critical(self):
        engine = HealthEngine()
        node = make_node("dead-node", status=NodeStatus.OFFLINE)
        node.last_heartbeat = time.time() - 300
        report = await engine.analyze(FakeRegistry([node]), None)
        assert report.vitals.nodes_offline == 1
        crit = [r for r in report.recommendations if r.severity == Severity.CRITICAL]
        assert len(crit) >= 1
        assert "dead-node" in crit[0].title

    @pytest.mark.asyncio
    async def test_degraded_node_warning(self):
        engine = HealthEngine()
        node = make_node("flaky-node", status=NodeStatus.DEGRADED)
        node.last_heartbeat = time.time() - 20
        report = await engine.analyze(FakeRegistry([node]), None)
        assert report.vitals.nodes_degraded == 1
        warns = [r for r in report.recommendations if r.check_id == "node_degraded"]
        assert len(warns) == 1

    @pytest.mark.asyncio
    async def test_memory_pressure_warning(self):
        engine = HealthEngine()
        node = make_node(
            "stressed",
            pressure=MemoryPressure.WARN,
            memory_total=64.0,
            memory_used=58.0,
        )
        report = await engine.analyze(FakeRegistry([node]), None)
        warns = [r for r in report.recommendations if r.check_id == "memory_pressure"]
        assert len(warns) == 1
        assert warns[0].severity == Severity.WARNING

    @pytest.mark.asyncio
    async def test_memory_pressure_critical(self):
        engine = HealthEngine()
        node = make_node(
            "dying",
            pressure=MemoryPressure.CRITICAL,
            memory_total=64.0,
            memory_used=62.0,
        )
        report = await engine.analyze(FakeRegistry([node]), None)
        crits = [r for r in report.recommendations if r.check_id == "memory_pressure"]
        assert len(crits) == 1
        assert crits[0].severity == Severity.CRITICAL

    @pytest.mark.asyncio
    async def test_underutilized_memory(self):
        engine = HealthEngine()
        node = make_node(
            "idle-box",
            memory_total=128.0,
            memory_used=30.0,
            loaded_models=[("phi4:14b", 9.0)],
        )
        report = await engine.analyze(FakeRegistry([node]), None)
        infos = [r for r in report.recommendations if r.check_id == "underutilized_memory"]
        assert len(infos) == 1
        assert infos[0].severity == Severity.INFO

    @pytest.mark.asyncio
    async def test_no_underutilized_if_memory_full(self):
        engine = HealthEngine()
        node = make_node(
            "full-box",
            memory_total=64.0,
            memory_used=50.0,
            loaded_models=[("llama3:70b", 40.0)],
        )
        report = await engine.analyze(FakeRegistry([node]), None)
        infos = [r for r in report.recommendations if r.check_id == "underutilized_memory"]
        assert len(infos) == 0

    @pytest.mark.asyncio
    async def test_empty_fleet(self):
        engine = HealthEngine()
        report = await engine.analyze(FakeRegistry([]), None)
        assert report.vitals.nodes_total == 0
        assert report.vitals.health_score == 100
        assert len(report.recommendations) == 0

    @pytest.mark.asyncio
    async def test_health_score_degrades_with_issues(self):
        engine = HealthEngine()
        node = make_node("bad", status=NodeStatus.OFFLINE, pressure=MemoryPressure.CRITICAL)
        node.last_heartbeat = time.time() - 600
        report = await engine.analyze(FakeRegistry([node]), None)
        # Offline (critical -20) + memory pressure critical (-20) = 60
        assert report.vitals.health_score <= 60

    @pytest.mark.asyncio
    async def test_recommendations_sorted_by_severity(self):
        engine = HealthEngine()
        nodes = [
            make_node("offline-node", status=NodeStatus.OFFLINE),
            make_node(
                "idle-node",
                memory_total=128.0,
                memory_used=30.0,
                loaded_models=[("phi4:14b", 9.0)],
            ),
        ]
        nodes[0].last_heartbeat = time.time() - 300
        report = await engine.analyze(FakeRegistry(nodes), None)
        # Critical should come first
        assert len(report.recommendations) >= 2
        assert report.recommendations[0].severity == Severity.CRITICAL


class TestTraceChecks:
    """Tests for checks that query the TraceStore."""

    @pytest_asyncio.fixture
    async def store(self, tmp_path):
        s = TraceStore(data_dir=str(tmp_path))
        await s.initialize()
        yield s
        await s.close()

    @pytest.mark.asyncio
    async def test_model_thrashing_detected(self, store):
        """Cold loads within the last hour → active warning."""
        engine = HealthEngine()
        for i in range(5):
            await store.record_trace(
                request_id=f"cold-{i}",
                model="llama3:70b",
                original_model="llama3:70b",
                node_id="studio",
                status="completed",
                latency_ms=50000.0,
                time_to_first_token_ms=45000.0,
            )
        node = make_node("studio", memory_total=128.0, memory_used=40.0)
        report = await engine.analyze(FakeRegistry([node]), store)
        thrashing = [r for r in report.recommendations if r.check_id == "model_thrashing"]
        assert len(thrashing) == 1
        assert thrashing[0].severity == Severity.WARNING
        assert "OLLAMA_KEEP_ALIVE" in thrashing[0].fix

    @pytest.mark.asyncio
    async def test_no_thrashing_if_few_cold_loads(self, store):
        engine = HealthEngine()
        # Only 2 cold loads — below the threshold of 3
        for i in range(2):
            await store.record_trace(
                request_id=f"cold-{i}",
                model="llama3:70b",
                original_model="llama3:70b",
                node_id="studio",
                status="completed",
                latency_ms=50000.0,
                time_to_first_token_ms=45000.0,
            )
        node = make_node("studio", memory_total=128.0, memory_used=40.0)
        report = await engine.analyze(FakeRegistry([node]), store)
        thrashing = [r for r in report.recommendations if r.check_id == "model_thrashing"]
        assert len(thrashing) == 0

    @pytest.mark.asyncio
    async def test_model_thrashing_resolved_when_no_recent_cold_loads(self, store):
        """Cold loads only in the distant past (>1h ago) → resolved info."""
        engine = HealthEngine()
        # Insert cold loads backdated to 6 hours ago
        old_ts = time.time() - 6 * 3600
        for i in range(10):
            await store._db.execute(
                "INSERT INTO request_traces "
                "(request_id, model, original_model, node_id, status, "
                "latency_ms, time_to_first_token_ms, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (f"old-cold-{i}", "llama3:70b", "llama3:70b", "studio",
                 "completed", 50000.0, 45000.0, old_ts + i),
            )
        await store._db.commit()
        node = make_node("studio", memory_total=128.0, memory_used=40.0)
        report = await engine.analyze(FakeRegistry([node]), store)
        thrashing = [r for r in report.recommendations if r.check_id == "model_thrashing"]
        assert len(thrashing) == 1
        assert thrashing[0].severity == Severity.INFO
        assert "resolved" in thrashing[0].title.lower()
        assert thrashing[0].data.get("resolved") is True

    @pytest.mark.asyncio
    async def test_error_rate_resolved_when_recent_is_clean(self, store):
        """High error rate in 24h but clean recent window → resolved info."""
        engine = HealthEngine()
        # Insert old failures (6h ago)
        old_ts = time.time() - 6 * 3600
        for i in range(5):
            await store._db.execute(
                "INSERT INTO request_traces "
                "(request_id, model, original_model, node_id, status, "
                "latency_ms, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"old-fail-{i}", "phi4:14b", "phi4:14b", "flaky",
                 "failed", 500.0, old_ts + i),
            )
        # Insert recent successes (within last hour)
        for i in range(5):
            await store.record_trace(
                request_id=f"recent-ok-{i}",
                model="phi4:14b",
                original_model="phi4:14b",
                node_id="flaky",
                status="completed",
                latency_ms=1000.0,
            )
        await store._db.commit()
        node = make_node("flaky")
        report = await engine.analyze(FakeRegistry([node]), store)
        errors = [r for r in report.recommendations if r.check_id == "high_error_rate"]
        assert len(errors) == 1
        assert errors[0].severity == Severity.INFO
        assert "recovered" in errors[0].title.lower()
        assert errors[0].data.get("resolved") is True

    @pytest.mark.asyncio
    async def test_high_error_rate_detected(self, store):
        engine = HealthEngine()
        for i in range(8):
            await store.record_trace(
                request_id=f"ok-{i}",
                model="phi4:14b",
                original_model="phi4:14b",
                node_id="flaky",
                status="completed",
                latency_ms=1000.0,
            )
        for i in range(2):
            await store.record_trace(
                request_id=f"fail-{i}",
                model="phi4:14b",
                original_model="phi4:14b",
                node_id="flaky",
                status="failed",
                latency_ms=500.0,
                error_message="Connection refused",
            )
        node = make_node("flaky")
        report = await engine.analyze(FakeRegistry([node]), store)
        errors = [r for r in report.recommendations if r.check_id == "high_error_rate"]
        assert len(errors) == 1
        assert "20.0%" in errors[0].description

    @pytest.mark.asyncio
    async def test_no_error_rate_if_below_threshold(self, store):
        engine = HealthEngine()
        for i in range(99):
            await store.record_trace(
                request_id=f"ok-{i}",
                model="phi4:14b",
                original_model="phi4:14b",
                node_id="solid",
                status="completed",
                latency_ms=1000.0,
            )
        await store.record_trace(
            request_id="fail-0",
            model="phi4:14b",
            original_model="phi4:14b",
            node_id="solid",
            status="failed",
            latency_ms=500.0,
        )
        node = make_node("solid")
        report = await engine.analyze(FakeRegistry([node]), store)
        errors = [r for r in report.recommendations if r.check_id == "high_error_rate"]
        assert len(errors) == 0  # 1% error rate is below 5% threshold

    @pytest.mark.asyncio
    async def test_vitals_populated_from_traces(self, store):
        engine = HealthEngine()
        await store.record_trace(
            request_id="r1",
            model="phi4:14b",
            original_model="phi4:14b",
            node_id="node-a",
            status="completed",
            latency_ms=1000.0,
            time_to_first_token_ms=500.0,
            retry_count=1,
        )
        node = make_node("node-a")
        report = await engine.analyze(FakeRegistry([node]), store)
        assert report.vitals.total_requests_24h == 1
        assert report.vitals.total_retries_24h == 1
        assert report.vitals.avg_ttft_ms is not None


class TestVersionMismatchCheck:
    """Tests for node agent version mismatch detection."""

    @pytest.mark.asyncio
    async def test_matching_versions_no_recommendation(self):
        from fleet_manager import __version__

        engine = HealthEngine()
        node = make_node("studio", agent_version=__version__)
        report = await engine.analyze(FakeRegistry([node]), None)
        version_recs = [r for r in report.recommendations if "version" in r.check_id]
        assert len(version_recs) == 0

    @pytest.mark.asyncio
    async def test_mismatched_version_warning(self):
        engine = HealthEngine()
        node = make_node("old-node", agent_version="0.0.1")
        report = await engine.analyze(FakeRegistry([node]), None)
        mismatch = [r for r in report.recommendations if r.check_id == "version_mismatch"]
        assert len(mismatch) == 1
        assert mismatch[0].severity == Severity.WARNING
        assert "old-node" in mismatch[0].description

    @pytest.mark.asyncio
    async def test_empty_version_info(self):
        engine = HealthEngine()
        node = make_node("legacy-node", agent_version="")
        report = await engine.analyze(FakeRegistry([node]), None)
        unknown = [r for r in report.recommendations if r.check_id == "version_unknown"]
        assert len(unknown) == 1
        assert unknown[0].severity == Severity.INFO
        assert "legacy-node" in unknown[0].description

    @pytest.mark.asyncio
    async def test_offline_nodes_ignored(self):
        engine = HealthEngine()
        node = make_node("dead", status=NodeStatus.OFFLINE, agent_version="0.0.1")
        node.last_heartbeat = time.time() - 300
        report = await engine.analyze(FakeRegistry([node]), None)
        version_recs = [r for r in report.recommendations if r.check_id == "version_mismatch"]
        assert len(version_recs) == 0


class TestContextProtectionCheck:
    """Tests for context protection health visibility."""

    @pytest.mark.asyncio
    async def test_no_events_no_recommendation(self):
        engine = HealthEngine()
        node = make_node("studio")
        report = await engine.analyze(FakeRegistry([node]), None)
        ctx_recs = [r for r in report.recommendations if "context_protection" in r.check_id]
        assert len(ctx_recs) == 0

    @pytest.mark.asyncio
    async def test_stripped_events_info(self):
        from fleet_manager.server.streaming import _context_protection_events, _record_context_protection

        _context_protection_events.clear()
        for _ in range(5):
            _record_context_protection("stripped", "gpt-oss:120b", "studio", 4096, 32768)

        engine = HealthEngine()
        node = make_node("studio")
        report = await engine.analyze(FakeRegistry([node]), None)
        active = [r for r in report.recommendations if r.check_id == "context_protection_active"]
        assert len(active) == 1
        assert active[0].severity == Severity.INFO
        assert active[0].data["stripped"] == 5

        _context_protection_events.clear()

    @pytest.mark.asyncio
    async def test_warning_events_warning(self):
        from fleet_manager.server.streaming import _context_protection_events, _record_context_protection

        _context_protection_events.clear()
        _record_context_protection("warning", "small-model:7b", "studio", 65536, 32768)

        engine = HealthEngine()
        node = make_node("studio")
        report = await engine.analyze(FakeRegistry([node]), None)
        insufficient = [r for r in report.recommendations if r.check_id == "context_protection_insufficient"]
        assert len(insufficient) == 1
        assert insufficient[0].severity == Severity.WARNING

        _context_protection_events.clear()

    @pytest.mark.asyncio
    async def test_upgraded_events_info(self):
        from fleet_manager.server.streaming import _context_protection_events, _record_context_protection

        _context_protection_events.clear()
        _record_context_protection("upgraded", "small:7b", "studio", 65536, 32768, "big:70b")

        engine = HealthEngine()
        node = make_node("studio")
        report = await engine.analyze(FakeRegistry([node]), None)
        active = [r for r in report.recommendations if r.check_id == "context_protection_active"]
        assert len(active) == 1
        assert active[0].data["upgraded"] == 1

        _context_protection_events.clear()


class TestZombieReaperCheck:
    """Tests for zombie reaper health visibility."""

    @pytest.mark.asyncio
    async def test_no_events_no_recommendation(self):
        engine = HealthEngine()
        node = make_node("studio")
        report = await engine.analyze(FakeRegistry([node]), None)
        reaper_recs = [r for r in report.recommendations if r.check_id == "zombie_reaper_active"]
        assert len(reaper_recs) == 0

    @pytest.mark.asyncio
    async def test_few_zombies_warning(self):
        from fleet_manager.server.queue_manager import _reaper_events

        _reaper_events.clear()
        for i in range(3):
            _reaper_events.append({
                "timestamp": time.time(),
                "request_id": f"zombie-{i}",
                "queue_key": "studio:gpt-oss:120b",
                "stuck_seconds": 920,
            })

        engine = HealthEngine()
        node = make_node("studio")
        report = await engine.analyze(FakeRegistry([node]), None)
        reaper = [r for r in report.recommendations if r.check_id == "zombie_reaper_active"]
        assert len(reaper) == 1
        assert reaper[0].severity == Severity.WARNING
        assert reaper[0].data["total_reaped"] == 3

        _reaper_events.clear()

    @pytest.mark.asyncio
    async def test_many_zombies_critical(self):
        from fleet_manager.server.queue_manager import _reaper_events

        _reaper_events.clear()
        for i in range(15):
            _reaper_events.append({
                "timestamp": time.time(),
                "request_id": f"zombie-{i}",
                "queue_key": "studio:gpt-oss:120b",
                "stuck_seconds": 1000,
            })

        engine = HealthEngine()
        node = make_node("studio")
        report = await engine.analyze(FakeRegistry([node]), None)
        reaper = [r for r in report.recommendations if r.check_id == "zombie_reaper_active"]
        assert len(reaper) == 1
        assert reaper[0].severity == Severity.CRITICAL

        _reaper_events.clear()
