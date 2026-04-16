"""Vision embedding server — serves image embeddings via HTTP.

Runs on the node as a lightweight FastAPI app.  Supports DINOv2 (MLX)
and CLIP (ONNX) backends, auto-selected based on platform.
"""

from __future__ import annotations

import base64
import io
import logging
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from PIL import Image

logger = logging.getLogger(__name__)

router = APIRouter()

# Loaded backend — initialized on first request or at server start
_backend = None
_backend_model: str = ""


def _get_backend(model_name: str = ""):
    """Get or lazily load the embedding backend."""
    global _backend, _backend_model

    from fleet_manager.node.embedding_models import load_backend, select_default_model

    if not model_name:
        model_name = select_default_model()

    if _backend is not None and _backend_model == model_name:
        return _backend, _backend_model

    logger.info(f"Loading vision embedding backend: {model_name}")
    _backend = load_backend(model_name)
    _backend_model = model_name
    return _backend, _backend_model


def preload(model_name: str = "") -> None:
    """Pre-load a model backend at server startup."""
    _get_backend(model_name)


@router.post("/embed")
async def embed_images(request: Request):
    """Generate vision embeddings for one or more images.

    Request:
        {
            "model": "dinov2-vit-s14",  // optional
            "images": ["base64..."],     // required
            "normalize": true            // optional, default true
        }

    Response:
        {
            "model": "dinov2-vit-s14",
            "embeddings": [[0.123, ...], ...],
            "dimensions": 384
        }
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"error": "Invalid JSON body"},
            status_code=400,
        )

    images_b64 = body.get("images", [])
    if not images_b64 or not isinstance(images_b64, list):
        return JSONResponse(
            {"error": "Request must include 'images' as a list of base64 strings"},
            status_code=400,
        )

    model_name = body.get("model", "")

    try:
        backend, resolved_model = _get_backend(model_name)
    except Exception as exc:
        return JSONResponse(
            {"error": f"Failed to load model: {exc}"},
            status_code=500,
        )

    # Decode base64 images
    pil_images: list[Image.Image] = []
    for i, b64 in enumerate(images_b64):
        try:
            raw = base64.b64decode(b64)
            pil_images.append(Image.open(io.BytesIO(raw)))
        except Exception as exc:
            return JSONResponse(
                {"error": f"Failed to decode image {i}: {exc}"},
                status_code=400,
            )

    # Generate embeddings
    t0 = time.time()
    try:
        embeddings = backend.embed(pil_images)
    except Exception as exc:
        logger.error(f"Embedding inference failed: {exc}")
        return JSONResponse(
            {"error": f"Inference failed: {exc}"},
            status_code=500,
        )
    elapsed_ms = (time.time() - t0) * 1000

    logger.info(
        f"Embedded {len(pil_images)} image(s) with {resolved_model} "
        f"in {elapsed_ms:.0f}ms"
    )

    return {
        "model": resolved_model,
        "embeddings": embeddings.tolist(),
        "dimensions": backend.dimensions,
    }


@router.get("/models")
async def list_models():
    """List available vision embedding models on this node."""
    from fleet_manager.node.embedding_models import (
        VISION_EMBEDDING_MODELS,
        _mlx_available,
        is_model_downloaded,
    )

    mlx_ok = _mlx_available()
    models = []
    for name, spec in VISION_EMBEDDING_MODELS.items():
        if spec["runtime"] == "mlx" and not mlx_ok:
            continue
        models.append({
            "name": name,
            "runtime": spec["runtime"],
            "dimensions": spec["dimensions"],
            "downloaded": is_model_downloaded(name),
            "size_mb": spec["size_mb"],
            "description": spec["description"],
        })
    return {"models": models}
