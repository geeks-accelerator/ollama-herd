"""Resident traffic avoids a node in critical memory pressure while a healthy
node holds the same model (run 6: a swapping 24 GB Mac answered empty streams
for ten minutes while herd kept sending it work), but is never refused when
the critical node holds the only copy."""

from __future__ import annotations

from fleet_manager.models.config import ServerSettings
from fleet_manager.models.node import MemoryPressure
from fleet_manager.server.registry import NodeRegistry
from fleet_manager.server.scorer import ScoringEngine
from fleet_manager.testing.heartbeats import make_heartbeat

MODEL = "qwen2.5:7b"


async def _fleet(*nodes):
    settings = ServerSettings()
    registry = NodeRegistry(settings)
    for node_id, pressure, loaded in nodes:
        await registry.update_from_heartbeat(
            make_heartbeat(
                node_id=node_id,
                memory_total=24.0,
                memory_used=12.0,
                pressure=pressure,
                loaded_models=[(MODEL, 5.0)] if loaded else [],
                available_models=[MODEL],
            )
        )
    return ScoringEngine(settings, registry)


async def test_a_healthy_copy_takes_the_traffic():
    scorer = await _fleet(
        ("swapping", MemoryPressure.CRITICAL, True), ("fine", MemoryPressure.NORMAL, True)
    )
    # The critical node idle and the healthy one busy: still the healthy one.
    assert [r.node_id for r in scorer.score_request(MODEL, {f"fine:{MODEL}": 3})] == ["fine"]


async def test_the_only_copy_is_still_served():
    scorer = await _fleet(("swapping", MemoryPressure.CRITICAL, True))
    assert [r.node_id for r in scorer.score_request(MODEL, {})] == ["swapping"]


async def test_a_cold_node_is_not_an_alternative():
    """Loading the model elsewhere is not a healthy copy: both stay, scored as usual."""
    scorer = await _fleet(
        ("swapping", MemoryPressure.CRITICAL, True), ("cold", MemoryPressure.NORMAL, False)
    )
    assert {r.node_id for r in scorer.score_request(MODEL, {})} == {"swapping", "cold"}


async def test_auto_routing_avoids_it_too():
    scorer = await _fleet(
        ("swapping", MemoryPressure.CRITICAL, True), ("fine", MemoryPressure.NORMAL, True)
    )
    assert [r.node_id for r, _ in scorer.score_loaded_models(None, {})] == ["fine"]
