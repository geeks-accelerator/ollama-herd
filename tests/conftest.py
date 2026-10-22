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


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    """Hide the operator's own ``FLEET_*`` environment from every test.

    ``ServerSettings`` and ``NodeSettings`` read the whole ``FLEET_`` /
    ``FLEET_NODE_`` namespace through pydantic, and six places read
    ``os.environ`` directly (``FLEET_ANTHROPIC_MODEL_MAP``,
    ``FLEET_NUM_CTX_OVERRIDES``, ``FLEET_DYNAMIC_NUM_CTX``,
    ``FLEET_MLX_ENABLED``, ``FLEET_EMBEDDING_USE_COREML``, the log levels).  So
    on a machine that actually *runs* a herd, the suite tests the operator's
    configuration rather than the defaults, and which tests fail depends on
    whose shell it is.

    Found 2026-10-22: three tests went red on this fleet and passed in CI,
    because ``~/.zshrc`` exports ``FLEET_ANTHROPIC_MODEL_MAP`` and
    ``_check_mapped_model_missing`` reads it straight from ``os.environ`` --
    ``mapped_model_missing`` fired CRITICAL inside ``test_empty_fleet``, taking
    the health score to 80, and ``/v1/responses`` answered 503 (no node hosts
    the mapped model) where the test wanted the empty-fleet 404.  Nothing was
    wrong with the code.

    Three modules had already hit this and each patched it locally, listing the
    variables its own author remembered -- which is why the fourth caller was
    exposed.  One definition instead, for the same reason
    ``resident_context_for`` is one: several callers need the answer and must
    not drift.  ``CLOUD_FLEET_TOKEN`` goes too; a test has no business reading
    the operator's platform credential.

    Tests that want a variable set it with ``monkeypatch.setenv``, which runs
    after this (autouse fixtures are ordered before the test body and before
    explicitly requested fixtures of the same scope).
    """
    import os

    for key in list(os.environ):
        if key.startswith("FLEET_") or key == "CLOUD_FLEET_TOKEN":
            monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture(autouse=True)
def _clear_context_protection_events():
    """Reset the module-global context-protection event list between tests.

    ``streaming._context_protection_events`` is a process-wide list that the
    health engine reads to build ``context_protection_active`` and
    ``context_protection_insufficient``.  Any test that builds an Ollama body
    appends to it, so events leaked across *modules*: run
    ``test_streaming.py`` before ``test_health_engine.py`` and three tests there
    failed on events they never created, while each passed in isolation and the
    full suite passed on whatever order pytest happened to pick.

    Filed 2026-10-11 after reproducing it on unmodified main, and it went red
    the same day when ``test_context_protection_every_format.py`` added 20 tests
    that each record events -- a latent order dependency became a reliable
    failure, which is the good outcome.

    Clearing on both sides: before, so a test starts from a known state; after,
    so a test that *asserts* on events does not hand them to the next module.
    """
    from fleet_manager.server import streaming

    streaming._context_protection_events.clear()
    yield
    streaming._context_protection_events.clear()


@pytest.fixture(autouse=True)
def _clear_collector_ttl_caches():
    """Reset the per-heartbeat TTL caches between every test.

    ``_detect_image_models`` and ``_detect_transcription_models`` are
    decorated with a 30 s TTL cache (see ``_ttl_cache`` in collector.py).
    Without resetting between tests, a test that monkey-patches
    ``shutil.which`` to simulate "no mflux installed" would still see the
    cached "mflux installed" result from a previous test, producing
    confusing assertion failures.

    Same reasoning for the ``/dashboard/api/health`` response cache —
    state from one test must not bleed into the next.
    """
    from fleet_manager.node import collector
    from fleet_manager.server.routes import dashboard

    if hasattr(collector._detect_image_models, "cache_clear"):
        collector._detect_image_models.cache_clear()
    if hasattr(collector._detect_transcription_models, "cache_clear"):
        collector._detect_transcription_models.cache_clear()
    dashboard._HEALTH_CACHE["payload"] = None
    dashboard._HEALTH_CACHE["ts"] = 0.0
    # Tag-metadata send-on-change state: a test that "already sent" meta would
    # otherwise make the next test's first heartbeat omit it.
    collector._meta_sent["fingerprint"] = None
    collector._meta_sent["at"] = 0.0
    yield


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
    chip: str = "",
    memory_bandwidth_gbps: float = 0.0,
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
        chip=chip,
        memory_bandwidth_gbps=memory_bandwidth_gbps,
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
