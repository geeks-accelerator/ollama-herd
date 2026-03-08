"""Tests for the TraceStore — per-request trace logging and usage stats."""

from __future__ import annotations

import time

import pytest
import pytest_asyncio

from fleet_manager.server.trace_store import TraceStore


@pytest_asyncio.fixture
async def store(tmp_path):
    s = TraceStore(data_dir=str(tmp_path))
    await s.initialize()
    yield s
    await s.close()


async def _seed_traces(store: TraceStore):
    """Insert sample traces for testing queries."""
    now = time.time()
    traces = [
        # Completed request on node-a, model phi4:14b, today
        {
            "request_id": "req-1",
            "model": "phi4:14b",
            "original_model": "phi4:14b",
            "node_id": "node-a",
            "score": 85.0,
            "scores_breakdown": {"thermal": 50, "memory_fit": 20, "queue_depth": 0, "wait_time": 0, "role_affinity": 15},
            "status": "completed",
            "latency_ms": 1500.0,
            "time_to_first_token_ms": 120.0,
            "prompt_tokens": 50,
            "completion_tokens": 100,
            "retry_count": 0,
            "fallback_used": False,
            "original_format": "openai",
        },
        # Completed request on node-b, model phi4:14b, today
        {
            "request_id": "req-2",
            "model": "phi4:14b",
            "original_model": "phi4:14b",
            "node_id": "node-b",
            "score": 72.0,
            "status": "completed",
            "latency_ms": 2200.0,
            "time_to_first_token_ms": 200.0,
            "prompt_tokens": 60,
            "completion_tokens": 150,
        },
        # Failed request on node-a, model llama3:8b, today
        {
            "request_id": "req-3",
            "model": "llama3:8b",
            "original_model": "llama3:70b",
            "node_id": "node-a",
            "score": 40.0,
            "status": "failed",
            "latency_ms": 500.0,
            "retry_count": 1,
            "fallback_used": True,
            "excluded_nodes": ["node-c"],
            "error_message": "Connection refused",
            "original_format": "ollama",
        },
    ]
    for t in traces:
        await store.record_trace(**t)


class TestTraceStoreInit:
    @pytest.mark.asyncio
    async def test_initialize_creates_table(self, tmp_path):
        store = TraceStore(data_dir=str(tmp_path))
        await store.initialize()
        # Verify table exists by querying it
        cursor = await store._db.execute(
            "SELECT count(*) FROM request_traces"
        )
        row = await cursor.fetchone()
        assert row[0] == 0
        await store.close()

    @pytest.mark.asyncio
    async def test_initialize_idempotent(self, tmp_path):
        """Calling initialize twice doesn't error."""
        store = TraceStore(data_dir=str(tmp_path))
        await store.initialize()
        await store.initialize()  # Should not raise
        await store.close()


class TestRecordTrace:
    @pytest.mark.asyncio
    async def test_record_and_retrieve(self, store):
        await store.record_trace(
            request_id="test-123",
            model="phi4:14b",
            original_model="phi4:14b",
            node_id="node-a",
            status="completed",
            latency_ms=1234.5,
        )
        traces = await store.get_recent_traces(limit=10)
        assert len(traces) == 1
        assert traces[0]["request_id"] == "test-123"
        assert traces[0]["model"] == "phi4:14b"
        assert traces[0]["status"] == "completed"
        assert traces[0]["latency_ms"] == 1234.5

    @pytest.mark.asyncio
    async def test_record_with_all_fields(self, store):
        await store.record_trace(
            request_id="full-trace",
            model="llama3:8b",
            original_model="llama3:70b",
            node_id="node-b",
            score=85.5,
            scores_breakdown={"thermal": 50, "memory_fit": 20},
            status="completed",
            latency_ms=2000.0,
            time_to_first_token_ms=150.5,
            prompt_tokens=100,
            completion_tokens=500,
            retry_count=1,
            fallback_used=True,
            excluded_nodes=["node-a"],
            client_ip="192.168.1.5",
            original_format="openai",
            error_message=None,
        )
        traces = await store.get_recent_traces()
        t = traces[0]
        assert t["request_id"] == "full-trace"
        assert t["original_model"] == "llama3:70b"
        assert t["score"] == 85.5
        assert t["scores_breakdown"] == {"thermal": 50, "memory_fit": 20}
        assert t["time_to_first_token_ms"] == 150.5
        assert t["prompt_tokens"] == 100
        assert t["completion_tokens"] == 500
        assert t["retry_count"] == 1
        assert t["fallback_used"] is True
        assert t["excluded_nodes"] == ["node-a"]
        assert t["client_ip"] == "192.168.1.5"
        assert t["original_format"] == "openai"

    @pytest.mark.asyncio
    async def test_record_without_initialize(self):
        """record_trace is a no-op when store hasn't been initialized."""
        store = TraceStore(data_dir="/tmp/nonexistent")
        # Should not raise
        await store.record_trace(
            request_id="x", model="x", original_model="x",
            node_id="x", status="completed",
        )

    @pytest.mark.asyncio
    async def test_get_trace_by_request_id(self, store):
        await store.record_trace(
            request_id="lookup-me", model="phi4:14b", original_model="phi4:14b",
            node_id="node-a", status="retried", latency_ms=100.0,
            error_message="ConnectError",
        )
        await store.record_trace(
            request_id="lookup-me", model="phi4:14b", original_model="phi4:14b",
            node_id="node-b", status="completed", latency_ms=1500.0,
        )
        await store.record_trace(
            request_id="other-req", model="phi4:14b", original_model="phi4:14b",
            node_id="node-a", status="completed", latency_ms=800.0,
        )
        traces = await store.get_trace_by_request_id("lookup-me")
        assert len(traces) == 2
        assert traces[0]["status"] == "retried"
        assert traces[1]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_recent_traces_ordering(self, store):
        """Most recent traces should come first."""
        for i in range(5):
            await store.record_trace(
                request_id=f"req-{i}", model="phi4:14b", original_model="phi4:14b",
                node_id="node-a", status="completed", latency_ms=float(i * 100),
            )
        traces = await store.get_recent_traces(limit=3)
        assert len(traces) == 3
        assert traces[0]["request_id"] == "req-4"  # newest first


class TestUsageStats:
    @pytest.mark.asyncio
    async def test_usage_by_node_model_day(self, store):
        await _seed_traces(store)
        data = await store.get_usage_by_node_model_day(days=7)
        assert len(data) > 0
        # Should have node-a and node-b entries
        node_ids = {d["node_id"] for d in data}
        assert "node-a" in node_ids
        assert "node-b" in node_ids

    @pytest.mark.asyncio
    async def test_usage_empty(self, store):
        data = await store.get_usage_by_node_model_day(days=7)
        assert data == []

    @pytest.mark.asyncio
    async def test_node_summary(self, store):
        await _seed_traces(store)
        summary = await store.get_node_summary()
        assert len(summary) == 2  # node-a and node-b
        node_a = next(s for s in summary if s["node_id"] == "node-a")
        assert node_a["total_requests"] == 2  # req-1 and req-3
        assert node_a["completed_count"] == 1
        assert node_a["failed_count"] == 1

    @pytest.mark.asyncio
    async def test_usage_overview(self, store):
        await _seed_traces(store)
        overview = await store.get_usage_overview()
        assert overview["total_requests"] == 3
        assert overview["completed_count"] == 2
        assert overview["failed_count"] == 1
        assert overview["total_prompt_tokens"] == 110  # 50 + 60
        assert overview["total_completion_tokens"] == 250  # 100 + 150
        assert overview["total_tokens"] == 360
        assert overview["total_retries"] == 1
        assert overview["total_fallbacks"] == 1

    @pytest.mark.asyncio
    async def test_usage_overview_empty(self, store):
        overview = await store.get_usage_overview()
        assert overview["total_requests"] == 0


class TestTagAnalytics:
    @pytest.mark.asyncio
    async def test_record_trace_with_tags(self, store):
        await store.record_trace(
            request_id="tagged-1",
            model="phi4:14b",
            original_model="phi4:14b",
            node_id="node-a",
            status="completed",
            latency_ms=1000.0,
            tags=["my-app", "production"],
        )
        traces = await store.get_recent_traces(limit=1)
        assert len(traces) == 1
        assert traces[0]["tags"] == ["my-app", "production"]

    @pytest.mark.asyncio
    async def test_record_trace_without_tags(self, store):
        await store.record_trace(
            request_id="no-tags",
            model="phi4:14b",
            original_model="phi4:14b",
            node_id="node-a",
            status="completed",
            latency_ms=500.0,
        )
        traces = await store.get_recent_traces(limit=1)
        assert traces[0]["tags"] is None

    @pytest.mark.asyncio
    async def test_get_usage_by_tag(self, store):
        await store.record_trace(
            request_id="t1", model="phi4:14b", original_model="phi4:14b",
            node_id="node-a", status="completed", latency_ms=1000.0,
            prompt_tokens=50, completion_tokens=100,
            tags=["app-a", "prod"],
        )
        await store.record_trace(
            request_id="t2", model="phi4:14b", original_model="phi4:14b",
            node_id="node-a", status="completed", latency_ms=2000.0,
            prompt_tokens=60, completion_tokens=200,
            tags=["app-a"],
        )
        await store.record_trace(
            request_id="t3", model="llama3:8b", original_model="llama3:8b",
            node_id="node-b", status="failed", latency_ms=500.0,
            tags=["app-b"],
        )
        data = await store.get_usage_by_tag(days=7)
        tags_map = {d["tag"]: d for d in data}

        assert "app-a" in tags_map
        assert tags_map["app-a"]["request_count"] == 2
        assert tags_map["app-a"]["completed_count"] == 2
        assert tags_map["app-a"]["total_prompt_tokens"] == 110
        assert tags_map["app-a"]["total_completion_tokens"] == 300

        assert "prod" in tags_map
        assert tags_map["prod"]["request_count"] == 1

        assert "app-b" in tags_map
        assert tags_map["app-b"]["request_count"] == 1
        assert tags_map["app-b"]["failed_count"] == 1

    @pytest.mark.asyncio
    async def test_get_usage_by_tag_empty(self, store):
        data = await store.get_usage_by_tag(days=7)
        assert data == []

    @pytest.mark.asyncio
    async def test_get_tag_daily_stats(self, store):
        await store.record_trace(
            request_id="d1", model="phi4:14b", original_model="phi4:14b",
            node_id="node-a", status="completed", latency_ms=1000.0,
            prompt_tokens=50, completion_tokens=100,
            tags=["my-app"],
        )
        data = await store.get_tag_daily_stats(days=7)
        assert len(data) >= 1
        assert data[0]["tag"] == "my-app"
        assert data[0]["request_count"] == 1
        assert data[0]["total_tokens"] == 150

    @pytest.mark.asyncio
    async def test_get_tag_summary(self, store):
        await store.record_trace(
            request_id="s1", model="phi4:14b", original_model="phi4:14b",
            node_id="node-a", status="completed", latency_ms=1000.0,
            tags=["project-x"],
        )
        await store.record_trace(
            request_id="s2", model="phi4:14b", original_model="phi4:14b",
            node_id="node-a", status="completed", latency_ms=2000.0,
            tags=["project-x", "staging"],
        )
        summary = await store.get_tag_summary()
        tags_map = {s["tag"]: s for s in summary}
        assert "project-x" in tags_map
        assert tags_map["project-x"]["total_requests"] == 2
        assert "staging" in tags_map
        assert tags_map["staging"]["total_requests"] == 1


class TestBenchmarkRuns:
    @pytest.mark.asyncio
    async def test_save_and_get_benchmark_run(self, store):
        data = {
            "run_id": "bench-001",
            "timestamp": time.time(),
            "duration_s": 60.0,
            "total_requests": 25,
            "total_failures": 3,
            "total_prompt_tokens": 1000,
            "total_completion_tokens": 5000,
            "requests_per_sec": 0.42,
            "tokens_per_sec": 83.3,
            "latency_p50_ms": 4200.0,
            "latency_p95_ms": 8100.0,
            "latency_p99_ms": 9500.0,
            "ttft_p50_ms": 850.0,
            "ttft_p95_ms": 1200.0,
            "ttft_p99_ms": 1400.0,
            "fleet_snapshot": {
                "nodes": [
                    {"node_id": "mac-studio", "cores": 32, "memory_total_gb": 512},
                ],
                "models": [
                    {"name": "llama3:70b", "size_gb": 40, "concurrency": 4},
                ],
            },
            "per_model_results": [
                {"model": "llama3:70b", "requests": 25, "tok_s": 83.3, "avg_latency_ms": 4200},
            ],
            "per_node_results": [
                {"node_id": "mac-studio", "requests": 25, "pct": 100, "tok_s": 83.3, "tokens": 5000},
            ],
            "peak_utilization": [
                {"node_id": "mac-studio", "cpu_peak": 80.0, "mem_peak": 65.0, "active_peak": 4},
            ],
        }
        await store.save_benchmark_run(data)

        run = await store.get_benchmark_run("bench-001")
        assert run is not None
        assert run["run_id"] == "bench-001"
        assert run["duration_s"] == 60.0
        assert run["total_requests"] == 25
        assert run["total_failures"] == 3
        assert run["tokens_per_sec"] == 83.3
        assert run["latency_p50_ms"] == 4200.0
        assert run["fleet_snapshot"]["nodes"][0]["node_id"] == "mac-studio"
        assert run["per_model_results"][0]["model"] == "llama3:70b"
        assert run["per_node_results"][0]["tokens"] == 5000
        assert run["peak_utilization"][0]["cpu_peak"] == 80.0

    @pytest.mark.asyncio
    async def test_get_benchmark_runs_list(self, store):
        for i in range(3):
            await store.save_benchmark_run({
                "run_id": f"bench-{i:03d}",
                "timestamp": time.time() + i,
                "duration_s": 60.0,
                "total_requests": 10 + i,
                "total_failures": 0,
                "total_prompt_tokens": 500,
                "total_completion_tokens": 2000,
                "requests_per_sec": 0.5,
                "tokens_per_sec": 50.0,
            })
        runs = await store.get_benchmark_runs(limit=50)
        assert len(runs) == 3
        # Newest first
        assert runs[0]["run_id"] == "bench-002"
        assert runs[2]["run_id"] == "bench-000"

    @pytest.mark.asyncio
    async def test_benchmark_run_not_found(self, store):
        run = await store.get_benchmark_run("nonexistent")
        assert run is None

    @pytest.mark.asyncio
    async def test_save_benchmark_without_initialize(self):
        store = TraceStore(data_dir="/tmp/nonexistent")
        await store.save_benchmark_run({
            "run_id": "x",
            "timestamp": time.time(),
            "duration_s": 10,
            "total_requests": 0,
            "total_failures": 0,
            "total_prompt_tokens": 0,
            "total_completion_tokens": 0,
        })
        # Should not raise

    @pytest.mark.asyncio
    async def test_get_benchmark_runs_empty(self, store):
        runs = await store.get_benchmark_runs()
        assert runs == []


class TestHealthQueries:
    @pytest.mark.asyncio
    async def test_get_cold_loads_24h(self, store):
        # Insert a cold load (TTFT > 40s)
        await store.record_trace(
            request_id="cold-1",
            model="llama3:70b",
            original_model="llama3:70b",
            node_id="node-a",
            status="completed",
            latency_ms=45000.0,
            time_to_first_token_ms=42000.0,
        )
        # Insert a normal trace (TTFT < 40s)
        await store.record_trace(
            request_id="warm-1",
            model="phi4:14b",
            original_model="phi4:14b",
            node_id="node-a",
            status="completed",
            latency_ms=2000.0,
            time_to_first_token_ms=500.0,
        )
        result = await store.get_cold_loads_24h(ttft_threshold_ms=40000)
        assert result["total_count"] == 1
        assert result["by_node"]["node-a"] == 1

    @pytest.mark.asyncio
    async def test_get_cold_loads_empty(self, store):
        result = await store.get_cold_loads_24h()
        assert result["total_count"] == 0
        assert result["by_node"] == {}

    @pytest.mark.asyncio
    async def test_get_error_rates_24h(self, store):
        await _seed_traces(store)
        rates = await store.get_error_rates_24h()
        assert len(rates) > 0
        node_a = next(r for r in rates if r["node_id"] == "node-a")
        assert node_a["total"] == 2
        assert node_a["failed"] == 1
        assert node_a["error_rate_pct"] == 50.0

    @pytest.mark.asyncio
    async def test_get_error_rates_empty(self, store):
        rates = await store.get_error_rates_24h()
        assert rates == []

    @pytest.mark.asyncio
    async def test_get_retry_stats_24h(self, store):
        await _seed_traces(store)
        stats = await store.get_retry_stats_24h()
        assert stats["total_requests"] == 3
        assert stats["total_retries"] == 1

    @pytest.mark.asyncio
    async def test_get_retry_stats_empty(self, store):
        stats = await store.get_retry_stats_24h()
        assert stats["total_requests"] == 0
        assert stats["total_retries"] == 0

    @pytest.mark.asyncio
    async def test_get_overall_stats_24h(self, store):
        await _seed_traces(store)
        stats = await store.get_overall_stats_24h()
        assert stats["total_requests"] == 3
        assert stats["error_rate_pct"] > 0
        assert stats["total_retries"] == 1

    @pytest.mark.asyncio
    async def test_get_overall_stats_empty(self, store):
        stats = await store.get_overall_stats_24h()
        assert stats["total_requests"] == 0
