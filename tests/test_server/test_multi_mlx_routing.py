"""Tests for multi-MLX registry aggregation and MlxProxy URL resolution.

Covers the server-side half of multi-MLX-server support:
  - Registry builds a correct {model: url} map from heartbeats
  - MlxProxy uses the resolver first, falls back to legacy base_url
  - Per-URL client cache is isolated between servers
"""

from __future__ import annotations

import pytest

from fleet_manager.models.config import ServerSettings
from fleet_manager.models.node import (
    CpuMetrics,
    HardwareProfile,
    MemoryMetrics,
    MlxServerInfo,
    NodeState,
    NodeStatus,
    OllamaMetrics,
)
from fleet_manager.server.mlx_proxy import MlxProxy
from fleet_manager.server.registry import NodeRegistry


def _mk_node(
    node_id: str,
    lan_ip: str,
    mlx_servers: list[MlxServerInfo],
    status: NodeStatus = NodeStatus.ONLINE,
) -> NodeState:
    import time as _t
    return NodeState(
        node_id=node_id,
        status=status,
        hardware=HardwareProfile(
            node_id=node_id, memory_total_gb=128.0, cores_physical=16,
        ),
        last_heartbeat=_t.time(),
        cpu=CpuMetrics(cores_physical=16, utilization_pct=5.0),
        memory=MemoryMetrics(total_gb=128.0, used_gb=20.0, available_gb=100.0),
        ollama=OllamaMetrics(models_loaded=[], models_available=[]),
        ollama_base_url=f"http://{lan_ip}:11434",
        mlx_servers=mlx_servers,
        mlx_bind_host="0.0.0.0",
    )


class TestRegistryResolveMlxUrl:
    def test_returns_none_when_no_nodes(self):
        reg = NodeRegistry(ServerSettings())
        assert reg.resolve_mlx_url("mlx:any/model") is None

    def test_returns_url_for_healthy_server(self):
        reg = NodeRegistry(ServerSettings())
        node = _mk_node("mac-studio", "10.0.0.100", [
            MlxServerInfo(
                port=11440, model="mlx-community/Qwen3-Coder-Next-4bit",
                status="healthy",
            ),
        ])
        reg._nodes[node.node_id] = node
        url = reg.resolve_mlx_url("mlx:mlx-community/Qwen3-Coder-Next-4bit")
        assert url == "http://10.0.0.100:11440"

    def test_accepts_bare_model_id_without_prefix(self):
        reg = NodeRegistry(ServerSettings())
        node = _mk_node("mac-studio", "10.0.0.100", [
            MlxServerInfo(port=11440, model="a/main", status="healthy"),
        ])
        reg._nodes[node.node_id] = node
        # Both forms should resolve to the same URL
        assert reg.resolve_mlx_url("mlx:a/main") == "http://10.0.0.100:11440"
        assert reg.resolve_mlx_url("a/main") == "http://10.0.0.100:11440"

    def test_skips_unhealthy_servers(self):
        reg = NodeRegistry(ServerSettings())
        node = _mk_node("mac-studio", "10.0.0.100", [
            MlxServerInfo(port=11440, model="a/main", status="unhealthy"),
            MlxServerInfo(port=11441, model="a/compactor", status="healthy"),
        ])
        reg._nodes[node.node_id] = node
        # unhealthy main → no URL
        assert reg.resolve_mlx_url("mlx:a/main") is None
        # healthy compactor → URL
        assert reg.resolve_mlx_url("mlx:a/compactor") == "http://10.0.0.100:11441"

    def test_skips_offline_nodes(self):
        reg = NodeRegistry(ServerSettings())
        node = _mk_node(
            "mac-studio", "10.0.0.100",
            [MlxServerInfo(port=11440, model="a/main", status="healthy")],
            status=NodeStatus.OFFLINE,
        )
        reg._nodes[node.node_id] = node
        assert reg.resolve_mlx_url("mlx:a/main") is None

    def test_multi_node_aggregation(self):
        reg = NodeRegistry(ServerSettings())
        studio = _mk_node("studio", "10.0.0.100", [
            MlxServerInfo(port=11440, model="big/main", status="healthy"),
        ])
        mbp = _mk_node("mbp", "10.0.0.200", [
            MlxServerInfo(port=11440, model="small/helper", status="healthy"),
        ])
        reg._nodes["studio"] = studio
        reg._nodes["mbp"] = mbp
        assert reg.resolve_mlx_url("mlx:big/main") == "http://10.0.0.100:11440"
        assert reg.resolve_mlx_url("mlx:small/helper") == "http://10.0.0.200:11440"

    def test_all_mlx_urls_builds_full_map(self):
        reg = NodeRegistry(ServerSettings())
        studio = _mk_node("studio", "10.0.0.100", [
            MlxServerInfo(port=11440, model="big/main", status="healthy"),
            MlxServerInfo(port=11441, model="big/compactor", status="healthy"),
            MlxServerInfo(port=11442, model="big/dead", status="unhealthy"),
        ])
        reg._nodes["studio"] = studio
        out = reg.all_mlx_urls()
        assert out == {
            "mlx:big/main": "http://10.0.0.100:11440",
            "mlx:big/compactor": "http://10.0.0.100:11441",
        }


class TestMlxProxyUrlResolution:
    def test_falls_back_to_fixed_url_when_no_resolver(self):
        p = MlxProxy("http://127.0.0.1:11440")
        assert p._resolve_url("any/model") == "http://127.0.0.1:11440"
        assert p._resolve_url(None) == "http://127.0.0.1:11440"

    def test_resolver_wins_over_fixed_when_match_found(self):
        def resolver(model: str) -> str | None:
            if model == "special/model":
                return "http://10.0.0.200:11441"
            return None

        p = MlxProxy("http://127.0.0.1:11440", url_resolver=resolver)
        assert p._resolve_url("special/model") == "http://10.0.0.200:11441"
        # No match → fallback to fixed
        assert p._resolve_url("unknown/model") == "http://127.0.0.1:11440"

    def test_resolver_exception_falls_back_to_fixed(self):
        def bad_resolver(_model: str) -> str | None:
            raise RuntimeError("registry blew up")

        p = MlxProxy("http://127.0.0.1:11440", url_resolver=bad_resolver)
        assert p._resolve_url("any/model") == "http://127.0.0.1:11440"

    def test_raises_when_no_url_available(self):
        p = MlxProxy(url_resolver=lambda _m: None)
        with pytest.raises(ValueError, match="no URL configured"):
            import asyncio
            asyncio.run(p._get_client("any/model"))

    @pytest.mark.asyncio
    async def test_per_url_client_cache_isolation(self):
        def resolver(model: str) -> str | None:
            if model == "a/main":
                return "http://10.0.0.100:11440"
            if model == "a/helper":
                return "http://10.0.0.100:11441"
            return None

        p = MlxProxy(url_resolver=resolver)
        c1 = await p._get_client("a/main")
        c2 = await p._get_client("a/helper")
        c3 = await p._get_client("a/main")  # should hit cache
        assert c1 is not c2  # different URLs ⇒ different clients
        assert c1 is c3       # same URL ⇒ reused client
        assert len(p._clients) == 2
        # close drains them all
        await p.close()
        assert len(p._clients) == 0
