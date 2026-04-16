"""Vision embedding routes — routes image embedding requests to the best node.

Supports DINOv2, SigLIP, and CLIP via ONNX.
Clients can use /api/embed-image directly or /api/embed with a vision
embedding model name (e.g., "dinov2-vit-s14").
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["vision-embedding"])

# Model names that are vision embedding models (not Ollama text embeddings)
VISION_EMBEDDING_MODEL_NAMES = {
    "dinov2-vit-s14",
    "siglip2-base",
    "clip-vit-b32",
    # Common aliases
    "clip",
    "dinov2",
    "siglip",
    "siglip2",
}


def is_vision_embedding_model(model: str) -> bool:
    """Check if a model name refers to a vision embedding model."""
    return model.lower().strip() in VISION_EMBEDDING_MODEL_NAMES


def _resolve_model_name(model: str) -> str:
    """Resolve aliases to canonical model names."""
    aliases = {
        "clip": "clip-vit-b32",
        "dinov2": "dinov2-vit-s14",
        "siglip": "siglip2-base",
        "siglip2": "siglip2-base",
    }
    return aliases.get(model.lower().strip(), model.lower().strip())


def _score_embedding_candidates(candidates):
    """Score nodes for vision embedding — prefer idle, more memory."""
    scored = []
    for node in candidates:
        score = 0.0
        if node.vision_embedding and node.vision_embedding.processing:
            score -= 50.0
        if node.memory:
            score += node.memory.available_gb * 0.5
        if node.cpu:
            score -= node.cpu.utilization_pct * 0.2
        scored.append((score, node))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


@router.post("/api/embed-image")
async def embed_image(request: Request):
    """Generate vision embeddings for one or more images.

    Request:
        {
            "model": "dinov2-vit-s14",  // optional — auto-selects best
            "images": ["base64..."],     // required
        }

    Response:
        {
            "model": "dinov2-vit-s14",
            "embeddings": [[0.123, ...], ...],
            "dimensions": 384,
            "node": "Neons-Mac-Studio"
        }
    """
    settings = request.app.state.settings

    if not settings.vision_embedding:
        return JSONResponse(
            status_code=503,
            content={
                "error": "Vision embedding is disabled. "
                "Enable via FLEET_VISION_EMBEDDING=true or the settings API."
            },
        )

    # Use cached body from /api/embed redirect, or parse fresh
    body = getattr(request.state, "_parsed_body", None)
    if body is None:
        body = await request.json()
    model = body.get("model", "")
    images = body.get("images", [])

    # Also accept "input" field (Ollama embed compat)
    if not images:
        images = body.get("input", [])
        if isinstance(images, str):
            images = [images]

    if not images:
        return JSONResponse(
            status_code=400,
            content={"error": "Request must include 'images' as a list of base64 strings"},
        )

    # Resolve model alias
    if model:
        model = _resolve_model_name(model)

    registry = request.app.state.registry

    # Find nodes with vision embedding capabilities
    candidates = [
        n
        for n in registry.get_online_nodes()
        if n.vision_embedding
        and n.vision_embedding_port > 0
        and n.vision_embedding.models_available
    ]

    # Filter for specific model if requested
    if model:
        model_candidates = [
            n for n in candidates
            if any(m.name == model for m in n.vision_embedding.models_available)
        ]
        if model_candidates:
            candidates = model_candidates

    if not candidates:
        return JSONResponse(
            status_code=404,
            content={
                "error": "No vision embedding models available on any node. "
                "Download a model first: POST /api/pull {\"model\": \"dinov2-vit-s14\"}"
            },
        )

    best = _score_embedding_candidates(candidates)

    # Resolve model name from node's available models if not specified
    if not model:
        model = best.vision_embedding.models_available[0].name

    logger.info(f"Vision embedding: {len(images)} image(s), model={model} → {best.node_id}")

    # Direct proxy to the node's embedding server (fast, non-streaming)
    proxy = request.app.state.streaming_proxy
    try:
        result = await proxy.embed_image_on_node(
            best.node_id,
            {"model": model, "images": images},
            timeout=settings.vision_embedding_timeout,
        )
    except Exception as exc:
        logger.error(f"Vision embedding failed on {best.node_id}: {exc}")
        return JSONResponse(
            status_code=502,
            content={"error": f"Embedding failed: {exc}"},
        )

    result["node"] = best.node_id
    return JSONResponse(
        content=result,
        headers={"X-Fleet-Node": best.node_id},
    )
