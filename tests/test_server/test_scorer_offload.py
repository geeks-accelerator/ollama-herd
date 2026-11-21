"""A model Ollama put partly on the CPU scores as cold, not hot.

On the 2026-11-21 vision checks moondream loaded 59% CPU / 41% GPU on an A10
beside qwen3-coder:30b.  The scorer still counted it hot, so an empty queue
there (88) beat the A100 that served it fully on the GPU with one request in
flight (82): image work moved to a card where it took seconds and slowed the
coder.  See docs/plans/fleet-benchmark-findings-2026-11.md, item 32.
"""

from __future__ import annotations

import pytest

from fleet_manager.models.config import ServerSettings
from fleet_manager.models.node import LoadedModel
from fleet_manager.server.registry import NodeRegistry
from fleet_manager.server.scorer import ScoringEngine
from tests.conftest import make_heartbeat

MODEL = "moondream:latest"


def _hb(node_id, vram_gb):
    hb = make_heartbeat(node_id=node_id, loaded_models=[(MODEL, 2.6)], available_models=[MODEL])
    hb.ollama.models_loaded[0].size_vram_gb = vram_gb
    return hb


@pytest.fixture
def scorer():
    s = ServerSettings()
    return ScoringEngine(s, NodeRegistry(s)), s


class TestOffloaded:
    @pytest.mark.parametrize(
        ("vram", "offloaded"),
        [(1.1, True), (2.6, False), (2.4, False), (0.0, False)],
    )
    def test_offloaded_is_part_on_the_gpu(self, vram, offloaded):
        """0 is unreported (an older agent, or a machine with no GPU)."""
        m = LoadedModel(name=MODEL, size_gb=2.6, size_vram_gb=vram)
        assert m.offloaded is offloaded

    @pytest.mark.asyncio
    async def test_scores_as_cold(self, scorer):
        eng, s = scorer
        await eng._registry.update_from_heartbeat(_hb("a10", 1.1))
        node = eng._registry.get_node("a10")
        assert eng._score_thermal(node, MODEL) == s.score_model_cold

    @pytest.mark.asyncio
    async def test_a_fully_resident_copy_wins_with_a_request_in_flight(self, scorer):
        eng, _ = scorer
        await eng._registry.update_from_heartbeat(_hb("a100", 2.6))
        await eng._registry.update_from_heartbeat(_hb("a10", 1.1))
        ranked = eng.score_request(MODEL, {f"a100:{MODEL}": 1})
        assert ranked[0].node_id == "a100"

    @pytest.mark.asyncio
    async def test_unreported_gpu_share_stays_hot(self, scorer):
        """Older agents and CPU-only machines keep today's behaviour."""
        eng, s = scorer
        await eng._registry.update_from_heartbeat(_hb("old", 0.0))
        node = eng._registry.get_node("old")
        assert eng._score_thermal(node, MODEL) == s.score_model_hot
