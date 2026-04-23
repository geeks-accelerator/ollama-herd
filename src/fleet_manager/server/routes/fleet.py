"""Fleet status endpoint for monitoring and future dashboard."""

from __future__ import annotations

import contextlib
import time

from fastapi import APIRouter, Request

router = APIRouter(tags=["fleet"])


@router.get("/fleet/status")
async def fleet_status(request: Request):
    """Full fleet state — nodes, queues, and health summary."""
    registry = request.app.state.registry
    queue_mgr = request.app.state.queue_mgr

    nodes = []
    total_models_loaded = 0
    total_requests_active = 0

    for node in registry.get_all_nodes():
        node_data = {
            "node_id": node.node_id,
            "status": node.status.value,
            "hardware": {
                "memory_total_gb": node.hardware.memory_total_gb,
                "cores_physical": node.hardware.cores_physical,
                "chip": node.hardware.chip,
                "memory_bandwidth_gbps": node.hardware.memory_bandwidth_gbps,
                "arch": node.hardware.arch,
            },
            "ollama_url": node.ollama_base_url,
        }
        if node.cpu:
            node_data["cpu"] = node.cpu.model_dump()
        if node.memory:
            node_data["memory"] = node.memory.model_dump()
        if node.ollama:
            node_data["ollama"] = node.ollama.model_dump()
            total_models_loaded += len(node.ollama.models_loaded)
            total_requests_active += node.ollama.requests_active
        if node.image:
            node_data["image"] = node.image.model_dump()
            node_data["image_port"] = node.image_port
        if node.transcription:
            node_data["transcription"] = node.transcription.model_dump()
            node_data["transcription_port"] = node.transcription_port
        if node.vision_embedding:
            node_data["vision_embedding"] = node.vision_embedding.model_dump()
            node_data["vision_embedding_port"] = node.vision_embedding_port
        nodes.append(node_data)

    online_count = sum(1 for n in registry.get_all_nodes() if n.status.value == "online")

    return {
        "fleet": {
            "nodes_total": len(nodes),
            "nodes_online": online_count,
            "models_loaded": total_models_loaded,
            "requests_active": total_requests_active,
        },
        "nodes": nodes,
        "queues": queue_mgr.get_queue_info(),
        "timestamp": time.time(),
    }


@router.get("/fleet/queue")
async def fleet_queue(request: Request):
    """Lightweight queue status for client-side backoff decisions.

    Returns current queue depths, estimated wait, and per-tag active request
    counts. Designed for high-frequency polling (sub-second response).
    Clients can use this to decide whether to send a request now or wait.
    """
    queue_mgr = request.app.state.queue_mgr
    registry = request.app.state.registry

    queue_info = queue_mgr.get_queue_info()
    # Merge in MLX synthetic queues so the MLX backend is visible alongside
    # Ollama queues. mlx_lm.server is single-threaded per process — its
    # in-flight count tells you when MLX is busy and how many are waiting.
    mlx_proxy = getattr(request.app.state, "mlx_proxy", None)
    if mlx_proxy is not None:
        # Never break /fleet/queue on MLX stats hiccups
        with contextlib.suppress(Exception):
            queue_info = {**queue_info, **mlx_proxy.get_queue_info()}
    total_pending = sum(q["pending"] for q in queue_info.values())
    total_in_flight = sum(q["in_flight"] for q in queue_info.values())
    total_completed = sum(q["completed"] for q in queue_info.values())
    total_failed = sum(q["failed"] for q in queue_info.values())

    # Estimate wait time from recent latency (rough: pending * avg_latency / concurrency)
    latency_store = getattr(request.app.state, "latency_store", None)
    estimated_wait_ms = None
    if total_pending > 0 and latency_store:
        # Use cached p75 latencies across all queues
        latencies = []
        for _key, q in queue_info.items():
            p75 = latency_store.get_cached_percentile(q["node_id"], q["model"])
            if p75 is not None:
                latencies.append(p75)
        if latencies:
            avg_latency = sum(latencies) / len(latencies)
            total_concurrency = sum(
                q.get("concurrency", 1) for q in queue_info.values()
            )
            estimated_wait_ms = int(
                total_pending * avg_latency / max(total_concurrency, 1)
            )

    online_count = sum(
        1 for n in registry.get_all_nodes() if n.status.value == "online"
    )

    return {
        "queue_depth": total_pending + total_in_flight,
        "pending": total_pending,
        "in_flight": total_in_flight,
        "completed": total_completed,
        "failed": total_failed,
        "estimated_wait_ms": estimated_wait_ms,
        "nodes_online": online_count,
        "queues": {
            key: {
                "pending": q["pending"],
                "in_flight": q["in_flight"],
                "concurrency": q.get("concurrency", 1),
                "model": q["model"],
                "node_id": q["node_id"],
                "backend": q.get("backend", "ollama"),
                # MLX-only fields: admission control exposure so clients +
                # dashboard can distinguish "backend overloaded" from
                # "request errored" and tune retry behavior accordingly.
                "completed": q.get("completed", 0),
                "failed": q.get("failed", 0),
                "rejected": q.get("rejected", 0),
                "max_queue_depth": q.get("max_queue_depth"),
                # Rolling MLX prompt-cache hit rate (fraction in [0, 1]).
                # None until the proxy has observations; clients should
                # treat as "no data yet" rather than 0%.
                "cache_hit_rate": q.get("cache_hit_rate"),
            }
            for key, q in queue_info.items()
        },
        "timestamp": time.time(),
    }
