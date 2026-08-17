"""Regression tests for issue #1 — Docker nodes unreachable, arch always Apple.

Reported by an external user running herd-node in containers on Linux/NVIDIA
hosts. Two independent bugs, both of which made the fleet unusable off Apple
Silicon, and both of which our own single-Mac fleet could never have surfaced.
"""

from __future__ import annotations

from fleet_manager.models.config import ServerSettings
from fleet_manager.models.node import (
    CpuMetrics,
    DiskMetrics,
    HeartbeatPayload,
    MemoryMetrics,
    OllamaMetrics,
)
from fleet_manager.server.registry import NodeRegistry


def _payload(ollama_host: str, lan_ip: str) -> HeartbeatPayload:
    return HeartbeatPayload(
        node_id="container-node",
        cpu=CpuMetrics(cores_physical=8, utilization_pct=10.0),
        memory=MemoryMetrics(total_gb=64.0, used_gb=8.0, available_gb=56.0),
        disk=DiskMetrics(total_gb=500.0, used_gb=100.0, available_gb=400.0),
        ollama=OllamaMetrics(),
        ollama_host=ollama_host,
        lan_ip=lan_ip,
    )


class TestExplicitOllamaHostIsRespected:
    """`FLEET_NODE_OLLAMA_HOST` was discarded: the router kept only the port and
    rebuilt the URL from lan_ip, which inside a container is the unreachable
    bridge address. Every routed request then failed with ConnectError."""

    def _registry(self):
        return NodeRegistry(ServerSettings())

    def test_container_bridge_ip_does_not_override_configured_host(self):
        url = self._registry()._build_ollama_url(
            _payload("http://192.168.221.15:11434", "172.17.0.4"),
            request_ip="192.168.221.15",
        )
        assert url == "http://192.168.221.15:11434", (
            "explicitly configured FLEET_NODE_OLLAMA_HOST must win over lan_ip"
        )
        assert "172.17." not in url

    def test_falls_back_to_the_ip_the_heartbeat_came_from(self):
        """Reachable by construction -- the packet arrived from there --
        whereas lan_ip is self-reported and wrong behind NAT."""
        url = self._registry()._build_ollama_url(
            _payload("http://localhost:11434", "172.17.0.4"),
            request_ip="192.168.221.15",
        )
        assert url == "http://192.168.221.15:11434"

    def test_non_default_port_survives_the_fallback(self):
        url = self._registry()._build_ollama_url(
            _payload("http://localhost:11500", "172.17.0.4"),
            request_ip="192.168.221.15",
        )
        assert url.endswith(":11500")

    def test_colocated_node_still_uses_localhost(self):
        """Unchanged behaviour for the common single-machine case."""
        url = self._registry()._build_ollama_url(
            _payload("http://localhost:11434", "10.0.0.5"), request_ip="127.0.0.1"
        )
        assert url == "http://localhost:11434"


class TestArchIsDetectedNotAssumed:
    """Every node reported "apple_silicon" because nothing ever set arch --
    including Linux and NVIDIA boxes. The router uses arch for device-aware
    scoring, so a wrong value is not cosmetic."""

    def test_payload_no_longer_defaults_to_apple(self):
        assert _payload("http://localhost:11434", "10.0.0.5").arch == "unknown"

    def test_collector_actually_sets_arch(self):
        from fleet_manager.node.collector import _detect_arch

        arch = _detect_arch()
        assert arch and arch != "unknown"

    def test_detected_arch_is_plausible_for_this_machine(self):
        import platform

        from fleet_manager.node.collector import _detect_arch

        arch = _detect_arch()
        if platform.system() == "Darwin" and platform.machine().lower() in ("arm64", "aarch64"):
            assert arch == "apple_silicon"
        else:
            assert arch != "apple_silicon", "non-Apple hardware must not claim Apple Silicon"
