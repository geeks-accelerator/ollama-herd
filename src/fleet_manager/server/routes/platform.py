"""Platform connection routes — dashboard-facing, localhost only.

Three endpoints for opting a node into the platform.ollamaherd.com
coordination platform:

  GET  /api/platform/status       — current state + identity + features
  POST /api/platform/connect      — validate token, register, persist
  POST /api/platform/disconnect   — clear local state, stop tasks

Same trust model as every other Settings toggle: localhost only, no
auth layer. Anyone with LAN access can modify these. Users are
responsible for not exposing the OSS dashboard beyond their LAN.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from fleet_manager.node.platform_connection import (
    DEFAULT_PLATFORM_URL,
    InvalidTokenError,
    PlatformUnreachableError,
    RegistrationError,
    connect_to_platform,
    disconnect_from_platform,
    load_state,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["platform"])


@router.get("/api/platform/status")
async def platform_status(request: Request):
    """Return the current connection state + identity + feature toggles."""
    state = load_state()

    # Feature toggles come from NodeSettings if available; routes run on
    # the server side, so we read saved state for everything.
    settings = getattr(request.app.state, "settings", None)
    features = {
        "telemetry_local_summary": False,
        "telemetry_include_tags": False,
        "p2p_serve": False,  # disabled until P2P routing ships
    }
    if settings is not None:
        features["telemetry_local_summary"] = getattr(
            settings, "telemetry_local_summary", False
        )
        features["telemetry_include_tags"] = getattr(
            settings, "telemetry_include_tags", False
        )

    if state is None:
        return {
            "state": "not_connected",
            "platform_url": DEFAULT_PLATFORM_URL,
            "connected": None,
            "features": features,
            "error": None,
        }

    return {
        "state": "connected",
        "platform_url": state.platform_url,
        "connected": {
            "user_email": state.user_email,
            "user_display_name": state.user_display_name,
            "node_id": state.node_id,
            "connected_at": state.connected_at.isoformat(),
        },
        "features": features,
        "error": None,
    }


@router.post("/api/platform/connect")
async def platform_connect(request: Request):
    """Validate operator token, register node, persist state.

    Request body:
        {"operator_token": "herd_...", "platform_url": "https://..."}

    Returns 200 on success with the new state, 400 on validation error,
    502 on platform unreachable.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"error": "Invalid JSON body"},
        )

    token = body.get("operator_token", "").strip()
    platform_url = body.get("platform_url", DEFAULT_PLATFORM_URL).strip()
    node_name = body.get("node_name")
    region = body.get("region")

    if not token:
        return JSONResponse(
            status_code=400,
            content={
                "error": "operator_token is required",
                "hint": "Get one at platform.ollamaherd.com/web/",
            },
        )

    try:
        state = await connect_to_platform(
            token=token,
            platform_url=platform_url,
            node_name=node_name,
            region=region,
        )
    except InvalidTokenError as exc:
        return JSONResponse(
            status_code=400,
            content={"error": str(exc), "code": "invalid_token"},
        )
    except PlatformUnreachableError as exc:
        return JSONResponse(
            status_code=502,
            content={"error": str(exc), "code": "platform_unreachable"},
        )
    except RegistrationError as exc:
        return JSONResponse(
            status_code=400,
            content={"error": str(exc), "code": "registration_failed"},
        )
    except Exception as exc:
        logger.exception("Unexpected error during platform connect")
        return JSONResponse(
            status_code=500,
            content={"error": f"Unexpected error: {exc}", "code": "internal_error"},
        )

    return {
        "state": "connected",
        "node_id": state.node_id,
        "user_email": state.user_email,
        "user_display_name": state.user_display_name,
        "platform_url": state.platform_url,
        "connected_at": state.connected_at.isoformat(),
    }


@router.post("/api/platform/disconnect")
async def platform_disconnect(request: Request):
    """Clear local state and stop platform-dependent tasks.

    Idempotent: disconnecting when already disconnected is a no-op success.
    Does NOT delete the node from the platform side — user must do that
    via the platform dashboard.
    """
    disconnect_from_platform()
    return {"state": "not_connected"}
