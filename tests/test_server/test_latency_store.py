"""Tests for the LatencyStore."""

from __future__ import annotations

import tempfile

import pytest

from fleet_manager.server.latency_store import LatencyStore


@pytest.mark.asyncio
class TestLatencyStore:
    async def test_initialize_creates_db(self, tmp_path):
        store = LatencyStore(data_dir=str(tmp_path))
        await store.initialize()
        assert (tmp_path / "latency.db").exists()
        await store.close()

    async def test_record_and_get_percentile(self, tmp_path):
        store = LatencyStore(data_dir=str(tmp_path))
        await store.initialize()

        await store.record("studio", "phi4:14b", 500.0)
        await store.record("studio", "phi4:14b", 600.0)
        await store.record("studio", "phi4:14b", 700.0)
        await store.record("studio", "phi4:14b", 800.0)

        p75 = await store.get_percentile("studio", "phi4:14b", 75)
        assert p75 is not None
        assert 600.0 <= p75 <= 800.0
        await store.close()

    async def test_get_percentile_empty(self, tmp_path):
        store = LatencyStore(data_dir=str(tmp_path))
        await store.initialize()

        p75 = await store.get_percentile("nonexistent", "model", 75)
        assert p75 is None
        await store.close()

    async def test_cached_percentile(self, tmp_path):
        store = LatencyStore(data_dir=str(tmp_path))
        await store.initialize()

        # No data yet
        assert store.get_cached_percentile("studio", "phi4:14b") is None

        # Record data — cache should update
        await store.record("studio", "phi4:14b", 500.0)
        cached = store.get_cached_percentile("studio", "phi4:14b")
        assert cached is not None
        assert cached == 500.0
        await store.close()

    async def test_cached_percentile_updates(self, tmp_path):
        store = LatencyStore(data_dir=str(tmp_path))
        await store.initialize()

        await store.record("studio", "phi4:14b", 100.0)
        c1 = store.get_cached_percentile("studio", "phi4:14b")

        await store.record("studio", "phi4:14b", 900.0)
        c2 = store.get_cached_percentile("studio", "phi4:14b")

        # Cache should have been updated with new data
        assert c2 is not None
        await store.close()

    async def test_refresh_cache_on_init(self, tmp_path):
        # Create store and add data
        store1 = LatencyStore(data_dir=str(tmp_path))
        await store1.initialize()
        await store1.record("studio", "phi4:14b", 500.0)
        await store1.record("studio", "phi4:14b", 700.0)
        await store1.close()

        # New store instance — cache should be populated on init
        store2 = LatencyStore(data_dir=str(tmp_path))
        await store2.initialize()
        cached = store2.get_cached_percentile("studio", "phi4:14b")
        assert cached is not None
        await store2.close()

    async def test_multiple_nodes_and_models(self, tmp_path):
        store = LatencyStore(data_dir=str(tmp_path))
        await store.initialize()

        await store.record("studio", "phi4:14b", 500.0)
        await store.record("macbook", "phi4:14b", 800.0)
        await store.record("studio", "llama3.3:70b", 3000.0)

        p1 = await store.get_percentile("studio", "phi4:14b")
        p2 = await store.get_percentile("macbook", "phi4:14b")
        p3 = await store.get_percentile("studio", "llama3.3:70b")

        assert p1 == 500.0
        assert p2 == 800.0
        assert p3 == 3000.0
        await store.close()

    async def test_record_without_initialize(self, tmp_path):
        store = LatencyStore(data_dir=str(tmp_path))
        # Should not crash, just silently skip
        await store.record("studio", "phi4:14b", 500.0)
        result = await store.get_percentile("studio", "phi4:14b")
        assert result is None

    async def test_record_with_tokens(self, tmp_path):
        store = LatencyStore(data_dir=str(tmp_path))
        await store.initialize()
        await store.record(
            "studio", "phi4:14b", 500.0,
            prompt_tokens=50, completion_tokens=200,
        )
        # Verify via raw SQL
        cursor = await store._db.execute(
            "SELECT prompt_tokens, completion_tokens FROM latency_observations LIMIT 1"
        )
        row = await cursor.fetchone()
        assert row[0] == 50
        assert row[1] == 200
        await store.close()

    async def test_record_without_tokens_backward_compat(self, tmp_path):
        store = LatencyStore(data_dir=str(tmp_path))
        await store.initialize()
        # Old-style call without token params
        await store.record("studio", "phi4:14b", 500.0)
        cursor = await store._db.execute(
            "SELECT prompt_tokens, completion_tokens FROM latency_observations LIMIT 1"
        )
        row = await cursor.fetchone()
        assert row[0] is None
        assert row[1] is None
        await store.close()

    async def test_get_hourly_trends(self, tmp_path):
        import time

        store = LatencyStore(data_dir=str(tmp_path))
        await store.initialize()

        now = time.time()
        # Insert data across 3 different hours
        for i in range(3):
            ts = now - (i * 3600)
            await store._db.execute(
                "INSERT INTO latency_observations "
                "(node_id, model_name, latency_ms, prompt_tokens, completion_tokens, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("studio", "phi4:14b", 500.0 + i * 100, 50, 200, ts),
            )
        await store._db.commit()

        trends = await store.get_hourly_trends(hours=6)
        assert len(trends) >= 2  # At least 2 different hour buckets
        assert all("request_count" in t for t in trends)
        assert all("avg_latency_ms" in t for t in trends)
        assert all("total_prompt_tokens" in t for t in trends)
        await store.close()

    async def test_get_hourly_trends_empty(self, tmp_path):
        store = LatencyStore(data_dir=str(tmp_path))
        await store.initialize()
        trends = await store.get_hourly_trends(hours=24)
        assert trends == []
        await store.close()

    async def test_get_model_daily_stats(self, tmp_path):
        import time

        store = LatencyStore(data_dir=str(tmp_path))
        await store.initialize()

        now = time.time()
        # Two models, two days
        for model in ("phi4:14b", "llama3.3:70b"):
            for day_offset in (0, 1):
                ts = now - (day_offset * 86400)
                await store._db.execute(
                    "INSERT INTO latency_observations "
                    "(node_id, model_name, latency_ms, prompt_tokens, completion_tokens, timestamp) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ("studio", model, 500.0, 50, 200, ts),
                )
        await store._db.commit()

        daily = await store.get_model_daily_stats(days=3)
        assert len(daily) >= 2
        models_seen = {d["model_name"] for d in daily}
        assert "phi4:14b" in models_seen
        assert "llama3.3:70b" in models_seen
        await store.close()

    async def test_get_model_summary(self, tmp_path):
        store = LatencyStore(data_dir=str(tmp_path))
        await store.initialize()

        await store.record("studio", "phi4:14b", 500.0, prompt_tokens=50, completion_tokens=200)
        await store.record("studio", "phi4:14b", 600.0, prompt_tokens=60, completion_tokens=300)
        await store.record("studio", "llama3.3:70b", 3000.0, prompt_tokens=100, completion_tokens=500)

        summary = await store.get_model_summary()
        assert len(summary) == 2
        # phi4 has more requests, so it should be first (sorted by total_requests DESC)
        assert summary[0]["model_name"] == "phi4:14b"
        assert summary[0]["total_requests"] == 2
        assert summary[0]["total_prompt_tokens"] == 110
        assert summary[0]["total_completion_tokens"] == 500
        assert summary[1]["model_name"] == "llama3.3:70b"
        assert summary[1]["total_requests"] == 1
        await store.close()

    async def test_migration_idempotent(self, tmp_path):
        # Initialize twice — should not error
        store = LatencyStore(data_dir=str(tmp_path))
        await store.initialize()
        await store.record("studio", "phi4:14b", 500.0, prompt_tokens=50, completion_tokens=200)
        await store.close()

        store2 = LatencyStore(data_dir=str(tmp_path))
        await store2.initialize()
        # Data should still be there
        summary = await store2.get_model_summary()
        assert len(summary) == 1
        assert summary[0]["total_prompt_tokens"] == 50
        await store2.close()

    async def test_node_model_daily_stats(self, tmp_path):
        """get_node_model_daily_stats groups by node_id, model, day."""
        store = LatencyStore(data_dir=str(tmp_path))
        await store.initialize()
        # Add records for different nodes and models
        await store.record("node-a", "phi4:14b", 500.0, prompt_tokens=50, completion_tokens=200)
        await store.record("node-a", "phi4:14b", 600.0, prompt_tokens=60, completion_tokens=300)
        await store.record("node-b", "phi4:14b", 700.0, prompt_tokens=70, completion_tokens=400)
        await store.record("node-a", "llama3:8b", 300.0, prompt_tokens=30, completion_tokens=100)

        data = await store.get_node_model_daily_stats(days=7)
        assert len(data) >= 3  # 3 unique node:model combos
        # Check node-a phi4:14b has 2 requests
        node_a_phi = [d for d in data if d["node_id"] == "node-a" and d["model_name"] == "phi4:14b"]
        assert len(node_a_phi) == 1
        assert node_a_phi[0]["request_count"] == 2
        assert node_a_phi[0]["total_prompt_tokens"] == 110
        # Check node-b phi4:14b has 1 request
        node_b_phi = [d for d in data if d["node_id"] == "node-b" and d["model_name"] == "phi4:14b"]
        assert len(node_b_phi) == 1
        assert node_b_phi[0]["request_count"] == 1
        await store.close()
