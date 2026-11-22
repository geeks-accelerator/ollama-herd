"""scripts/fake_fleet.py: fake herd-nodes for control-plane scale runs.

The short run drives a real ``NodeRegistry`` behind the real heartbeat and
``/fleet/status`` routes over ASGI, with the heartbeat thresholds shrunk from
15/30 s to fractions of a second so drains, rejoins and offline detection all
happen inside one test.
"""

from __future__ import annotations

import argparse
import asyncio

import httpx
import pytest
from fastapi import FastAPI

from fleet_manager.models.config import ServerSettings
from fleet_manager.models.node import HeartbeatPayload, NodeStatus
from fleet_manager.server.queue_manager import QueueManager
from fleet_manager.server.registry import NodeRegistry
from fleet_manager.server.routes import fleet as fleet_routes
from fleet_manager.server.routes import heartbeat as heartbeat_routes
from tests.test_scripts import load_script

ff = load_script("fake_fleet")

MODELS = [("llama3.2:3b", 2.0), ("qwen3:8b", 5.2), ("nomic-embed-text:latest", 0.3)]


class TestPayload:
    def test_heartbeat_is_a_valid_payload_pointing_at_the_mock(self):
        config = ff.FleetConfig(
            nodes=1,
            models=MODELS[:2],
            memory_gb=128,
            chip="Apple M3 Ultra",
            num_parallel=4,
            ollama=["http://127.0.0.1:11601"],
        )
        (node,) = ff.build_nodes(config)
        hb = HeartbeatPayload(**ff.heartbeat_for(node, config, "192.168.1.20"))
        assert hb.node_id == "fake-000"
        assert hb.ollama_host == "http://127.0.0.1:11601"
        assert hb.lan_ip == "192.168.1.20"
        assert [(m.name, m.size_gb) for m in hb.ollama.models_loaded] == MODELS[:2]
        assert hb.ollama.models_available == ["llama3.2:3b", "qwen3:8b"]
        assert hb.ollama.num_parallel == 4
        assert hb.memory.total_gb == 128
        assert hb.memory_bandwidth_gbps == 819.0
        assert hb.memory.used_gb == pytest.approx(8.0 + 7.2)

    def test_models_rotate_across_nodes_and_backends_round_robin(self):
        config = ff.FleetConfig(
            nodes=4, models=MODELS, models_per_node=1, ollama=["http://a:1", "http://b:2"]
        )
        nodes = ff.build_nodes(config)
        assert [n.models[0][0] for n in nodes] == [
            "llama3.2:3b",
            "qwen3:8b",
            "nomic-embed-text:latest",
            "llama3.2:3b",
        ]
        assert [n.ollama_host for n in nodes] == ["http://a:1", "http://b:2"] * 2

    def test_model_spec(self):
        assert ff.parse_model("llama3.2:3b:2.0") == ("llama3.2:3b", 2.0)
        with pytest.raises(argparse.ArgumentTypeError):
            ff.parse_model("llama3.2")

    def test_cli(self):
        router, config, output = ff.parse_args(
            ["--router", "http://r:11435/", "--nodes", "3", "--model", "a:1b:1.5", "--kill", "1"]
        )
        assert router == "http://r:11435"
        assert config.nodes == 3 and config.models == [("a:1b", 1.5)] and config.kill == 1
        assert output is None

    def test_cli_refuses_more_kills_than_nodes(self):
        with pytest.raises(SystemExit):
            ff.parse_args(["--router", "http://r", "--nodes", "2", "--kill", "3"])

    async def test_follow_ps_reports_what_the_backend_holds_on_a_gpu(self):
        mock = load_script("mock_ollama")
        backend_app = mock.build_app(
            mock.MockConfig(models=["a:7b:15.0", "b:7b:18.0"], max_loaded=1)
        )
        config = ff.FleetConfig(nodes=1, models=[], memory_gb=55, gpu_gb=22.5, follow_ps=True)
        (node,) = ff.build_nodes(config)
        node.ollama_host = "http://backend"
        transport = httpx.ASGITransport(app=backend_app)
        async with httpx.AsyncClient(transport=transport) as client:
            await ff.follow_backend(client, node)
            assert node.models == [("a:7b", 15.0), ("b:7b", 18.0)]
            assert node.loaded == [("a:7b", 15.0)]
            await client.post("http://backend/api/generate", json={"model": "b:7b", "prompt": ""})
            await ff.follow_backend(client, node)
        assert node.loaded == [("b:7b", 18.0)]
        hb = HeartbeatPayload(**ff.heartbeat_for(node, config, "10.0.0.2"))
        assert [(m.name, m.size_vram_gb) for m in hb.ollama.models_loaded] == [("b:7b", 18.0)]
        assert hb.ollama.models_available == ["a:7b", "b:7b"]
        assert hb.ollama.models_available_sizes == {"a:7b": 15.0, "b:7b": 18.0}
        assert hb.memory.gpu_total_gb == 22.5
        assert hb.memory.gpu_used_gb == 18.0 and hb.memory.gpu_source == "detected"
        node.loading_gb = 4.0  # mid-load: on the card, not yet in /api/ps
        assert ff.heartbeat_for(node, config, "10.0.0.2")["memory"]["gpu_used_gb"] == 22.0
        assert hb.memory.used_gb == 8.0  # the weights are on the card, not in RAM


class TestPerNodeMemory:
    """``--pressure`` and ``--working-set``: per-node memory state."""

    def test_defaults_report_normal_pressure_and_no_working_set(self):
        config = ff.FleetConfig(nodes=1, models=MODELS[:1])
        (node,) = ff.build_nodes(config)
        body = ff.heartbeat_for(node, config, "10.0.0.2")
        assert body["memory"]["pressure"] == "normal"
        assert body["memory"].get("gpu_working_set_gb", 0.0) == 0.0  # 0: not reported

    def test_by_index_by_id_and_star(self):
        _, config, _ = ff.parse_args(
            [
                "--router", "http://r", "--nodes", "3", "--model", "a:1b:1.0",
                "--pressure", "*=warn", "--pressure", "1=critical",
                "--working-set", "fake-002=17.8", "--working-set", "*=107.5",
            ]
        )  # fmt: skip
        nodes = ff.build_nodes(config)
        bodies = [HeartbeatPayload(**ff.heartbeat_for(n, config, "10.0.0.2")) for n in nodes]
        assert [b.memory.pressure for b in bodies] == ["warn", "critical", "warn"]
        assert [b.memory.gpu_working_set_gb for b in bodies] == [107.5, 107.5, 17.8]

    @pytest.mark.parametrize(
        "flag",
        [
            ["--pressure", "0=hot"],
            ["--pressure", "critical"],
            ["--working-set", "0=lots"],
            ["--working-set", "0=-1"],
            ["--pressure", "5=critical"],  # no such node
            ["--working-set", "fake-009=8"],
        ],
    )
    def test_cli_refuses_bad_values_and_unknown_nodes(self, flag):
        with pytest.raises(SystemExit):
            ff.parse_args(["--router", "http://r", "--nodes", "2", *flag])

    async def test_the_router_registers_the_pressure(self):
        app = router_app(ServerSettings())
        config = ff.FleetConfig(nodes=2, models=MODELS[:1], pressure={"0": "critical"})
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://router") as client:
            fleet = ff.Fleet(config, client)
            for node in fleet.nodes:
                assert await fleet.post(ff.heartbeat_for(node, config, "10.0.0.2"), "hb")
        pressure = {n.node_id: n.memory.pressure for n in app.state.registry.get_all_nodes()}
        assert pressure == {"fake-000": "critical", "fake-001": "normal"}


def router_app(settings: ServerSettings) -> FastAPI:
    app = FastAPI()
    app.include_router(heartbeat_routes.router)
    app.include_router(fleet_routes.router)
    app.state.registry = NodeRegistry(settings)
    app.state.queue_mgr = QueueManager()
    return app


async def test_short_run_registers_churns_and_detects_the_dead():
    settings = ServerSettings(
        heartbeat_interval=0.05, heartbeat_timeout=0.15, heartbeat_offline=0.3
    )
    app = router_app(settings)
    registry = app.state.registry
    monitor = asyncio.create_task(registry.monitor_heartbeats())
    config = ff.FleetConfig(
        nodes=6,
        models=MODELS,
        models_per_node=2,
        ollama=["http://127.0.0.1:11601"],
        interval=0.05,
        duration=1.0,
        churn_every=0.25,
        rejoin_after=0.1,
        kill=2,
        kill_after=0.2,
        status_every=0,
        seed=1,
    )
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://router") as client:
            fleet = ff.Fleet(config, client)
            summary = await fleet.run(asyncio.Event())
    finally:
        monitor.cancel()

    assert summary["heartbeat_failures"] == 0, summary["failure_reasons"]
    assert summary["heartbeats_sent"] > 6 * 5
    assert summary["post_ms"]["n"] == summary["heartbeats_sent"] - summary["heartbeat_failures"]
    assert summary["post_ms"]["p50"] is not None
    assert summary["rejoins"] >= 1
    assert summary["drains_sent"] >= summary["rejoins"] + 4  # churn, plus the exit drain

    nodes = registry.get_all_nodes()
    assert sorted(n.node_id for n in nodes) == [f"fake-{i:03d}" for i in range(6)]
    # The loopback mock URL resolved the way the docstring promises.
    assert {n.ollama_base_url for n in nodes} == {"http://localhost:11601"}
    # Everyone is offline at the end: the killed by timeout, the rest by drain.
    assert all(n.status == NodeStatus.OFFLINE for n in nodes)

    killed = summary["killed"]
    assert len(killed) == 2
    for k in killed:
        assert k["degraded_after_kill_s"] is not None
        assert k["offline_after_kill_s"] is not None
        assert k["degraded_after_kill_s"] <= k["offline_after_kill_s"]
        # Offline needs heartbeat_offline (0.3 s) of silence, then up to a
        # monitor tick and a poll; the upper bound leaves room for a slow CI box.
        assert 0.3 <= k["offline_after_last_heartbeat_s"] < 1.0
