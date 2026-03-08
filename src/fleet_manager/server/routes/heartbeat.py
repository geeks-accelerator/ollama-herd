"""Heartbeat endpoint — receives node agent heartbeats."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from pydantic import ValidationError

from fleet_manager.models.node import HeartbeatPayload

logger = logging.getLogger(__name__)
router = APIRouter(tags=["heartbeat"])


@router.post("/heartbeat")
async def receive_heartbeat(request: Request):
    """Process a node heartbeat or drain signal."""
    body = await request.json()

    # Handle drain signal
    if body.get("draining"):
        node_id = body.get("node_id", "unknown")
        logger.info(f"Drain signal received from {node_id}")
        request.app.state.registry.handle_drain(node_id)
        return {"status": "draining", "node_id": node_id}

    try:
        payload = HeartbeatPayload(**body)
    except ValidationError as e:
        client = request.client.host if request.client else "?"
        logger.warning(f"Malformed heartbeat from {client}: {e}")
        raise

    registry = request.app.state.registry
    client_ip = request.client.host if request.client else ""
    node = await registry.update_from_heartbeat(payload, request_ip=client_ip)
    logger.debug(f"Heartbeat from {payload.node_id}: status={node.status.value}")
    return {"status": "ok", "node_status": node.status.value}
