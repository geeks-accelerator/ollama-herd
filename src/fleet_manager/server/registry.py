"""Node Registry — in-memory tracking of all fleet nodes."""

from __future__ import annotations

import asyncio
import logging
import time

from fleet_manager.common.system_metrics import get_local_ip
from fleet_manager.models.config import ServerSettings
from fleet_manager.models.node import (
    HardwareProfile,
    HeartbeatPayload,
    NodeState,
    NodeStatus,
)

logger = logging.getLogger(__name__)

_LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")


class NodeRegistry:
    def __init__(self, settings: ServerSettings):
        self._settings = settings
        self._nodes: dict[str, NodeState] = {}
        self._lock = asyncio.Lock()
        # Optional async callback invoked on an online -> offline transition.
        # A callback rather than a direct import keeps the registry unaware of
        # browsers and notifications; app.py wires the alerter in.
        self.on_node_offline = None

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
                        arch=payload.arch or "unknown",
                        chip=payload.chip,
                        memory_total_gb=payload.memory.total_gb,
                        cores_physical=payload.cpu.cores_physical,
                        memory_bandwidth_gbps=payload.memory_bandwidth_gbps,
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
            node.thermal = payload.thermal
            node.disk = payload.disk
            # Tag metadata rides only on heartbeats where it changed (see
            # collector._meta_to_send); None means "unchanged", so keep ours.
            if (
                payload.ollama is not None
                and payload.ollama.models_available_meta is None
                and node.ollama is not None
            ):
                payload.ollama.models_available_meta = node.ollama.models_available_meta
            node.ollama = payload.ollama
            node.capacity = payload.capacity
            node.agent_version = payload.agent_version
            node.process_memory = payload.process_memory
            # Copy explicitly: this method assigns field by field, so a new
            # heartbeat field is invisible to the router until it is listed
            # here.  mlx_version shipped empty in the telemetry device row for
            # exactly this reason before it was added.
            node.mlx_version = payload.mlx_version
            node.image = payload.image
            node.image_port = payload.image_port
            node.transcription = payload.transcription
            node.transcription_port = payload.transcription_port
            node.vision_embedding = payload.vision_embedding
            node.vision_embedding_port = payload.vision_embedding_port
            node.vision_embedding_status = dict(payload.vision_embedding_status or {})
            node.text_embedding = payload.text_embedding
            node.text_embedding_port = payload.text_embedding_port
            node.text_embedding_status = dict(payload.text_embedding_status or {})
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

            # Rebuilt from every heartbeat: the node's address, or its
            # ollama_host, may have changed since it registered.
            new_url = self._build_ollama_url(payload, request_ip)
            if new_url != node.ollama_base_url:
                node.ollama_base_url = new_url

            return node

    def handle_drain(self, node_id: str):
        """Mark a node as draining (going offline gracefully).

        Deliberately does NOT fire ``on_node_offline``.  A drain means the
        node announced its own shutdown, so somebody already knows -- waking
        an operator for it is the kind of noise that trains people to ignore
        the alert that matters.  Same reasoning as the launchd agents'
        ``KeepAlive{SuccessfulExit:false}``: a clean exit stays stopped.
        Only the stale-heartbeat path in ``monitor_heartbeats`` alerts,
        because that is the one nobody asked for.
        """
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
                # The node's ollama_base_url hostname: already resolved
                # LAN-reachably by _build_ollama_url() on every heartbeat.
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

    async def _fire_offline_callback(self, node_id: str) -> None:
        """Run ``on_node_offline`` without letting it break the monitor."""
        try:
            await self.on_node_offline(node_id)
        except Exception as e:
            logger.warning(f"on_node_offline callback failed for {node_id}: {e!r}")

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
                            # Fire-and-forget: this loop holds ``self._lock``
                            # and must keep checking every other node, so the
                            # alert must not block it or propagate failures.
                            if self.on_node_offline is not None:
                                asyncio.create_task(self._fire_offline_callback(node.node_id))
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

        If the node is on the same machine as the router, use localhost directly
        -- more reliable than going through the LAN IP since it avoids network
        stack issues (firewall, IP changes, macOS quirks).  For remote nodes the
        order is: an explicitly-configured non-loopback ollama_host, then the IP
        the heartbeat arrived from (reachable by construction), then the
        self-reported lan_ip as a last resort.
        """
        from urllib.parse import urlparse

        parsed = urlparse(payload.ollama_host)
        if self._node_is_on_this_machine(payload, request_ip):
            # The collector rewrites a loopback ollama_host to its LAN address
            # before sending (collector._make_lan_reachable_url), so a host equal
            # to lan_ip means "my own Ollama": use localhost.  A host naming some
            # other machine is a deliberate FLEET_NODE_OLLAMA_HOST -- a node on
            # this box driving a remote Ollama -- and is honoured.
            host = parsed.hostname or ""
            if host and host not in _LOOPBACK_HOSTS and host != payload.lan_ip:
                return payload.ollama_host
            return f"http://localhost:{parsed.port or 11434}"

        # An operator who set a non-loopback ollama_host knows their topology
        # better than we can infer it.  The node's collector only rewrites
        # ollama_host when it points at loopback, so a non-loopback value here
        # is either a deliberate FLEET_NODE_OLLAMA_HOST or an address the node
        # already believes is reachable.  Respect it.
        #
        # Discarding it was a real outage for Docker deployments (issue #1):
        # inside a container lan_ip is the bridge address (172.17.0.x), which
        # the router cannot reach, so every routed request failed with
        # ConnectError even though FLEET_NODE_OLLAMA_HOST was set correctly.
        if parsed.hostname and parsed.hostname not in _LOOPBACK_HOSTS:
            return payload.ollama_host

        port = parsed.port or 11434

        # Otherwise prefer the address the heartbeat actually arrived from.
        # It is reachable by construction -- the packet got here -- whereas
        # lan_ip is self-reported and wrong inside any NAT'd network namespace.
        if request_ip and request_ip not in _LOOPBACK_HOSTS:
            return f"http://{request_ip}:{port}"

        if payload.lan_ip and payload.lan_ip != "127.0.0.1":
            return f"http://{payload.lan_ip}:{port}"
        return payload.ollama_host

    @staticmethod
    def _node_is_on_this_machine(payload: HeartbeatPayload, request_ip: str) -> bool:
        """Did this heartbeat come from a node running on the router's machine?

        Arriving over loopback is necessary but not sufficient.  Anything on
        this machine that forwards the heartbeat makes a remote node look local
        -- an SSH tunnel (``ssh -L 11435:...``), or nginx's default
        ``proxy_pass``, which sets no ``X-Forwarded-For`` for uvicorn to
        recover the client from.  Treating those as local routed every remote
        node's traffic to *this* machine's Ollama: served by the wrong box when
        it had the model, a 404 when it did not.  The loopback test alone was
        the whole check until 2026-11-17 (docs/plans/backend-model-not-found.md).

        So also require the node's own LAN address to be ours.  The node fills
        ``lan_ip`` with ``get_local_ip()``; the same function on the same
        machine, over the same routing table, gives the same answer, which is
        why this needs no interface enumeration.  An empty or loopback
        ``lan_ip`` (a node with no network) can only be local.
        """
        if request_ip not in _LOOPBACK_HOSTS and request_ip != "":
            return False
        lan_ip = payload.lan_ip or ""
        if not lan_ip or lan_ip in _LOOPBACK_HOSTS:
            return True
        return lan_ip == get_local_ip(warn=False)
