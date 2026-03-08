"""Heartbeat endpoint — receives node agent heartbeats."""

from __future__ import annotations

from fastapi import APIRouter, Request

from fleet_manager.models.node import HeartbeatPayload

router = APIRouter(tags=["heartbeat"])


@router.post("/heartbeat")
async def receive_heartbeat(request: Request):
    """Process a node heartbeat or drain signal."""
    body = await request.json()

    # Handle drain signal
    if body.get("draining"):
        node_id = body.get("node_id", "unknown")
        request.app.state.registry.handle_drain(node_id)
        return {"status": "draining", "node_id": node_id}

    payload = HeartbeatPayload(**body)
    registry = request.app.state.registry
    client_ip = request.client.host if request.client else ""
    node = await registry.update_from_heartbeat(payload, request_ip=client_ip)
    return {"status": "ok", "node_status": node.status.value}
