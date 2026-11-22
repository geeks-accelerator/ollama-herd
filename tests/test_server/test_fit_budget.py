"""``NodeState.fit_budget``: one budget for every fit decision, capped by
Metal's working set on Apple Silicon and by the capacity ceiling the same way
(B1 of docs/plans/review-and-run6-fixes-2026-11.md).

Run 6: a 24 GB M4 whose GPU may keep 17.8 GiB resident was judged by available
RAM, took a third model, swapped, and answered empty streams for ten minutes.
"""

from __future__ import annotations

import pytest

from fleet_manager.models.config import ServerSettings
from fleet_manager.models.node import CapacityMetrics
from fleet_manager.server import model_preloader
from fleet_manager.server.registry import NodeRegistry
from fleet_manager.testing.heartbeats import make_heartbeat


async def _mac(*, used=11.0, loaded=(("qwen2.5:14b", 13.0),), working_set=17.8, num_parallel=0):
    registry = NodeRegistry(ServerSettings())
    hb = make_heartbeat(
        node_id="s1",
        memory_total=24.0,
        memory_used=used,
        loaded_models=list(loaded),
        num_parallel=num_parallel,
    )
    hb.memory.gpu_working_set_gb = working_set
    await registry.update_from_heartbeat(hb)
    return registry.get_node("s1")


async def test_metal_caps_what_fits_without_evicting():
    node = await _mac()  # 13 GB loaded, 13 GB of RAM still available
    total, free = node.fit_budget()
    assert node.memory.available_gb == pytest.approx(13.0)
    assert free == pytest.approx(17.8 - 13.0), "RAM says 13 GB free; the GPU may keep 4.8 more"
    assert total == pytest.approx(17.8), "even evicting, never past the working set"


async def test_without_a_working_set_it_is_available_ram_as_before():
    node = await _mac(working_set=0.0)
    total, free = node.fit_budget()
    assert free == pytest.approx(13.0)
    assert total == pytest.approx(26.0)


async def test_the_ceiling_caps_the_total_so_free_is_what_is_left_under_it():
    node = await _mac(working_set=0.0)
    node.capacity = CapacityMetrics(mode="learned_medium", ceiling_gb=16.0)
    total, free = node.fit_budget()
    assert total == pytest.approx(16.0)
    assert free == pytest.approx(3.0), "16 GB ceiling less 13 GB already loaded"


async def test_a_model_bigger_than_the_working_set_has_no_home_there():
    node = await _mac()
    assert node.model_capacity_gb() == pytest.approx(17.8)
    node.memory.gpu_working_set_gb = 0.0
    assert node.model_capacity_gb() == pytest.approx(24.0)


async def test_the_preloader_does_not_load_what_the_gpu_cannot_keep(monkeypatch):
    """The preload gate is the budget before eviction: 13 GB of RAM free, but
    only 4.8 GB the GPU can still keep, so a 9 GB model is not preloaded."""
    from unittest.mock import AsyncMock, MagicMock

    node = await _mac()
    node.ollama.models_available = ["qwen2.5:14b", "gemma3:12b"]
    node.ollama.models_available_sizes = {"gemma3:12b": 8.1}
    proxy = MagicMock()
    proxy.pre_warm = AsyncMock()
    ok = await model_preloader._load_model_on_best_node(
        "gemma3:12b", [node], proxy, why="test", target_node_id="s1"
    )
    assert ok is False
    proxy.pre_warm.assert_not_awaited()


async def test_kv_cost_learned_on_four_slots_is_charged_per_slot():
    """``context_length`` is per slot and Ollama allocates every ``-np`` slot,
    so the KV a node reports is ``ctx x slots`` tokens.  Learned without the
    slots, a cost seen on a 4-slot node was charged four times over on a
    1-slot one."""
    model_preloader._kv_cost_mb_per_token.pop("qwen2.5:7b", None)
    four = await _mac(loaded=(), working_set=0.0, num_parallel=4)
    from fleet_manager.models.node import LoadedModel

    four.ollama.models_loaded = [
        LoadedModel(name="qwen2.5:7b", size_gb=4.7 + 4.0, context_length=8192)
    ]
    four.ollama.models_available_sizes = {"qwen2.5:7b": 4.7}
    model_preloader._observe_kv_cost(four)
    per_slot = model_preloader._kv_cost_mb_per_token["qwen2.5:7b"]
    assert per_slot == pytest.approx(4.0 * 1024 / (8192 * 4))

    one = await _mac(loaded=(), working_set=0.0, num_parallel=1)
    one.ollama.models_available_sizes = {"qwen2.5:7b": 4.7}
    cost = model_preloader.measured_resident_gb("qwen2.5:7b", one, 8192)
    assert cost == pytest.approx(4.7 + 1.0), "one slot: a quarter of the KV"
    model_preloader._kv_cost_mb_per_token.pop("qwen2.5:7b", None)
