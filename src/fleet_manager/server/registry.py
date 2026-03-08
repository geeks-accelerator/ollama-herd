"""Node Registry — in-memory tracking of all fleet nodes."""

from __future__ import annotations

import asyncio
import logging
import time

from fleet_manager.models.config import ServerSettings
from fleet_manager.models.node import (
    CpuMetrics,
    HardwareProfile,
    HeartbeatPayload,
    MemoryMetrics,
    NodeState,
    NodeStatus,
    OllamaMetrics,
)

logger = logging.getLogger(__name__)


class NodeRegistry:
    def __init__(self, settings: ServerSettings):
        self._settings = settings
        self._nodes: dict[str, NodeState] = {}
        self._lock = asyncio.Lock()

    async def update_from_heartbeat(self, payload: HeartbeatPayload, request_ip: str = "") -> NodeState:
        """Process an incoming heartbeat. Creates or updates node state."""
        async with self._lock:
            if payload.node_id not in self._nodes:
                # Construct ollama URL from the node's LAN IP
                ollama_url = self._build_ollama_url(payload, request_ip)
                node = NodeState(
                    node_id=payload.node_id,
                    hardware=HardwareProfile(
                        node_id=payload.node_id,
                        memory_total_gb=payload.memory.total_gb,
                        cores_physical=payload.cpu.cores_physical,
                        ollama_host=payload.ollama_host,
                    ),
                    ollama_base_url=ollama_url,
                )
                self._nodes[payload.node_id] = node
                logger.info(
                    f"New node registered: {payload.node_id} "
                    f"({payload.memory.total_gb:.0f}GB, {payload.cpu.cores_physical} cores, "
                    f"ollama={ollama_url})"
                )

            node = self._nodes[payload.node_id]

            # Track model unload times for warm-tier scoring
            if node.ollama and payload.ollama:
                prev_loaded = {m.name for m in node.ollama.models_loaded}
                curr_loaded = {m.name for m in payload.ollama.models_loaded}
                for unloaded in prev_loaded - curr_loaded:
                    node.model_unloaded_at[unloaded] = time.time()

            node.cpu = payload.cpu
            node.memory = payload.memory
            node.ollama = payload.ollama
            node.last_heartbeat = time.time()
            node.missed_heartbeats = 0
            node.status = NodeStatus.ONLINE

            # Update ollama URL if LAN IP changed
            new_url = self._build_ollama_url(payload, request_ip)
            if new_url != node.ollama_base_url:
                node.ollama_base_url = new_url

            return node

    def handle_drain(self, node_id: str):
        """Mark a node as draining (going offline gracefully)."""
        if node_id in self._nodes:
            self._nodes[node_id].status = NodeStatus.OFFLINE
            logger.info(f"Node {node_id} is draining, marked OFFLINE")

    def get_node(self, node_id: str) -> NodeState | None:
        return self._nodes.get(node_id)

    def get_online_nodes(self) -> list[NodeState]:
        return [n for n in self._nodes.values() if n.status != NodeStatus.OFFLINE]

    def get_all_nodes(self) -> list[NodeState]:
        return list(self._nodes.values())

    def get_nodes_with_model(self, model: str) -> list[NodeState]:
        result = []
        for node in self._nodes.values():
            if node.status == NodeStatus.OFFLINE or node.ollama is None:
                continue
            loaded_names = [m.name for m in node.ollama.models_loaded]
            if model in loaded_names or model in node.ollama.models_available:
                result.append(node)
        return result

    async def monitor_heartbeats(self):
        """Background task: check for stale heartbeats."""
        while True:
            await asyncio.sleep(self._settings.heartbeat_interval)
            now = time.time()
            async with self._lock:
                for node in self._nodes.values():
                    if node.status == NodeStatus.OFFLINE:
                        continue
                    elapsed = now - node.last_heartbeat
                    if elapsed > self._settings.heartbeat_offline:
                        if node.status != NodeStatus.OFFLINE:
                            node.status = NodeStatus.OFFLINE
                            node.missed_heartbeats += 1
                            logger.warning(f"Node {node.node_id} marked OFFLINE (no heartbeat for {elapsed:.0f}s)")
                    elif elapsed > self._settings.heartbeat_timeout:
                        if node.status != NodeStatus.DEGRADED:
                            node.status = NodeStatus.DEGRADED
                            node.missed_heartbeats += 1
                            logger.info(f"Node {node.node_id} marked DEGRADED (no heartbeat for {elapsed:.0f}s)")

    def _build_ollama_url(self, payload: HeartbeatPayload, request_ip: str = "") -> str:
        """Build the network-reachable Ollama URL from the heartbeat.

        If the node is on the same machine as the router (heartbeat from localhost),
        use the node's configured ollama_host directly (typically localhost:11434).
        For remote nodes, construct URL from the node's LAN IP.
        """
        from urllib.parse import urlparse

        # If heartbeat came from localhost, the node is on this machine
        is_local = request_ip in ("127.0.0.1", "::1", "") or request_ip == payload.lan_ip
        if is_local:
            # Check if ollama_host points to localhost — use it directly
            parsed = urlparse(payload.ollama_host)
            if parsed.hostname in ("localhost", "127.0.0.1", "::1"):
                return payload.ollama_host

        if payload.lan_ip and payload.lan_ip != "127.0.0.1":
            parsed = urlparse(payload.ollama_host)
            port = parsed.port or 11434
            return f"http://{payload.lan_ip}:{port}"
        return payload.ollama_host
