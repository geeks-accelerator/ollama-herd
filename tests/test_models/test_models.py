"""Tests for data models."""

from __future__ import annotations

import time

from fleet_manager.models.config import NodeSettings, ServerSettings
from fleet_manager.models.node import (
    CpuMetrics,
    HardwareProfile,
    HeartbeatPayload,
    LoadedModel,
    MemoryMetrics,
    MemoryPressure,
    NodeState,
    NodeStatus,
    OllamaMetrics,
)
from fleet_manager.models.request import (
    InferenceRequest,
    QueueEntry,
    RequestFormat,
    RequestStatus,
    RoutingResult,
    normalize_model_name,
)


class TestNodeModels:
    def test_memory_pressure_values(self):
        assert MemoryPressure.NORMAL == "normal"
        assert MemoryPressure.WARN == "warn"
        assert MemoryPressure.CRITICAL == "critical"

    def test_node_status_values(self):
        assert NodeStatus.ONLINE == "online"
        assert NodeStatus.DEGRADED == "degraded"
        assert NodeStatus.OFFLINE == "offline"

    def test_cpu_metrics(self):
        cpu = CpuMetrics(cores_physical=12, utilization_pct=45.5)
        assert cpu.cores_physical == 12
        assert cpu.utilization_pct == 45.5

    def test_memory_metrics(self):
        mem = MemoryMetrics(
            total_gb=64.0,
            used_gb=32.0,
            available_gb=32.0,
            pressure=MemoryPressure.NORMAL,
        )
        assert mem.total_gb == 64.0
        assert mem.pressure == MemoryPressure.NORMAL

    def test_loaded_model(self):
        m = LoadedModel(name="llama3.3:70b", size_gb=40.0, requests_active=2)
        assert m.name == "llama3.3:70b"
        assert m.size_gb == 40.0
        assert m.requests_active == 2

    def test_loaded_model_defaults(self):
        m = LoadedModel(name="phi4:14b", size_gb=9.0)
        assert m.requests_active == 0

    def test_ollama_metrics(self):
        om = OllamaMetrics(
            models_loaded=[LoadedModel(name="phi4:14b", size_gb=9.0)],
            models_available=["phi4:14b", "llama3.3:70b"],
            requests_active=1,
        )
        assert len(om.models_loaded) == 1
        assert len(om.models_available) == 2

    def test_heartbeat_payload(self):
        hb = HeartbeatPayload(
            node_id="studio",
            cpu=CpuMetrics(cores_physical=12, utilization_pct=10.0),
            memory=MemoryMetrics(total_gb=64.0, used_gb=20.0, available_gb=44.0),
            ollama=OllamaMetrics(),
        )
        assert hb.node_id == "studio"
        assert hb.arch == "apple_silicon"
        assert hb.draining is False
        assert hb.timestamp > 0

    def test_hardware_profile(self):
        hp = HardwareProfile(node_id="studio", memory_total_gb=64.0, cores_physical=12)
        assert hp.node_id == "studio"
        assert hp.chip == ""

    def test_node_state_defaults(self):
        ns = NodeState(
            node_id="studio",
            hardware=HardwareProfile(node_id="studio"),
        )
        assert ns.status == NodeStatus.ONLINE
        assert ns.missed_heartbeats == 0
        assert ns.cpu is None
        assert ns.memory is None
        assert ns.ollama is None
        assert ns.model_unloaded_at == {}

    def test_node_state_model_unloaded_at(self):
        ns = NodeState(
            node_id="studio",
            hardware=HardwareProfile(node_id="studio"),
            model_unloaded_at={"phi4:14b": time.time()},
        )
        assert "phi4:14b" in ns.model_unloaded_at


class TestModelNameNormalization:
    """Model names should always include a tag to match Ollama conventions."""

    def test_appends_latest_when_no_tag(self):
        assert normalize_model_name("qwen3-coder") == "qwen3-coder:latest"
        assert normalize_model_name("llama3") == "llama3:latest"

    def test_preserves_explicit_tag(self):
        assert normalize_model_name("qwen3:235b") == "qwen3:235b"
        assert normalize_model_name("phi4:14b") == "phi4:14b"
        assert normalize_model_name("llama3.3:70b") == "llama3.3:70b"

    def test_preserves_latest_tag(self):
        assert normalize_model_name("qwen3-coder:latest") == "qwen3-coder:latest"

    def test_empty_string_unchanged(self):
        assert normalize_model_name("") == ""

    def test_inference_request_normalizes_model(self):
        req = InferenceRequest(model="qwen3-coder")
        assert req.model == "qwen3-coder:latest"

    def test_inference_request_normalizes_original_model(self):
        req = InferenceRequest(model="qwen3-coder", original_model="qwen3-coder")
        assert req.original_model == "qwen3-coder:latest"

    def test_inference_request_normalizes_fallbacks(self):
        req = InferenceRequest(
            model="phi4:14b",
            fallback_models=["llama3", "qwen3:235b"],
        )
        assert req.fallback_models == ["llama3:latest", "qwen3:235b"]

    def test_inference_request_preserves_tagged_model(self):
        req = InferenceRequest(model="phi4:14b")
        assert req.model == "phi4:14b"


class TestRequestModels:
    def test_request_format_values(self):
        assert RequestFormat.OPENAI == "openai"
        assert RequestFormat.OLLAMA == "ollama"

    def test_request_status_values(self):
        assert RequestStatus.PENDING == "pending"
        assert RequestStatus.IN_FLIGHT == "in_flight"
        assert RequestStatus.COMPLETED == "completed"
        assert RequestStatus.FAILED == "failed"

    def test_inference_request_defaults(self):
        req = InferenceRequest(model="phi4:14b")
        assert req.model == "phi4:14b"
        assert req.stream is True
        assert req.temperature == 0.7
        assert req.max_tokens is None
        assert req.original_format == RequestFormat.OPENAI
        assert req.request_id  # auto-generated UUID
        assert req.created_at > 0
        assert req.tags == []

    def test_inference_request_with_tags(self):
        req = InferenceRequest(
            model="phi4:14b",
            tags=["my-app", "production", "user:alice"],
        )
        assert req.tags == ["my-app", "production", "user:alice"]

    def test_inference_request_custom(self):
        req = InferenceRequest(
            model="llama3.3:70b",
            messages=[{"role": "user", "content": "Hello"}],
            stream=False,
            temperature=0.3,
            max_tokens=100,
            original_format=RequestFormat.OLLAMA,
        )
        assert req.stream is False
        assert req.temperature == 0.3
        assert req.max_tokens == 100

    def test_queue_entry_defaults(self):
        req = InferenceRequest(model="phi4:14b")
        entry = QueueEntry(request=req)
        assert entry.status == RequestStatus.PENDING
        assert entry.assigned_node == ""
        assert entry.started_at is None
        assert entry.completed_at is None

    def test_routing_result(self):
        rr = RoutingResult(
            node_id="studio",
            queue_key="studio:phi4:14b",
            score=85.0,
            scores_breakdown={"thermal": 50.0, "memory_fit": 20.0, "queue_depth": 0.0},
        )
        assert rr.score == 85.0
        assert rr.scores_breakdown["thermal"] == 50.0


class TestConfigModels:
    def test_server_settings_defaults(self):
        s = ServerSettings()
        assert s.host == "0.0.0.0"
        assert s.port == 11435
        assert s.heartbeat_interval == 5.0
        assert s.score_model_hot == 50.0
        assert s.score_model_warm == 30.0
        assert s.score_model_cold == 10.0
        assert s.score_wait_time_max_penalty == 25.0
        assert s.rebalance_interval == 5.0
        assert s.pre_warm_threshold == 3

    def test_node_settings_defaults(self):
        s = NodeSettings()
        assert s.node_id == ""
        assert s.ollama_host == "http://localhost:11434"
        assert s.router_url == ""
        assert s.heartbeat_interval == 5.0
