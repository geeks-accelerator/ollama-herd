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
