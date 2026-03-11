"""Assembles heartbeat payloads from system metrics and Ollama state."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from fleet_manager.common.ollama_client import OllamaClient
from fleet_manager.common.system_metrics import (
    get_cpu_metrics,
    get_disk_metrics,
    get_local_ip,
    get_memory_metrics,
)
from fleet_manager.models.node import CapacityMetrics, HeartbeatPayload, OllamaMetrics

logger = logging.getLogger(__name__)


def _make_lan_reachable_url(ollama_host: str, lan_ip: str) -> str:
    """Replace localhost in ollama_host with the LAN IP so the router can reach us."""
    parsed = urlparse(ollama_host)
    if parsed.hostname in ("localhost", "127.0.0.1", "::1") and lan_ip and lan_ip != "127.0.0.1":
        port = parsed.port or 11434
        return f"http://{lan_ip}:{port}"
    return ollama_host


async def collect_heartbeat(
    node_id: str,
    ollama: OllamaClient,
    ollama_host: str = "http://localhost:11434",
    capacity_learner=None,
) -> HeartbeatPayload:
    """Assemble a complete heartbeat payload from local system state."""
    cpu = get_cpu_metrics()
    memory = get_memory_metrics()
    disk = get_disk_metrics()

    try:
        models_loaded = await ollama.get_running_models()
        models_available = await ollama.get_available_models()
        requests_active = sum(m.requests_active for m in models_loaded)
        logger.debug(
            f"Ollama state: {len(models_loaded)} loaded, "
            f"{len(models_available)} available, "
            f"{requests_active} active requests"
        )
    except Exception as e:
        logger.warning(f"Ollama not reachable at {ollama_host}: {type(e).__name__}: {e}")
        models_loaded = []
        models_available = []
        requests_active = 0

    # Run capacity learner observation if enabled
    capacity = None
    if capacity_learner is not None:
        cap_info = capacity_learner.observe(
            cpu.utilization_pct,
            memory.used_gb / memory.total_gb * 100 if memory.total_gb > 0 else 0,
        )
        capacity = CapacityMetrics(
            mode=cap_info.mode.value,
            ceiling_gb=cap_info.ceiling_gb,
            availability_score=cap_info.availability_score,
            reason=cap_info.reason,
            override_active=cap_info.override_active,
            learning_confidence=cap_info.learning_confidence,
            days_observed=cap_info.days_observed,
        )

    lan_ip = get_local_ip()

    return HeartbeatPayload(
        node_id=node_id,
        cpu=cpu,
        memory=memory,
        disk=disk,
        ollama=OllamaMetrics(
            models_loaded=models_loaded,
            models_available=models_available,
            requests_active=requests_active,
        ),
        ollama_host=_make_lan_reachable_url(ollama_host, lan_ip),
        lan_ip=lan_ip,
        capacity=capacity,
    )
