"""Assembles heartbeat payloads from system metrics and Ollama state."""

from __future__ import annotations

import logging

from fleet_manager.common.ollama_client import OllamaClient
from fleet_manager.common.system_metrics import get_cpu_metrics, get_local_ip, get_memory_metrics
from fleet_manager.models.node import HeartbeatPayload, OllamaMetrics

logger = logging.getLogger(__name__)


async def collect_heartbeat(
    node_id: str,
    ollama: OllamaClient,
    ollama_host: str = "http://localhost:11434",
) -> HeartbeatPayload:
    """Assemble a complete heartbeat payload from local system state."""
    cpu = get_cpu_metrics()
    memory = get_memory_metrics()

    try:
        models_loaded = await ollama.get_running_models()
        models_available = await ollama.get_available_models()
        requests_active = sum(m.requests_active for m in models_loaded)
    except Exception as e:
        logger.debug(f"Ollama not reachable: {e}")
        models_loaded = []
        models_available = []
        requests_active = 0

    return HeartbeatPayload(
        node_id=node_id,
        cpu=cpu,
        memory=memory,
        ollama=OllamaMetrics(
            models_loaded=models_loaded,
            models_available=models_available,
            requests_active=requests_active,
        ),
        ollama_host=ollama_host,
        lan_ip=get_local_ip(),
    )
