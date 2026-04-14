"""Shared test fixtures for the fleet manager test suite."""

from __future__ import annotations

import time

import pytest

from fleet_manager.models.config import ServerSettings
from fleet_manager.models.node import (
    CapacityMetrics,
    CpuMetrics,
    DiskMetrics,
    HardwareProfile,
    HeartbeatPayload,
    LoadedModel,
    MemoryMetrics,
    MemoryPressure,
    NodeState,
    NodeStatus,
    OllamaMetrics,
)
from fleet_manager.models.request import InferenceRequest, RequestFormat
from fleet_manager.server.registry import NodeRegistry


@pytest.fixture
def settings():
    return ServerSettings()


@pytest.fixture
def registry(settings):
    return NodeRegistry(settings)


def make_heartbeat(
    node_id: str = "mac-studio",
    memory_total: float = 64.0,
    memory_used: float = 20.0,
    cores: int = 12,
    cpu_pct: float = 15.0,
    pressure: MemoryPressure = MemoryPressure.NORMAL,
    loaded_models: list[tuple[str, float] | tuple[str, float, int]] | None = None,
    available_models: list[str] | None = None,
    lan_ip: str = "192.168.1.100",
    ollama_host: str = "http://localhost:11434",
    disk_total: float = 500.0,
    disk_used: float = 200.0,
    agent_version: str = "",
    connection_failures: int = 0,
    connection_failures_total: int = 0,
) -> HeartbeatPayload:
    loaded = []
    for entry in loaded_models or []:
        if len(entry) == 3:
            name, size, ctx = entry
            loaded.append(LoadedModel(name=name, size_gb=size, context_length=ctx))
        else:
            name, size = entry
            loaded.append(LoadedModel(name=name, size_gb=size))
    return HeartbeatPayload(
        node_id=node_id,
        cpu=CpuMetrics(cores_physical=cores, utilization_pct=cpu_pct),
        memory=MemoryMetrics(
            total_gb=memory_total,
            used_gb=memory_used,
            available_gb=memory_total - memory_used,
            pressure=pressure,
        ),
        disk=DiskMetrics(
            total_gb=disk_total,
            used_gb=disk_used,
            available_gb=disk_total - disk_used,
        ),
        ollama=OllamaMetrics(
            models_loaded=loaded,
            models_available=available_models or [],
        ),
        lan_ip=lan_ip,
        ollama_host=ollama_host,
        agent_version=agent_version,
        connection_failures=connection_failures,
        connection_failures_total=connection_failures_total,
    )


def make_node(
    node_id: str = "mac-studio",
    status: NodeStatus = NodeStatus.ONLINE,
    memory_total: float = 64.0,
    memory_used: float = 20.0,
    cores: int = 12,
    cpu_pct: float = 15.0,
    pressure: MemoryPressure = MemoryPressure.NORMAL,
    loaded_models: list[tuple[str, float]] | None = None,
    available_models: list[str] | None = None,
    capacity: CapacityMetrics | None = None,
    disk_total: float = 500.0,
    disk_used: float = 200.0,
    agent_version: str = "",
) -> NodeState:
    loaded = [
        LoadedModel(name=name, size_gb=size)
        for name, size in (loaded_models or [])
    ]
    return NodeState(
        node_id=node_id,
        status=status,
        hardware=HardwareProfile(
            node_id=node_id,
            memory_total_gb=memory_total,
            cores_physical=cores,
        ),
        last_heartbeat=time.time(),
        cpu=CpuMetrics(cores_physical=cores, utilization_pct=cpu_pct),
        memory=MemoryMetrics(
            total_gb=memory_total,
            used_gb=memory_used,
            available_gb=memory_total - memory_used,
            pressure=pressure,
        ),
        disk=DiskMetrics(
            total_gb=disk_total,
            used_gb=disk_used,
            available_gb=disk_total - disk_used,
        ),
        ollama=OllamaMetrics(
            models_loaded=loaded,
            models_available=available_models or [],
        ),
        capacity=capacity,
        agent_version=agent_version,
    )


def make_inference_request(
    model: str = "llama3.3:70b",
    fmt: RequestFormat = RequestFormat.OPENAI,
    fallback_models: list[str] | None = None,
) -> InferenceRequest:
    return InferenceRequest(
        model=model,
        original_model=model,
        fallback_models=fallback_models or [],
        messages=[{"role": "user", "content": "Hello"}],
        original_format=fmt,
        raw_body={"model": model, "messages": [{"role": "user", "content": "Hello"}]},
    )
