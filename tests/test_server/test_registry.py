"""Tests for the NodeRegistry."""

from __future__ import annotations

import time

import pytest

from fleet_manager.models.node import (
    LoadedModel,
    MemoryPressure,
    NodeStatus,
    OllamaMetrics,
)
from fleet_manager.server.registry import NodeRegistry

from tests.conftest import make_heartbeat, make_node


@pytest.mark.asyncio
class TestNodeRegistry:
    async def test_register_new_node(self, registry):
        hb = make_heartbeat(node_id="studio-1")
        node = await registry.update_from_heartbeat(hb)
        assert node.node_id == "studio-1"
        assert node.status == NodeStatus.ONLINE
        assert node.hardware.memory_total_gb == 64.0
        assert node.hardware.cores_physical == 12

    async def test_update_existing_node(self, registry):
        hb1 = make_heartbeat(node_id="studio-1", cpu_pct=10.0)
        await registry.update_from_heartbeat(hb1)

        hb2 = make_heartbeat(node_id="studio-1", cpu_pct=80.0)
        node = await registry.update_from_heartbeat(hb2)
        assert node.cpu.utilization_pct == 80.0

    async def test_get_all_nodes(self, registry):
        await registry.update_from_heartbeat(make_heartbeat(node_id="a"))
        await registry.update_from_heartbeat(make_heartbeat(node_id="b"))
        assert len(registry.get_all_nodes()) == 2

    async def test_get_online_nodes_excludes_offline(self, registry):
        await registry.update_from_heartbeat(make_heartbeat(node_id="a"))
        await registry.update_from_heartbeat(make_heartbeat(node_id="b"))
        registry.handle_drain("b")
        online = registry.get_online_nodes()
        assert len(online) == 1
        assert online[0].node_id == "a"

    async def test_get_node_by_id(self, registry):
        await registry.update_from_heartbeat(make_heartbeat(node_id="studio"))
        assert registry.get_node("studio") is not None
        assert registry.get_node("nonexistent") is None

    async def test_get_nodes_with_model(self, registry):
        hb1 = make_heartbeat(
            node_id="a",
            loaded_models=[("llama3.3:70b", 40.0)],
            available_models=["phi4:14b"],
        )
        hb2 = make_heartbeat(
            node_id="b",
            loaded_models=[("phi4:14b", 9.0)],
        )
        await registry.update_from_heartbeat(hb1)
        await registry.update_from_heartbeat(hb2)

        # Both have phi4 (a has it available, b has it loaded)
        nodes_phi = registry.get_nodes_with_model("phi4:14b")
        assert len(nodes_phi) == 2

        # Only a has llama3.3
        nodes_llama = registry.get_nodes_with_model("llama3.3:70b")
        assert len(nodes_llama) == 1
        assert nodes_llama[0].node_id == "a"

    async def test_handle_drain(self, registry):
        await registry.update_from_heartbeat(make_heartbeat(node_id="draining"))
        registry.handle_drain("draining")
        node = registry.get_node("draining")
        assert node.status == NodeStatus.OFFLINE

    async def test_model_unload_tracking(self, registry):
        # First heartbeat: model loaded
        hb1 = make_heartbeat(
            node_id="studio",
            loaded_models=[("llama3.3:70b", 40.0), ("phi4:14b", 9.0)],
        )
        await registry.update_from_heartbeat(hb1)

        # Second heartbeat: llama3.3 no longer loaded
        hb2 = make_heartbeat(
            node_id="studio",
            loaded_models=[("phi4:14b", 9.0)],
        )
        node = await registry.update_from_heartbeat(hb2)

        assert "llama3.3:70b" in node.model_unloaded_at
        assert "phi4:14b" not in node.model_unloaded_at

    async def test_ollama_url_local_node(self, registry):
        hb = make_heartbeat(
            node_id="local",
            lan_ip="192.168.1.100",
            ollama_host="http://localhost:11434",
        )
        node = await registry.update_from_heartbeat(hb, request_ip="127.0.0.1")
        assert "localhost" in node.ollama_base_url

    async def test_ollama_url_remote_node(self, registry):
        hb = make_heartbeat(
            node_id="remote",
            lan_ip="192.168.1.200",
            ollama_host="http://localhost:11434",
        )
        node = await registry.update_from_heartbeat(hb, request_ip="192.168.1.200")
        # Remote node should get URL rewritten to LAN IP, not localhost
        assert node.ollama_base_url == "http://192.168.1.200:11434"

    async def test_heartbeat_resets_status(self, registry):
        hb = make_heartbeat(node_id="studio")
        await registry.update_from_heartbeat(hb)
        node = registry.get_node("studio")
        node.status = NodeStatus.DEGRADED
        node.missed_heartbeats = 2

        await registry.update_from_heartbeat(hb)
        node = registry.get_node("studio")
        assert node.status == NodeStatus.ONLINE
        assert node.missed_heartbeats == 0

    async def test_agent_version_stored(self, registry):
        hb = make_heartbeat(node_id="versioned", agent_version="0.1.0")
        node = await registry.update_from_heartbeat(hb)
        assert node.agent_version == "0.1.0"

    async def test_agent_version_defaults_empty(self, registry):
        hb = make_heartbeat(node_id="no-version")
        node = await registry.update_from_heartbeat(hb)
        assert node.agent_version == ""
