"""FLEET_NODE_GPU_MEMORY_GB: GPU memory the OS does not report as RAM.

Issue #9: an AMD APU (GMKtec EVO-X5 Pro, 192 GB) with 160 GB given to the GPU
in the BIOS shows Linux about 30 GB. The fit gate read only the OS's available
RAM, so every model larger than that was eliminated although Ollama runs it on
the GPU. A node can now declare that memory; the router adds what loaded models
do not already hold there.
"""

from __future__ import annotations

import pytest

from fleet_manager.common.ollama_client import OllamaClient
from fleet_manager.models.config import NodeSettings, ServerSettings
from fleet_manager.models.node import LoadedModel
from fleet_manager.server.registry import NodeRegistry
from fleet_manager.server.scorer import ScoringEngine
from tests.conftest import make_heartbeat, make_node

# llama3.3:70b is sized at about 40 GB by the shared estimator, which puts it
# between the APU's OS RAM and its RAM plus carve-out.
BIG_MODEL = "llama3.3:70b"


class TestModelMemoryAvailable:
    def test_without_gpu_memory_it_is_the_os_figure(self):
        node = make_node(memory_total=64.0, memory_used=20.0)
        assert node.model_memory_available_gb() == pytest.approx(44.0)

    def test_gpu_memory_is_added(self):
        node = make_node(memory_total=32.0, memory_used=2.0)
        node.memory.gpu_total_gb = 160.0
        assert node.model_memory_available_gb() == pytest.approx(190.0)

    def test_models_already_in_gpu_memory_are_subtracted(self):
        node = make_node(memory_total=32.0, memory_used=2.0, loaded_models=[("a", 120.0)])
        node.ollama.models_loaded[0].size_vram_gb = 120.0
        node.memory.gpu_total_gb = 160.0
        assert node.model_memory_available_gb() == pytest.approx(30.0 + 40.0)

    def test_gpu_pool_never_goes_negative(self):
        node = make_node(memory_total=32.0, memory_used=2.0, loaded_models=[("a", 200.0)])
        node.ollama.models_loaded[0].size_vram_gb = 200.0
        node.memory.gpu_total_gb = 160.0
        assert node.model_memory_available_gb() == pytest.approx(30.0)

    def test_size_vram_is_ignored_without_a_declared_pool(self):
        """On Apple Silicon size_vram is the whole model and already in RAM;
        with no pool declared nothing is added or subtracted."""
        node = make_node(memory_total=128.0, memory_used=80.0, loaded_models=[("a", 60.0)])
        node.ollama.models_loaded[0].size_vram_gb = 60.0
        assert node.model_memory_available_gb() == pytest.approx(48.0)

    def test_no_memory_report_is_zero(self):
        node = make_node()
        node.memory = None
        assert node.model_memory_available_gb() == 0.0


@pytest.mark.asyncio
class TestFitGate:
    @pytest.fixture
    def registry(self):
        return NodeRegistry(ServerSettings())

    @pytest.fixture
    def scorer(self, registry):
        return ScoringEngine(ServerSettings(), registry)

    async def test_apu_without_the_setting_is_refused_the_issue_9_case(self, scorer, registry):
        hb = make_heartbeat(
            node_id="evo-x5", memory_total=32.0, memory_used=2.0, available_models=[BIG_MODEL]
        )
        await registry.update_from_heartbeat(hb)
        assert scorer.score_request(BIG_MODEL, {}) == []

    async def test_apu_with_the_setting_gets_the_model(self, scorer, registry):
        hb = make_heartbeat(
            node_id="evo-x5", memory_total=32.0, memory_used=2.0, available_models=[BIG_MODEL]
        )
        hb.memory.gpu_total_gb = 160.0
        await registry.update_from_heartbeat(hb)
        results = scorer.score_request(BIG_MODEL, {})
        assert [r.node_id for r in results] == ["evo-x5"]

    async def test_a_full_gpu_pool_still_refuses(self, scorer, registry):
        """The pool is shared with what is already loaded there."""
        hb = make_heartbeat(
            node_id="evo-x5",
            memory_total=32.0,
            memory_used=2.0,
            loaded_models=[("other:120b", 158.0)],
            available_models=[BIG_MODEL, "other:120b"],
        )
        hb.ollama.models_loaded[0].size_vram_gb = 158.0
        hb.memory.gpu_total_gb = 160.0
        await registry.update_from_heartbeat(hb)
        assert scorer.score_request(BIG_MODEL, {}) == []


class TestSetting:
    def test_read_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("FLEET_NODE_GPU_MEMORY_GB", "160")
        assert NodeSettings().gpu_memory_gb == 160.0

    def test_unset_means_detect(self, monkeypatch):
        monkeypatch.delenv("FLEET_NODE_GPU_MEMORY_GB", raising=False)
        assert NodeSettings().gpu_memory_gb is None

    def test_zero_is_kept_as_none_declared(self, monkeypatch):
        monkeypatch.setenv("FLEET_NODE_GPU_MEMORY_GB", "0")
        assert NodeSettings().gpu_memory_gb == 0.0

    def test_negative_is_rejected(self, monkeypatch):
        monkeypatch.setenv("FLEET_NODE_GPU_MEMORY_GB", "-1")
        with pytest.raises(ValueError):
            NodeSettings()


@pytest.mark.asyncio
async def test_size_vram_is_read_from_api_ps(httpx_mock):
    httpx_mock.add_response(
        url="http://ollama.test/api/ps",
        json={"models": [{"model": "big:70b", "size": 45 * 1024**3, "size_vram": 42 * 1024**3}]},
    )
    client = OllamaClient("http://ollama.test")
    models = await client.get_running_models()
    assert models == [LoadedModel(name="big:70b", size_gb=45.0, size_vram_gb=42.0)]


@pytest.mark.asyncio
async def test_the_node_puts_the_setting_in_its_heartbeat():
    from unittest.mock import AsyncMock, MagicMock

    from fleet_manager.node.collector import collect_heartbeat

    ollama = MagicMock()
    ollama.get_running_models = AsyncMock(return_value=[])
    ollama.get_available_models = AsyncMock(return_value=[])
    ollama.get_version = AsyncMock(return_value="0.40.16")
    ollama.get_available_model_sizes = AsyncMock(return_value={})
    ollama.get_available_model_meta = AsyncMock(return_value={})
    payload = await collect_heartbeat("evo-x5", ollama, gpu_memory_gb=160.0)
    assert payload.memory.gpu_total_gb == 160.0
    assert (await collect_heartbeat("plain", ollama)).memory.gpu_total_gb == 0.0


class TestDetectedVersusDeclared:
    def test_detected_uses_the_devices_measured_use(self):
        """Detected memory knows everything on the GPU, not only Ollama's models."""
        node = make_node(memory_total=32.0, memory_used=2.0, loaded_models=[("a", 20.0)])
        node.ollama.models_loaded[0].size_vram_gb = 20.0
        node.memory.gpu_total_gb = 160.0
        node.memory.gpu_used_gb = 50.0
        node.memory.gpu_source = "detected"
        assert node.model_memory_available_gb() == pytest.approx(30.0 + 110.0)

    def test_declared_uses_what_ollama_holds(self):
        node = make_node(memory_total=32.0, memory_used=2.0, loaded_models=[("a", 20.0)])
        node.ollama.models_loaded[0].size_vram_gb = 20.0
        node.memory.gpu_total_gb = 160.0
        node.memory.gpu_used_gb = 50.0  # ignored: not measured
        node.memory.gpu_source = "override"
        assert node.model_memory_available_gb() == pytest.approx(30.0 + 140.0)


@pytest.mark.asyncio
class TestCollector:
    @pytest.fixture
    def ollama(self):
        from unittest.mock import AsyncMock, MagicMock

        o = MagicMock()
        o.get_running_models = AsyncMock(return_value=[])
        o.get_available_models = AsyncMock(return_value=[])
        o.get_version = AsyncMock(return_value="0.40.16")
        o.get_available_model_sizes = AsyncMock(return_value={})
        o.get_available_model_meta = AsyncMock(return_value={})
        return o

    @pytest.fixture
    def detected(self, monkeypatch):
        from fleet_manager.models.node import GpuDevice
        from fleet_manager.node import collector

        devices = [
            GpuDevice(
                name="AMD GPU (card1)",
                kind="integrated",
                total_gb=160.0,
                used_gb=12.0,
                source="amdgpu",
            )
        ]
        monkeypatch.setattr(collector, "detect_gpu_memory", lambda: devices)
        return devices

    async def test_unset_reports_what_is_detected(self, ollama, detected):
        from fleet_manager.node.collector import collect_heartbeat

        m = (await collect_heartbeat("evo-x5", ollama)).memory
        assert (m.gpu_total_gb, m.gpu_used_gb, m.gpu_source) == (160.0, 12.0, "detected")
        assert m.gpu_devices == detected

    async def test_a_number_replaces_detection(self, ollama, detected):
        from fleet_manager.node.collector import collect_heartbeat

        m = (await collect_heartbeat("evo-x5", ollama, gpu_memory_gb=96.0)).memory
        assert (m.gpu_total_gb, m.gpu_source, m.gpu_devices) == (96.0, "override", [])

    async def test_zero_turns_detection_off(self, ollama, detected):
        from fleet_manager.node.collector import collect_heartbeat

        m = (await collect_heartbeat("evo-x5", ollama, gpu_memory_gb=0.0)).memory
        assert (m.gpu_total_gb, m.gpu_source) == (0.0, "")


def test_status_carries_the_fit_figure():
    from fleet_manager.server.serializers import serialize_node

    node = make_node(memory_total=32.0, memory_used=2.0)
    node.memory.gpu_total_gb = 160.0
    node.memory.gpu_source = "detected"
    data = serialize_node(node)
    assert data["model_memory_available_gb"] == pytest.approx(190.0)
    assert data["memory"]["gpu_source"] == "detected"
