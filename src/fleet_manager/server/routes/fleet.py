"""Fleet status endpoint for monitoring and future dashboard."""

from __future__ import annotations

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
            }
            for key, q in queue_info.items()
        },
        "timestamp": time.time(),
    }
