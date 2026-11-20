"""Placement that knows what a cold load would evict.

Two cases from the 2026-11-20 fleet benchmark, on 24 GB GPUs with ~52 GB of
free RAM, where Ollama evicts a resident model rather than spill a new one that
would fit after the eviction into RAM:

- **Alternation.** Two 15.7 GB models that cannot share one card, two nodes.
  Every request went to the node that had just evicted the requested model
  (warm +30 beat an empty node's cold +10, and free RAM said both "fit"), so
  the two models swapped on every request while the second node sat empty.
- **Mixed load.** A request for a small chat model evicted a busy coding model
  on its node although another node had the chat model loaded.
"""

from __future__ import annotations

import pytest

from fleet_manager.models.config import ServerSettings
from fleet_manager.server.registry import NodeRegistry
from fleet_manager.server.scorer import ScoringEngine
from tests.conftest import make_heartbeat

A = "qwen2.5:14b-instruct-q8_0"
B = "phi4:14b-q8_0"
SIZES = {A: 15.7, B: 15.6}


def _gpu_heartbeat(node_id, loaded=(), gpu_gb=22.5, ram_total=64.0, ram_used=12.0):
    hb = make_heartbeat(
        node_id=node_id,
        memory_total=ram_total,
        memory_used=ram_used,
        loaded_models=[(m, SIZES.get(m, 18.0)) for m in loaded],
        available_models=[A, B, "qwen3-coder:30b", "qwen3.5:9b"],
    )
    hb.ollama.models_available_sizes = {**SIZES, "qwen3-coder:30b": 18.6, "qwen3.5:9b": 6.6}
    for m in hb.ollama.models_loaded:
        m.size_vram_gb = m.size_gb
    hb.memory.gpu_total_gb = gpu_gb
    return hb


@pytest.fixture
def registry():
    return NodeRegistry(ServerSettings())


@pytest.fixture
def scorer(registry):
    return ScoringEngine(ServerSettings(), registry)


@pytest.mark.asyncio
class TestAlternation:
    async def _fleet(self, registry):
        # node-1 had A, then evicted it for B: A is "recently unloaded" there.
        await registry.update_from_heartbeat(_gpu_heartbeat("node-1", loaded=[A]))
        await registry.update_from_heartbeat(_gpu_heartbeat("node-1", loaded=[B]))
        await registry.update_from_heartbeat(_gpu_heartbeat("node-2"))
        assert A in registry.get_node("node-1").model_unloaded_at

    async def test_the_empty_node_wins(self, scorer, registry):
        await self._fleet(registry)
        results = scorer.score_request(A, {})
        assert results[0].node_id == "node-2"

    async def test_warm_is_not_awarded_when_loading_back_evicts(self, scorer, registry):
        await self._fleet(registry)
        by_node = {r.node_id: r.scores_breakdown for r in scorer.score_request(A, {})}
        assert by_node["node-1"]["thermal"] == ServerSettings().score_model_cold
        assert by_node["node-1"]["eviction"] < 0
        assert by_node["node-2"]["eviction"] == 0

    async def test_memory_fit_counts_the_card_not_the_ram(self, scorer, registry):
        await self._fleet(registry)
        by_node = {r.node_id: r.scores_breakdown for r in scorer.score_request(A, {})}
        assert by_node["node-1"]["memory_fit"] == 0  # 6.9 GB free on the card
        assert by_node["node-2"]["memory_fit"] > 0


@pytest.mark.asyncio
class TestBusyModelsAreExpensiveToEvict:
    async def test_a_busy_model_is_not_evicted_when_another_node_has_room(self, scorer, registry):
        await registry.update_from_heartbeat(_gpu_heartbeat("busy", loaded=["qwen3-coder:30b"]))
        await registry.update_from_heartbeat(_gpu_heartbeat("roomy"))
        depths = {"busy:qwen3-coder:30b": 4}
        results = scorer.score_request("qwen3.5:9b", depths)
        assert results[0].node_id == "roomy"
        busy = next(r for r in results if r.node_id == "busy")
        s = ServerSettings()
        assert busy.scores_breakdown["eviction"] == -(s.score_model_hot - s.score_model_cold)

    async def test_an_idle_model_costs_less_than_a_busy_one(self, scorer, registry):
        await registry.update_from_heartbeat(_gpu_heartbeat("n", loaded=["qwen3-coder:30b"]))
        idle = scorer.score_request("qwen3.5:9b", {})[0].scores_breakdown["eviction"]
        busy = scorer.score_request("qwen3.5:9b", {"n:qwen3-coder:30b": 2})[0].scores_breakdown[
            "eviction"
        ]
        assert busy < idle < 0


@pytest.mark.asyncio
class TestUnchanged:
    async def test_a_resident_model_has_no_eviction(self, scorer, registry):
        await registry.update_from_heartbeat(_gpu_heartbeat("n", loaded=[A]))
        assert scorer.score_request(A, {})[0].scores_breakdown["eviction"] == 0

    async def test_unified_memory_with_room_has_no_eviction(self, scorer, registry):
        """No GPU pool (Apple Silicon): available RAM is what a load can use."""
        hb = make_heartbeat(
            node_id="mac",
            memory_total=128.0,
            memory_used=40.0,
            loaded_models=[(B, 15.6)],
            available_models=[A, B],
        )
        hb.ollama.models_available_sizes = dict(SIZES)
        await registry.update_from_heartbeat(hb)
        assert scorer.score_request(A, {})[0].scores_breakdown["eviction"] == 0
