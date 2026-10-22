"""Vision embedding routes — routes image embedding requests to the best node.

Supports DINOv2, SigLIP, and CLIP via ONNX.
Clients can use /api/embed-image directly or /api/embed with a vision
embedding model name (e.g., "dinov2-vit-s14").
"""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from fleet_manager.common.errors import describe_exception
from fleet_manager.server.fleet_headers import fleet_headers
from fleet_manager.server.routes.routing import direct_proxy_tracer, record_routing_rejection

logger = logging.getLogger(__name__)

router = APIRouter(tags=["vision-embedding"])

# Trace fields.  `original_format` matches native text embedding, so both kinds
# of /api/embed request read alike; the tag is what tells them apart.
_ORIGINAL_FORMAT = "embed"
_TAGS = ("embed", "vision-embed")

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
    trace_store = request.app.state.trace_store
    client_ip = request.client.host if request.client else ""

    # Use cached body from /api/embed redirect, or parse fresh
    body = getattr(request.state, "_parsed_body", None)
    if body is None:
        body = await request.json()
    model = body.get("model", "")

    # Resolve model alias
    if model:
        model = _resolve_model_name(model)

    async def reject(status_code: int, reason: str) -> JSONResponse:
        """Answer before any node is chosen, leaving a ``rejected`` trace.

        No node is attached, so these stay out of per-node error rates: a
        disabled feature or a missing model is not a node fault.
        """
        logger.warning(f"Vision embedding for '{model}' rejected ({status_code}): {reason}")
        await record_routing_rejection(
            trace_store,
            SimpleNamespace(model=model, request_id=str(uuid.uuid4())),
            reason=reason,
            original_format=_ORIGINAL_FORMAT,
            client_ip=client_ip,
            tags=list(_TAGS),
        )
        return JSONResponse(status_code=status_code, content={"error": reason})

    if not settings.vision_embedding:
        return await reject(
            503,
            "Vision embedding is disabled. "
            "Enable via FLEET_VISION_EMBEDDING=true or the settings API.",
        )

    images = body.get("images", [])

    # Also accept "input" field (Ollama embed compat)
    if not images:
        images = body.get("input", [])
        if isinstance(images, str):
            images = [images]

    if not images:
        return await reject(400, "Request must include 'images' as a list of base64 strings")

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
            n
            for n in candidates
            if any(m.name == model for m in n.vision_embedding.models_available)
        ]
        if model_candidates:
            candidates = model_candidates

    if not candidates:
        return await reject(
            404,
            "No vision embedding models available on any node. "
            'Download a model first: POST /api/pull {"model": "dinov2-vit-s14"}',
        )

    best = _score_embedding_candidates(candidates)

    # Resolve model name from node's available models if not specified
    if not model:
        model = best.vision_embedding.models_available[0].name

    logger.info(f"Vision embedding: {len(images)} image(s), model={model} → {best.node_id}")

    # Direct proxy to the node's embedding server (fast, non-streaming)
    proxy = request.app.state.streaming_proxy
    trace = direct_proxy_tracer(
        trace_store,
        model=model,
        node_id=best.node_id,
        client_ip=client_ip,
        original_format=_ORIGINAL_FORMAT,
        tags=list(_TAGS),
    )
    try:
        result = await proxy.embed_image_on_node(
            best.node_id,
            {"model": model, "images": images},
            timeout=settings.vision_embedding_timeout,
        )
    except httpx.HTTPStatusError as exc:
        # Downstream returned 4xx/5xx — preserve the real status code and
        # error body instead of wrapping everything as a 502.  Previously this
        # turned a 400 "missing 'images'" into a confusing 502 with the real
        # message buried in a stringified exception; clients couldn't tell a
        # malformed request from a backend failure.
        downstream_status = exc.response.status_code
        try:
            downstream_body = exc.response.json()
        except Exception:  # noqa: BLE001 — response isn't valid JSON
            downstream_body = {
                "error": exc.response.text[:500] or f"Embedding failed (HTTP {downstream_status})",
            }
        logger.warning(
            f"Vision embedding returned HTTP {downstream_status} from "
            f"{best.node_id}: {downstream_body}"
        )
        # Preserve node attribution so clients know where it failed
        if isinstance(downstream_body, dict):
            downstream_body.setdefault("node", best.node_id)
        # Same recording as native text embedding: the node answered with an
        # error, so it is a failed request on that node, 4xx included.
        trace("failed", error=f"HTTP {downstream_status}: {exc.response.text[:200]}")
        return JSONResponse(status_code=downstream_status, content=downstream_body)
    except Exception as exc:
        # Transport error (timeout, connection refused, etc) — genuinely a
        # bad-gateway condition, keep the 502.  httpx timeouts stringify to "",
        # so describe them rather than record a failure with no cause.
        detail = describe_exception(exc)
        logger.error(f"Vision embedding transport error on {best.node_id}: {detail}")
        trace("failed", error=detail)
        return JSONResponse(
            status_code=502,
            content={
                "error": f"Embedding failed: {detail}",
                "node": best.node_id,
            },
        )

    # No prompt_tokens: images have none, and recording a count here would put
    # vision models in front of the num_ctx checks, which read prompt_tokens > 0
    # and have no context window to manage for them.
    trace("completed")
    result["node"] = best.node_id
    return JSONResponse(
        content=result,
        headers=fleet_headers(
            node_id=best.node_id,
            served_model=model,
            requested_model=model,
            backend="vision",
        ),
    )
