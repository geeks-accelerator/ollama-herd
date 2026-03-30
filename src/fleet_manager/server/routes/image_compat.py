"""Image generation routes — routes mflux requests to the best available node."""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from fleet_manager.models.request import InferenceRequest, QueueEntry, RequestFormat

logger = logging.getLogger(__name__)

router = APIRouter(tags=["images"])

# In-memory tracking of image generation events (same pattern as VRAM fallbacks)
_image_gen_events: list[dict] = []
_MAX_IMAGE_EVENTS = 200


def _record_image_gen(
    model: str,
    node_id: str,
    status: str,
    generation_ms: int = 0,
    width: int = 0,
    height: int = 0,
    error: str = "",
) -> None:
    """Record an image generation event for health monitoring."""
    _image_gen_events.append({
        "timestamp": time.time(),
        "model": model,
        "node_id": node_id,
        "status": status,
        "generation_ms": generation_ms,
        "width": width,
        "height": height,
        "error": error,
    })
    if len(_image_gen_events) > _MAX_IMAGE_EVENTS:
        del _image_gen_events[: len(_image_gen_events) - _MAX_IMAGE_EVENTS]


def get_image_gen_events(hours: float = 24) -> list[dict]:
    """Get image generation events from the last N hours."""
    cutoff = time.time() - hours * 3600
    return [e for e in _image_gen_events if e["timestamp"] >= cutoff]


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
    width = body.get("width", 1024)
    height = body.get("height", 1024)
    logger.info(f"Image generation: model={model} {width}x{height} → {best.node_id}")

    # Create an InferenceRequest so it flows through the queue like LLM requests
    inference_req = InferenceRequest(
        model=model,
        original_model=model,
        stream=False,
        original_format=RequestFormat.OLLAMA,
        raw_body=body,
    )

    queue_key = f"{best.node_id}:{inference_req.model}"
    entry = QueueEntry(
        request=inference_req,
        assigned_node=best.node_id,
        routing_score=0.0,
    )

    proxy = request.app.state.streaming_proxy
    queue_mgr = request.app.state.queue_mgr
    process_fn = proxy.make_image_process_fn(
        queue_key, queue_mgr, timeout=settings.image_timeout
    )
    response_future = await queue_mgr.enqueue(entry, process_fn)

    start = time.monotonic()
    try:
        stream = await response_future
        png_bytes = b""
        async for chunk in stream:
            png_bytes = chunk  # Single chunk — the full PNG

        elapsed_ms = int((time.monotonic() - start) * 1000)
        _record_image_gen(
            model, best.node_id, "completed",
            generation_ms=elapsed_ms, width=width, height=height,
        )
        return Response(
            content=png_bytes,
            media_type="image/png",
            headers={
                "X-Fleet-Node": best.node_id,
                "X-Fleet-Model": model,
                "X-Generation-Time": str(elapsed_ms),
            },
        )
    except Exception as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        _record_image_gen(
            model, best.node_id, "failed",
            generation_ms=elapsed_ms, width=width, height=height,
            error=repr(e),
        )
        logger.error(f"Image generation failed on {best.node_id}: {repr(e)}")
        return JSONResponse(
            status_code=502,
            content={"error": f"Image generation failed on {best.node_id}: {repr(e)}"},
        )
