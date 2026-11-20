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


@pytest.mark.asyncio
class TestJoiningALoad:
    """Requests for a model herd has already sent to a node to load go there
    too.  Each one scored that load as a fresh eviction, so concurrent requests
    for a model with no home started loads on every node at once."""

    async def test_a_model_on_its_way_to_a_node_is_charged_nothing_more(self, scorer, registry):
        await registry.update_from_heartbeat(_gpu_heartbeat("n1", loaded=[B]))
        await registry.update_from_heartbeat(_gpu_heartbeat("n2", loaded=[A]))
        busy = {f"n1:{B}": 2, f"n2:{A}": 2}
        before = {
            r.node_id: r.scores_breakdown["eviction"]
            for r in scorer.score_request("qwen3-coder:30b", busy)
        }
        assert before["n1"] < 0 and before["n2"] < 0
        busy["n1:qwen3-coder:30b"] = 1  # one coder request already sent to n1
        results = scorer.score_request("qwen3-coder:30b", busy)
        assert results[0].node_id == "n1"
        b = results[0].scores_breakdown
        assert b["eviction"] == before["n1"], "the eviction stays on record for the queue"
        assert b["joins_load"] == -b["eviction"]


@pytest.mark.asyncio
async def test_memory_held_by_a_model_still_loading_counts(scorer, registry):
    """Mid-swap, /api/ps lists nothing while the card is full: that is a load
    in progress, not free room, and must not earn the warm bonus."""
    await registry.update_from_heartbeat(_gpu_heartbeat("n1", loaded=[A]))
    hb = _gpu_heartbeat("n1")  # A unloaded, B loading: nothing listed
    hb.memory.gpu_source = "detected"
    hb.memory.gpu_used_gb = 17.5
    await registry.update_from_heartbeat(hb)
    await registry.update_from_heartbeat(_gpu_heartbeat("n2", loaded=["qwen3-coder:30b"]))
    by_node = {r.node_id: r.scores_breakdown for r in scorer.score_request(A, {})}
    s = ServerSettings()
    assert by_node["n1"]["eviction"] == -(s.score_model_hot - s.score_model_cold)
    assert by_node["n1"]["thermal"] == s.score_model_cold  # not warm


@pytest.mark.asyncio
class TestStrandedModels:
    """Run 4 (2026-11-20): two 24 GB cards and one 48 GB card, four models.
    Herd sent phi4 to the 48 GB card, evicting the 35 GB model nothing else
    could hold, and the big card swapped the two for the whole run."""

    BIG = "qwen2.5:32b-instruct-q8_0"

    def _hb(self, node_id, loaded, gpu_gb, has_big):
        sizes = {**SIZES, "qwen3-coder:30b": 18.6, self.BIG: 35.2}
        available = [A, B, "qwen3-coder:30b"] + ([self.BIG] if has_big else [])
        hb = make_heartbeat(
            node_id=node_id,
            memory_total=64.0,
            memory_used=12.0,
            loaded_models=[(m, sizes[m]) for m in loaded],
            available_models=available,
        )
        hb.ollama.models_available_sizes = {m: sizes[m] for m in available}
        for m in hb.ollama.models_loaded:
            m.size_vram_gb = m.size_gb
        hb.memory.gpu_total_gb = gpu_gb
        return hb

    async def _fleet(self, registry, big_elsewhere=False):
        await registry.update_from_heartbeat(
            self._hb("small-1", ["qwen3-coder:30b"], 22.5, big_elsewhere)
        )
        await registry.update_from_heartbeat(self._hb("small-2", [A], 22.5, big_elsewhere))
        await registry.update_from_heartbeat(self._hb("big", [self.BIG], 45.0, True))

    async def test_a_model_only_one_card_can_hold_is_not_evicted(self, scorer, registry):
        await self._fleet(registry)
        busy = {"small-1:qwen3-coder:30b": 2, f"small-2:{A}": 2, f"big:{self.BIG}": 2}
        results = scorer.score_request(B, busy)
        assert "big" not in {r.node_id for r in results}, "never a candidate while others are"

    async def test_no_signal_outweighs_stranding(self, scorer, registry):
        """The small cards' queues are deep and the big card's is empty: still
        not the big card.  A charge alone lost to two queue penalties."""
        await self._fleet(registry)
        busy = {"small-1:qwen3-coder:30b": 8, f"small-2:{A}": 8, f"big:{self.BIG}": 0}
        assert {r.node_id for r in scorer.score_request(B, busy)} == {"small-1", "small-2"}

    async def test_stranding_is_allowed_when_it_is_the_only_choice(self, scorer, registry):
        """The small cards are up but do not have B on disk."""
        await self._fleet(registry)
        for n in ("small-1", "small-2"):
            registry.get_node(n).ollama.models_available.remove(B)
        (only,) = scorer.score_request(B, {f"big:{self.BIG}": 2})
        assert only.node_id == "big"
        assert only.scores_breakdown["eviction"] == -ServerSettings().score_model_hot

    async def test_stranding_only_counts_when_no_other_node_can_hold_it(self, scorer, registry):
        """Give a small card room for the big model's size: then it has another home."""
        await self._fleet(registry, big_elsewhere=True)
        for n in ("small-1", "small-2"):
            registry.get_node(n).memory.gpu_total_gb = 40.0
        busy = {"small-1:qwen3-coder:30b": 2, f"small-2:{A}": 2, f"big:{self.BIG}": 2}
        by_node = {r.node_id: r.scores_breakdown["eviction"] for r in scorer.score_request(B, busy)}
        s = ServerSettings()
        assert by_node["big"] >= -(s.score_model_hot - s.score_model_cold)
