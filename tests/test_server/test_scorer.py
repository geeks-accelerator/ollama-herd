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

    async def test_context_fit_prefers_larger_context(self, scorer, registry):
        """Node with larger context window should score higher for long requests."""
        # Node A: 8K context
        hb_a = make_heartbeat(
            node_id="small-ctx",
            memory_total=64.0,
            memory_used=20.0,
            loaded_models=[("qwen3-coder:latest", 42.0, 8192)],
        )
        # Node B: 262K context
        hb_b = make_heartbeat(
            node_id="big-ctx",
            memory_total=512.0,
            memory_used=100.0,
            loaded_models=[("qwen3-coder:latest", 42.0, 262144)],
        )
        await registry.update_from_heartbeat(hb_a)
        await registry.update_from_heartbeat(hb_b)

        # Request with ~5K tokens — both can handle but big-ctx has more headroom
        results = scorer.score_request("qwen3-coder:latest", {}, estimated_tokens=5000)
        ctx_scores = {r.node_id: r.scores_breakdown["context_fit"] for r in results}
        assert ctx_scores["big-ctx"] > ctx_scores["small-ctx"]

    async def test_context_fit_penalizes_overflow(self, scorer, registry):
        """Node whose context window is smaller than estimated tokens gets penalized."""
        hb = make_heartbeat(
            node_id="tiny-ctx",
            memory_total=64.0,
            memory_used=20.0,
            loaded_models=[("phi4:14b", 9.0, 4096)],
        )
        await registry.update_from_heartbeat(hb)

        # Request with ~10K tokens — exceeds 4K context
        results = scorer.score_request("phi4:14b", {}, estimated_tokens=10000)
        assert results[0].scores_breakdown["context_fit"] < 0

    async def test_context_fit_neutral_without_tokens(self, scorer, registry):
        """Without token estimate, context_fit should be 0 (neutral)."""
        hb = make_heartbeat(
            node_id="studio",
            memory_total=128.0,
            memory_used=40.0,
            loaded_models=[("phi4:14b", 9.0, 131072)],
        )
        await registry.update_from_heartbeat(hb)

        results = scorer.score_request("phi4:14b", {})
        assert results[0].scores_breakdown["context_fit"] == 0.0

    async def test_context_fit_neutral_for_cold_model(self, scorer, registry):
        """Model on disk (not loaded) has no context_length — score should be 0."""
        hb = make_heartbeat(
            node_id="studio",
            memory_total=192.0,
            memory_used=20.0,
            available_models=["llama3.3:70b"],
        )
        await registry.update_from_heartbeat(hb)

        results = scorer.score_request("llama3.3:70b", {}, estimated_tokens=5000)
        assert results[0].scores_breakdown["context_fit"] == 0.0

    async def test_context_fit_routes_long_request_to_big_context(self, scorer, registry):
        """Long conversation should route to the node with the larger context window."""
        # Both nodes identical except context length
        hb_a = make_heartbeat(
            node_id="laptop",
            memory_total=64.0,
            memory_used=20.0,
            loaded_models=[("llama3.3:70b", 40.0, 8192)],
        )
        hb_b = make_heartbeat(
            node_id="studio",
            memory_total=64.0,
            memory_used=20.0,
            loaded_models=[("llama3.3:70b", 40.0, 131072)],
        )
        await registry.update_from_heartbeat(hb_a)
        await registry.update_from_heartbeat(hb_b)

        # Short request — both are fine, context_fit difference is small
        results_short = scorer.score_request("llama3.3:70b", {}, estimated_tokens=100)
        # Both have massive headroom relative to 100 tokens
        assert all(r.scores_breakdown["context_fit"] > 0 for r in results_short)

        # Long request (~50K tokens) — laptop can't handle, studio can
        results_long = scorer.score_request("llama3.3:70b", {}, estimated_tokens=50000)
        scores = {r.node_id: r.scores_breakdown["context_fit"] for r in results_long}
        assert scores["studio"] > 0  # 131K > 50K — comfortable
        assert scores["laptop"] < 0  # 8K < 50K — overflow penalty

    async def test_estimate_tokens(self):
        """Token estimation produces reasonable results."""
        # Simple message
        messages = [{"role": "user", "content": "Hello world"}]
        tokens = ScoringEngine.estimate_tokens(messages)
        assert 1 <= tokens <= 10  # "Hello world" = ~3 tokens + overhead

        # Long message (~4000 chars = ~1000 tokens)
        long_msg = [{"role": "user", "content": "x" * 4000}]
        tokens = ScoringEngine.estimate_tokens(long_msg)
        assert 900 <= tokens <= 1100

        # Empty messages
        tokens = ScoringEngine.estimate_tokens([])
        assert tokens >= 1

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


@pytest.mark.asyncio
class TestScoreLoadedModels:
    async def test_same_category_filter(self, scorer, registry):
        """Only return loaded models matching the requested category."""
        # Node with a coding model loaded
        hb = make_heartbeat(
            node_id="studio",
            memory_total=512.0,
            memory_used=200.0,
            loaded_models=[("qwen2.5-coder:32b", 20.0), ("llama3.3:70b", 40.0)],
        )
        await registry.update_from_heartbeat(hb)

        # Request coding category — should only return the coder model
        results = scorer.score_loaded_models("coding", {})
        assert len(results) == 1
        assert results[0][1] == "qwen2.5-coder:32b"

    async def test_any_category(self, scorer, registry):
        """With category=None, return all loaded models."""
        hb = make_heartbeat(
            node_id="studio",
            memory_total=512.0,
            memory_used=200.0,
            loaded_models=[("qwen2.5-coder:32b", 20.0), ("llama3.3:70b", 40.0)],
        )
        await registry.update_from_heartbeat(hb)

        results = scorer.score_loaded_models(None, {})
        model_names = {r[1] for r in results}
        assert "qwen2.5-coder:32b" in model_names
        assert "llama3.3:70b" in model_names

    async def test_exclude_models(self, scorer, registry):
        """Excluded models are filtered out."""
        hb = make_heartbeat(
            node_id="studio",
            memory_total=512.0,
            memory_used=200.0,
            loaded_models=[("qwen2.5-coder:32b", 20.0), ("llama3.3:70b", 40.0)],
        )
        await registry.update_from_heartbeat(hb)

        results = scorer.score_loaded_models(
            None, {}, exclude_models=["llama3.3:70b"],
        )
        model_names = {r[1] for r in results}
        assert "llama3.3:70b" not in model_names
        assert "qwen2.5-coder:32b" in model_names

    async def test_offline_nodes_excluded(self, scorer, registry):
        """Offline nodes are not scored."""
        hb = make_heartbeat(
            node_id="offline-node",
            loaded_models=[("phi4:14b", 9.0)],
        )
        await registry.update_from_heartbeat(hb)
        registry.handle_drain("offline-node")

        results = scorer.score_loaded_models(None, {})
        assert results == []

    async def test_sorted_by_score_descending(self, scorer, registry):
        """Results are sorted by score descending."""
        # Two nodes with different memory headroom
        hb_a = make_heartbeat(
            node_id="big",
            memory_total=512.0,
            memory_used=100.0,
            loaded_models=[("phi4:14b", 9.0)],
        )
        hb_b = make_heartbeat(
            node_id="small",
            memory_total=32.0,
            memory_used=10.0,
            loaded_models=[("phi4:14b", 9.0)],
        )
        await registry.update_from_heartbeat(hb_a)
        await registry.update_from_heartbeat(hb_b)

        results = scorer.score_loaded_models(None, {})
        assert len(results) == 2
        assert results[0][0].score >= results[1][0].score
