"""Node Registry — in-memory tracking of all fleet nodes."""

from __future__ import annotations

import asyncio
import logging
import time

from fleet_manager.models.config import ServerSettings
from fleet_manager.models.node import (
    HardwareProfile,
    HeartbeatPayload,
    NodeState,
    NodeStatus,
)

logger = logging.getLogger(__name__)


class NodeRegistry:
    def __init__(self, settings: ServerSettings):
        self._settings = settings
        self._nodes: dict[str, NodeState] = {}
        self._lock = asyncio.Lock()

    async def update_from_heartbeat(
        self, payload: HeartbeatPayload, request_ip: str = ""
    ) -> NodeState:
        """Process an incoming heartbeat. Creates or updates node state."""
        async with self._lock:
            if payload.node_id not in self._nodes:
                # Construct ollama URL from the node's LAN IP
                ollama_url = self._build_ollama_url(payload, request_ip)
                node = NodeState(
                    node_id=payload.node_id,
                    hardware=HardwareProfile(
                        node_id=payload.node_id,
                        arch=payload.arch or "apple_silicon",
                        chip=payload.chip,
                        memory_total_gb=payload.memory.total_gb,
                        cores_physical=payload.cpu.cores_physical,
                        memory_bandwidth_gbps=payload.memory_bandwidth_gbps,
                        ollama_host=payload.ollama_host,
                    ),
                    ollama_base_url=ollama_url,
                )
                self._nodes[payload.node_id] = node
                bw_str = (
                    f", {payload.memory_bandwidth_gbps:.0f}GB/s"
                    if payload.memory_bandwidth_gbps
                    else ""
                )
                chip_str = f" [{payload.chip}]" if payload.chip else ""
                logger.info(
                    f"New node registered: {payload.node_id}{chip_str} "
                    f"({payload.memory.total_gb:.0f}GB, "
                    f"{payload.cpu.cores_physical} cores{bw_str}, "
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
            node.disk = payload.disk
            node.ollama = payload.ollama
            node.capacity = payload.capacity
            node.agent_version = payload.agent_version
            node.image = payload.image
            node.image_port = payload.image_port
            node.transcription = payload.transcription
            node.transcription_port = payload.transcription_port
            node.vision_embedding = payload.vision_embedding
            node.vision_embedding_port = payload.vision_embedding_port
            node.connection_failures = payload.connection_failures
            node.connection_failures_total = payload.connection_failures_total
            # Multi-MLX per-server state: mirror heartbeat into node state so
            # routers and the dashboard have fresh per-server status without
            # re-polling mlx_lm.server directly.
            node.mlx_servers = list(payload.mlx_servers)
            node.mlx_bind_host = payload.mlx_bind_host or "127.0.0.1"
            node.last_heartbeat = time.time()
            # Keep the hardware profile in sync — lets a node that starts
            # before its chip detection completes (rare) pick up the numbers
            # on a later heartbeat without needing to restart.
            if payload.chip and node.hardware.chip != payload.chip:
                node.hardware.chip = payload.chip
            if (
                payload.memory_bandwidth_gbps
                and node.hardware.memory_bandwidth_gbps != payload.memory_bandwidth_gbps
            ):
                node.hardware.memory_bandwidth_gbps = payload.memory_bandwidth_gbps
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

    def resolve_mlx_url(self, mlx_model: str) -> str | None:
        """Return a LAN-reachable URL for an ``mlx:`` model, or None.

        Walks every online node's ``mlx_servers`` list and picks the first
        healthy entry hosting the requested model.  The URL is constructed
        from the node's ``lan_ip`` and the server's ``port`` — assumes the
        node's MLX supervisor bound to 0.0.0.0 (set
        ``FLEET_NODE_MLX_BIND_HOST=0.0.0.0`` for multi-node).

        ``mlx_model`` may be prefixed with ``mlx:`` or bare — both accepted.

        When the router and MLX are colocated (single-host fleet), the node's
        LAN IP and 127.0.0.1 are both fine; we always prefer the LAN IP so
        the same code path works locally and remotely.
        """
        bare = mlx_model.removeprefix("mlx:")
        for node in self._nodes.values():
            if node.status == NodeStatus.OFFLINE:
                continue
            for srv in node.mlx_servers:
                if srv.status != "healthy":
                    continue
                if srv.model != bare:
                    continue
                # Prefer node's ollama_base_url hostname since we already
                # resolved it LAN-reachably; fall back to hardware's
                # ollama_host.  Both were built by _build_ollama_url().
                from urllib.parse import urlparse
                parsed = urlparse(node.ollama_base_url)
                host = parsed.hostname or "127.0.0.1"
                return f"http://{host}:{srv.port}"
        return None

    def all_mlx_urls(self) -> dict[str, str]:
        """Return a ``{mlx:model: url}`` map for every healthy MLX server.

        Used by the dashboard to render the full MLX fleet and by the proxy
        client pool so it can pre-warm connections.  When multiple nodes
        host the same model, a later node's URL wins (arbitrary but stable
        within a heartbeat cycle).
        """
        from urllib.parse import urlparse

        out: dict[str, str] = {}
        for node in self._nodes.values():
            if node.status == NodeStatus.OFFLINE:
                continue
            parsed = urlparse(node.ollama_base_url)
            host = parsed.hostname or "127.0.0.1"
            for srv in node.mlx_servers:
                if srv.status != "healthy":
                    continue
                out[f"mlx:{srv.model}"] = f"http://{host}:{srv.port}"
        return out

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
                            logger.warning(
                                f"Node {node.node_id} marked OFFLINE "
                                f"(no heartbeat for {elapsed:.0f}s)"
                            )
                    elif (
                        elapsed > self._settings.heartbeat_timeout
                        and node.status != NodeStatus.DEGRADED
                    ):
                        node.status = NodeStatus.DEGRADED
                        node.missed_heartbeats += 1
                        logger.info(
                            f"Node {node.node_id} marked DEGRADED (no heartbeat for {elapsed:.0f}s)"
                        )

    def _build_ollama_url(self, payload: HeartbeatPayload, request_ip: str = "") -> str:
        """Build the network-reachable Ollama URL from the heartbeat.

        If the node is on the same machine as the router (heartbeat from localhost),
        use localhost directly — more reliable than going through the LAN IP since
        it avoids network stack issues (firewall, IP changes, macOS quirks).
        For remote nodes, construct URL from the node's LAN IP.
        """
        from urllib.parse import urlparse

        # If heartbeat came from loopback, the node is on this machine
        is_local = request_ip in ("127.0.0.1", "::1", "")
        if is_local:
            # Node on same machine — use localhost for reliability.
            # The collector may have rewritten ollama_host to the LAN IP,
            # but for co-located router+node, localhost is always reachable.
            parsed = urlparse(payload.ollama_host)
            port = parsed.port or 11434
            return f"http://localhost:{port}"

        if payload.lan_ip and payload.lan_ip != "127.0.0.1":
            parsed = urlparse(payload.ollama_host)
            port = parsed.port or 11434
            return f"http://{payload.lan_ip}:{port}"
        return payload.ollama_host
