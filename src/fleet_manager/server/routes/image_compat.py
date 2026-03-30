"""Image generation routes — routes mflux requests to the best available node."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

router = APIRouter(tags=["images"])


def _score_image_candidates(candidates, registry) -> object:
    """Pick the best node for image generation.

    Simple scoring: prefer nodes not currently generating, with the most
    available memory and lowest CPU utilization.
    """
    scored = []
    for node in candidates:
        score = 0.0
        # Penalty if currently generating (only 1 at a time)
        if node.image and node.image.generating:
            score -= 50.0
        # Memory available (more = better)
        if node.memory:
            score += node.memory.available_gb * 0.5
        # CPU utilization (lower = better)
        if node.cpu:
            score -= node.cpu.utilization_pct * 0.2
        scored.append((score, node))

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


@router.post("/api/generate-image")
async def generate_image(request: Request):
    """Generate an image on the best available node with the requested model."""
    settings = request.app.state.settings

    if not settings.image_generation:
        return JSONResponse(
            status_code=503,
            content={
                "error": "Image generation is disabled. "
                "Enable via FLEET_IMAGE_GENERATION=true or the settings API."
            },
        )

    body = await request.json()
    model = body.get("model", "")
    prompt = body.get("prompt", "")

    if not model:
        return JSONResponse(status_code=400, content={"error": "model is required"})
    if not prompt:
        return JSONResponse(status_code=400, content={"error": "prompt is required"})

    registry = request.app.state.registry

    # Find nodes with this image model available and an active image server
    candidates = [
        n
        for n in registry.get_online_nodes()
        if n.image
        and n.image_port > 0
        and any(m.name == model for m in n.image.models_available)
    ]

    if not candidates:
        # List available image models for helpful error
        all_image_models: set[str] = set()
        for n in registry.get_online_nodes():
            if n.image:
                for m in n.image.models_available:
                    all_image_models.add(m.name)
        available = ", ".join(sorted(all_image_models)) if all_image_models else "none"
        return JSONResponse(
            status_code=404,
            content={
                "error": f"Image model '{model}' not available on any node. Available: {available}"
            },
        )

    best = _score_image_candidates(candidates, registry)
    logger.info(f"Image generation: model={model} → node={best.node_id}")

    proxy = request.app.state.streaming_proxy
    try:
        png_bytes = await proxy.generate_image_on_node(
            best.node_id, body, timeout=settings.image_timeout
        )
        return Response(
            content=png_bytes,
            media_type="image/png",
            headers={
                "X-Fleet-Node": best.node_id,
                "X-Fleet-Model": model,
            },
        )
    except Exception as e:
        logger.error(f"Image generation failed on {best.node_id}: {repr(e)}")
        return JSONResponse(
            status_code=502,
            content={"error": f"Image generation failed on {best.node_id}: {repr(e)}"},
        )
