"""Priority Model Preloader — loads highest-priority models after restart.

Uses weighted usage data from the trace store to determine which models
to load first:  priority = (requests_24h * 3) + (requests_7d_daily_avg).

Runs once after the first node registers, loading models in priority
order until memory is full.  Non-blocking — runs as a background task.
"""

from __future__ import annotations

import asyncio
import logging
import time

from fleet_manager.models.config import ServerSettings
from fleet_manager.server.model_knowledge import lookup_model
from fleet_manager.server.registry import NodeRegistry
from fleet_manager.server.streaming import StreamingProxy
from fleet_manager.server.trace_store import TraceStore

logger = logging.getLogger(__name__)

# Cache priority scores to avoid repeated DB queries
_priority_cache: list[dict] = []
_priority_cache_time: float = 0
_CACHE_TTL = 300  # 5 minutes


async def get_cached_priorities(trace_store: TraceStore) -> list[dict]:
    """Get priority scores, cached for 5 minutes."""
    global _priority_cache, _priority_cache_time
    if time.time() - _priority_cache_time < _CACHE_TTL and _priority_cache:
        return _priority_cache
    _priority_cache = await trace_store.get_model_priority_scores()
    _priority_cache_time = time.time()
    return _priority_cache


def get_model_priority(model: str, priorities: list[dict]) -> float:
    """Look up the priority score for a model name."""
    for entry in priorities:
        if entry["model"] == model:
            return entry["priority_score"]
    return 0.0


def _estimate_model_size(model: str) -> float:
    """Estimate model RAM in GB from catalog or name heuristics."""
    spec = lookup_model(model)
    if spec:
        return spec.ram_gb

    # Heuristic from model name
    lower = model.lower()
    if "embed" in lower or "nomic" in lower:
        return 0.5
    if any(s in lower for s in (":1b", ":0.6b", ":0.5b")):
        return 1.0
    if any(s in lower for s in (":3b", ":4b")):
        return 3.0
    if any(s in lower for s in (":7b", ":8b")):
        return 5.0
    if any(s in lower for s in (":13b", ":14b")):
        return 10.0
    if any(s in lower for s in (":22b", ":27b", ":32b")):
        return 20.0
    if any(s in lower for s in (":70b", ":72b")):
        return 45.0
    if any(s in lower for s in (":120b", ":122b", ":235b")):
        return 75.0
    return 10.0  # Conservative default


async def preload_priority_models(
    registry: NodeRegistry,
    trace_store: TraceStore,
    proxy: StreamingProxy,
    settings: ServerSettings,
) -> None:
    """Wait for first node, then preload highest-priority models.

    Runs once at startup.  Waits up to 60s for a node to register,
    then loads models in priority order until memory would be exceeded.
    """
    # Wait for at least one node to come online
    for _ in range(60):
        nodes = registry.get_online_nodes()
        if nodes:
            break
        await asyncio.sleep(1)
    else:
        logger.info("Priority preload: no nodes registered after 60s, skipping")
        return

    # Brief delay to let the node's heartbeat fully populate
    await asyncio.sleep(3)

    # Use cached priorities so VRAM fallback can read the same data
    priorities = await get_cached_priorities(trace_store)
    if not priorities:
        logger.info("Priority preload: no usage history, skipping")
        return

    logger.info(
        f"Priority preload: {len(priorities)} model(s) in history, "
        f"top={priorities[0]['model']} (score={priorities[0]['priority_score']})"
    )

    loaded_count = 0
    for entry in priorities:
        model = entry["model"]
        score = entry["priority_score"]

        if score < 1.0:
            break  # No point loading rarely-used models

        nodes = registry.get_online_nodes()
        if not nodes:
            break

        # Check if already loaded on any node
        already_loaded = any(
            n.ollama and model in [m.name for m in n.ollama.models_loaded]
            for n in nodes
        )
        if already_loaded:
            continue

        # Check if available on disk on any node
        # models_available is list[str] (model name strings)
        available_nodes = [
            n for n in nodes
            if n.ollama and model in n.ollama.models_available
        ]
        if not available_nodes:
            continue

        # Pick node with most available memory
        best = max(
            available_nodes,
            key=lambda n: n.memory.available_gb if n.memory else 0,
        )

        # Check if there's enough memory (with 20% headroom)
        model_size = _estimate_model_size(model)
        available = best.memory.available_gb if best.memory else 0
        if available < model_size * 1.2:
            logger.info(
                f"Priority preload: stopping at {model} — "
                f"need {model_size:.0f}GB but only {available:.0f}GB free "
                f"on {best.node_id}"
            )
            break

        logger.info(
            f"Priority preload: loading {model} "
            f"(score={score}, ~{model_size:.0f}GB) on {best.node_id}"
        )
        try:
            await proxy.pre_warm(best.node_id, model)
            loaded_count += 1
            # Brief pause to let Ollama update its model list
            await asyncio.sleep(2)
        except Exception as e:
            logger.warning(f"Priority preload: failed to load {model}: {e}")

    logger.info(f"Priority preload complete: loaded {loaded_count} model(s)")

    # Keep priority models loaded — check every 10 minutes and reload
    # if they've been evicted.  Ollama's KEEP_ALIVE=-1 should prevent
    # this, but memory pressure or other models can force eviction.
    while True:
        await asyncio.sleep(600)  # 10 minutes
        try:
            await _refresh_priority_models(registry, trace_store, proxy)
        except Exception as exc:
            logger.warning(f"Priority refresh failed: {exc}")


async def _refresh_priority_models(
    registry: NodeRegistry,
    trace_store: TraceStore,
    proxy: StreamingProxy,
) -> None:
    """Reload priority models if they've been evicted from memory."""
    priorities = await get_cached_priorities(trace_store)
    if not priorities:
        return

    # Only reload the top 3 priority models to avoid memory thrashing
    top_priorities = [p for p in priorities[:3] if p["priority_score"] >= 10]

    for entry in top_priorities:
        model = entry["model"]
        score = entry["priority_score"]

        nodes = registry.get_online_nodes()
        if not nodes:
            return

        # Already loaded? Nothing to do.
        already_loaded = any(
            n.ollama and model in [m.name for m in n.ollama.models_loaded]
            for n in nodes
        )
        if already_loaded:
            continue

        # Available on disk?
        available_nodes = [
            n for n in nodes
            if n.ollama and model in n.ollama.models_available
        ]
        if not available_nodes:
            continue

        best = max(
            available_nodes,
            key=lambda n: n.memory.available_gb if n.memory else 0,
        )
        model_size = _estimate_model_size(model)
        available = best.memory.available_gb if best.memory else 0
        if available < model_size * 1.2:
            continue  # Not enough memory — skip silently

        logger.info(
            f"Priority refresh: reloading evicted {model} "
            f"(score={score}, ~{model_size:.0f}GB) on {best.node_id}"
        )
        try:
            await proxy.pre_warm(best.node_id, model)
            await asyncio.sleep(2)
        except Exception as e:
            logger.warning(f"Priority refresh: failed to reload {model}: {e}")
