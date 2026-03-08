"""Tests for the ScoringEngine."""

from __future__ import annotations

import time

import pytest

from fleet_manager.models.config import ServerSettings
from fleet_manager.models.node import MemoryPressure, NodeStatus
from fleet_manager.server.registry import NodeRegistry
from fleet_manager.server.scorer import ScoringEngine

from tests.conftest import make_heartbeat


@pytest.fixture
def settings():
    return ServerSettings()


@pytest.fixture
def registry(settings):
    return NodeRegistry(settings)


@pytest.fixture
def scorer(settings, registry):
    return ScoringEngine(settings, registry)


@pytest.mark.asyncio
class TestScoringEngine:
    async def test_no_nodes_returns_empty(self, scorer):
        results = scorer.score_request("llama3.3:70b", {})
        assert results == []

    async def test_hot_model_scores_highest(self, scorer, registry):
        # Node A has the model loaded (hot)
        hb_a = make_heartbeat(
            node_id="a",
            memory_total=128.0,
            memory_used=40.0,
            loaded_models=[("llama3.3:70b", 40.0)],
        )
        # Node B has the model on disk only (cold)
        hb_b = make_heartbeat(
            node_id="b",
            memory_total=128.0,
            memory_used=20.0,
            loaded_models=[],
            available_models=["llama3.3:70b"],
        )
        await registry.update_from_heartbeat(hb_a)
        await registry.update_from_heartbeat(hb_b)

        results = scorer.score_request("llama3.3:70b", {})
        assert len(results) == 2
        assert results[0].node_id == "a"  # hot beats cold
        assert results[0].scores_breakdown["thermal"] == 50.0
        assert results[1].scores_breakdown["thermal"] == 10.0

    async def test_warm_tier_scoring(self, scorer, registry):
        # Register node with model loaded
        hb1 = make_heartbeat(
            node_id="studio",
            memory_total=128.0,
            memory_used=40.0,
            loaded_models=[("phi4:14b", 9.0)],
            available_models=["phi4:14b"],
        )
        await registry.update_from_heartbeat(hb1)

        # Second heartbeat: model unloaded
        hb2 = make_heartbeat(
            node_id="studio",
            memory_total=128.0,
            memory_used=20.0,
            loaded_models=[],
            available_models=["phi4:14b"],
        )
        await registry.update_from_heartbeat(hb2)

        results = scorer.score_request("phi4:14b", {})
        assert len(results) == 1
        # Should be warm, not cold
        assert results[0].scores_breakdown["thermal"] == 30.0

    async def test_offline_node_eliminated(self, scorer, registry):
        hb = make_heartbeat(
            node_id="offline",
            loaded_models=[("llama3.3:70b", 40.0)],
        )
        await registry.update_from_heartbeat(hb)
        registry.handle_drain("offline")

        results = scorer.score_request("llama3.3:70b", {})
        assert results == []

    async def test_critical_pressure_eliminated(self, scorer, registry):
        hb = make_heartbeat(
            node_id="pressured",
            memory_total=64.0,
            memory_used=60.0,
            pressure=MemoryPressure.CRITICAL,
            loaded_models=[("phi4:14b", 9.0)],
        )
        await registry.update_from_heartbeat(hb)

        results = scorer.score_request("phi4:14b", {})
        assert results == []

    async def test_model_not_on_node_eliminated(self, scorer, registry):
        hb = make_heartbeat(
            node_id="studio",
            loaded_models=[("phi4:14b", 9.0)],
        )
        await registry.update_from_heartbeat(hb)

        results = scorer.score_request("llama3.3:70b", {})
        assert results == []

    async def test_queue_depth_penalty(self, scorer, registry):
        # Two identical nodes, one with deep queue
        for nid in ("a", "b"):
            hb = make_heartbeat(
                node_id=nid,
                memory_total=128.0,
                memory_used=40.0,
                loaded_models=[("llama3.3:70b", 40.0)],
            )
            await registry.update_from_heartbeat(hb)

        queue_depths = {"a:llama3.3:70b": 5, "b:llama3.3:70b": 0}
        results = scorer.score_request("llama3.3:70b", queue_depths)

        assert results[0].node_id == "b"  # lower queue wins
        assert results[0].scores_breakdown["queue_depth"] == 0.0
        assert results[1].scores_breakdown["queue_depth"] < 0  # penalty

    async def test_queue_depth_penalty_capped(self, scorer, registry):
        hb = make_heartbeat(
            node_id="busy",
            memory_total=128.0,
            memory_used=40.0,
            loaded_models=[("llama3.3:70b", 40.0)],
        )
        await registry.update_from_heartbeat(hb)

        # Very deep queue
        results = scorer.score_request("llama3.3:70b", {"busy:llama3.3:70b": 100})
        assert results[0].scores_breakdown["queue_depth"] == -30.0  # capped

    async def test_memory_fit_scoring(self, scorer, registry):
        # Node with lots of memory available
        hb_big = make_heartbeat(
            node_id="big",
            memory_total=192.0,
            memory_used=20.0,
            available_models=["llama3.3:70b"],
        )
        # Node with tight memory
        hb_tight = make_heartbeat(
            node_id="tight",
            memory_total=64.0,
            memory_used=20.0,
            available_models=["llama3.3:70b"],
        )
        await registry.update_from_heartbeat(hb_big)
        await registry.update_from_heartbeat(hb_tight)

        results = scorer.score_request("llama3.3:70b", {})
        scores = {r.node_id: r.scores_breakdown["memory_fit"] for r in results}
        assert scores["big"] > scores["tight"]

    async def test_role_affinity_large_model_big_node(self, scorer, registry):
        # Both nodes have model loaded (hot) so memory elimination doesn't apply
        hb_big = make_heartbeat(
            node_id="big",
            memory_total=128.0,
            memory_used=20.0,
            loaded_models=[("llama3.3:70b", 40.0)],
        )
        hb_small = make_heartbeat(
            node_id="small",
            memory_total=32.0,
            memory_used=8.0,
            loaded_models=[("llama3.3:70b", 40.0)],
        )
        await registry.update_from_heartbeat(hb_big)
        await registry.update_from_heartbeat(hb_small)

        results = scorer.score_request("llama3.3:70b", {})
        scores = {r.node_id: r.scores_breakdown["role_affinity"] for r in results}
        assert scores["big"] >= scores["small"]

    async def test_role_affinity_small_model_small_node(self, scorer, registry):
        # Small node should be preferred for small models
        hb_big = make_heartbeat(
            node_id="big",
            memory_total=128.0,
            memory_used=20.0,
            loaded_models=[("qwen2.5:0.5b", 0.4)],
        )
        hb_small = make_heartbeat(
            node_id="small",
            memory_total=16.0,
            memory_used=4.0,
            loaded_models=[("qwen2.5:0.5b", 0.4)],
        )
        await registry.update_from_heartbeat(hb_big)
        await registry.update_from_heartbeat(hb_small)

        results = scorer.score_request("qwen2.5:0.5b", {})
        scores = {r.node_id: r.scores_breakdown["role_affinity"] for r in results}
        assert scores["small"] >= scores["big"]

    async def test_results_sorted_by_score(self, scorer, registry):
        for i in range(3):
            hb = make_heartbeat(
                node_id=f"node-{i}",
                memory_total=64.0 * (i + 1),
                memory_used=10.0,
                loaded_models=[("phi4:14b", 9.0)],
            )
            await registry.update_from_heartbeat(hb)

        results = scorer.score_request("phi4:14b", {})
        assert len(results) == 3
        # Should be sorted descending by score
        for i in range(len(results) - 1):
            assert results[i].score >= results[i + 1].score

    async def test_insufficient_memory_eliminated(self, scorer, registry):
        hb = make_heartbeat(
            node_id="low-mem",
            memory_total=16.0,
            memory_used=14.0,  # only 2GB available
            available_models=["llama3.3:70b"],  # needs ~40GB
        )
        await registry.update_from_heartbeat(hb)

        results = scorer.score_request("llama3.3:70b", {})
        assert results == []

    async def test_model_size_estimation(self, scorer, registry):
        hb = make_heartbeat(
            node_id="studio",
            memory_total=192.0,
            memory_used=20.0,
            available_models=["deepseek-r1:671b"],
        )
        await registry.update_from_heartbeat(hb)

        # 671b model estimated at 370GB — should not fit in 172GB available
        results = scorer.score_request("deepseek-r1:671b", {})
        assert results == []

    async def test_wait_time_with_latency_store(self, settings, registry):
        class FakeLatencyStore:
            def get_cached_percentile(self, node_id, model):
                return 5000.0  # 5 seconds per request

        scorer = ScoringEngine(settings, registry, latency_store=FakeLatencyStore())

        hb = make_heartbeat(
            node_id="studio",
            memory_total=128.0,
            memory_used=40.0,
            loaded_models=[("llama3.3:70b", 40.0)],
        )
        await registry.update_from_heartbeat(hb)

        # With queue depth 3, wait_time should be negative (penalty)
        results = scorer.score_request("llama3.3:70b", {"studio:llama3.3:70b": 3})
        assert results[0].scores_breakdown["wait_time"] < 0

        # With queue depth 0, no wait penalty
        results = scorer.score_request("llama3.3:70b", {"studio:llama3.3:70b": 0})
        assert results[0].scores_breakdown["wait_time"] == 0.0
