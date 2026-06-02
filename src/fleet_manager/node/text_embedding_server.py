"""Native text embedding server — serves text embeddings via fastembed.

Runs on the node as a lightweight FastAPI app on port ollama_port+5 (11439).
Mirrors the structure of embedding_server.py (vision) but uses fastembed
(ONNX Runtime) instead of PIL+ONNX for text input.

Why this exists: Ollama's OLLAMA_NUM_PARALLEL limit means embed requests
queue behind LLM inference.  A 120B model can hold a slot for minutes while
embed requests (which take <100ms) pile up and time out.  This server runs
completely independently of Ollama — no shared queue, no contention.

Response format matches Ollama /api/embed exactly so clients need no changes.
"""

from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter
from fastapi.requests import Request
from fastapi.responses import JSONResponse

from fleet_manager.node.text_embedding_models import (
    TEXT_EMBEDDING_CACHE_DIR,
    TEXT_EMBEDDING_MODELS,
    get_fastembed_name,
    is_model_cached,
)

logger = logging.getLogger(__name__)

# Suppress filelock DEBUG chatter from fastembed's model-download path.
# filelock emits one DEBUG line per file lock acquired/released during the
# HuggingFace snapshot download, which floods herd-node.jsonl with hundreds
# of lines on first-run.  WARNING keeps "couldn't acquire lock" errors visible.
logging.getLogger("filelock").setLevel(logging.WARNING)

router = APIRouter()

# Module-level backend — loaded lazily on first request, swapped on model change.
# fastembed's TextEmbedding is thread-safe once loaded; asyncio.Lock guards
# the swap window so concurrent requests don't race on model loading.
_model = None
_model_fastembed_name: str = ""
_model_lock = asyncio.Lock()

# Default threads for ONNX Runtime — tune to M3 Ultra's performance-core count.
# fastembed default is all cores; 8 is a conservative starting point that
# leaves headroom for the two concurrent LLM inference processes.
_DEFAULT_THREADS = 8


async def _get_model(ollama_model_name: str):
    """Return a loaded fastembed TextEmbedding, loading or swapping as needed."""
    global _model, _model_fastembed_name

    fastembed_name = get_fastembed_name(ollama_model_name)

    # Fast path — already loaded, same model
    if _model is not None and _model_fastembed_name == fastembed_name:
        return _model

    async with _model_lock:
        # Re-check inside the lock (another coroutine may have loaded it)
        if _model is not None and _model_fastembed_name == fastembed_name:
            return _model

        logger.info(f"Loading text embedding model: {fastembed_name}")
        from fastembed import TextEmbedding

        TEXT_EMBEDDING_CACHE_DIR.mkdir(parents=True, exist_ok=True)

        # Run blocking model load in a thread pool so we don't block the event loop.
        # lazy_load=False so the model is fully loaded before the first request.
        loop = asyncio.get_running_loop()
        loaded = await loop.run_in_executor(
            None,
            lambda: TextEmbedding(
                model_name=fastembed_name,
                cache_dir=str(TEXT_EMBEDDING_CACHE_DIR),
                threads=_DEFAULT_THREADS,
                lazy_load=False,
            ),
        )
        _model = loaded
        _model_fastembed_name = fastembed_name
        logger.info(f"Text embedding model loaded: {fastembed_name}")
        return _model


@router.post("/embed")
async def embed_text(request: Request):
    """Generate text embeddings for one or more strings.

    Request (Ollama /api/embed compatible):
        {
            "model": "nomic-embed-text",     // required
            "input": "text here",            // string or list[str]
            "prompt": "legacy field"         // also accepted
        }

    Response (Ollama /api/embed compatible):
        {
            "model": "nomic-embed-text",
            "embeddings": [[0.012, -0.034, ...]],
            "total_duration": 45123456,      // nanoseconds
            "load_duration": 0,
            "prompt_eval_count": 5
        }

    Task prefixes (search_query:, search_document:, etc.) are the caller's
    responsibility — this server passes input through unchanged, identical to
    Ollama's behaviour.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body"})

    model = body.get("model", "nomic-embed-text")
    if model not in TEXT_EMBEDDING_MODELS:
        return JSONResponse(
            status_code=404,
            content={"error": f"Model '{model}' not in text embedding registry. "
                     f"Available: {sorted(TEXT_EMBEDDING_MODELS.keys())}"},
        )

    # Normalise input — Ollama accepts string or list[str] in the "input" field;
    # also accept "prompt" for legacy compat.
    raw_input = body.get("input") or body.get("prompt", "")
    if isinstance(raw_input, str):
        texts: list[str] = [raw_input] if raw_input else []
    else:
        texts = [str(t) for t in raw_input if t]

    if not texts:
        return JSONResponse(
            status_code=400,
            content={"error": "'input' is required and must be a non-empty string or list"},
        )

    try:
        backend = await _get_model(model)
    except Exception as exc:
        logger.error(f"Failed to load text embedding model '{model}': {exc}")
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to load model '{model}': {exc}"},
        )

    # Run inference in thread pool — ONNX Runtime is blocking.
    start_ns = time.perf_counter_ns()
    try:
        loop = asyncio.get_running_loop()
        embeddings_raw = await loop.run_in_executor(
            None,
            lambda: list(backend.embed(texts, batch_size=32)),
        )
    except Exception as exc:
        logger.error(f"Text embedding inference failed for model '{model}': {exc}")
        return JSONResponse(
            status_code=500,
            content={"error": f"Inference failed: {exc}"},
        )
    elapsed_ns = time.perf_counter_ns() - start_ns

    embeddings = [v.tolist() for v in embeddings_raw]
    prompt_eval_count = sum(len(t.split()) for t in texts)  # word-count approximation

    logger.info(
        f"Text embed: {len(texts)} string(s) → {len(embeddings[0])}d "
        f"via {model} in {elapsed_ns // 1_000_000}ms"
    )

    return JSONResponse({
        "model": model,
        "embeddings": embeddings,
        "total_duration": elapsed_ns,
        "load_duration": 0,
        "prompt_eval_count": prompt_eval_count,
    })


@router.get("/models")
async def list_models():
    """List registered text embedding models and their cache status."""
    models = []
    for name, spec in TEXT_EMBEDDING_MODELS.items():
        if name.endswith(":latest"):
            continue  # skip aliases
        models.append({
            "name": name,
            "fastembed_name": spec["fastembed_name"],
            "dimensions": spec["dimensions"],
            "max_tokens": spec["max_tokens"],
            "size_mb": spec["size_mb"],
            "cached": is_model_cached(name),
            "description": spec["description"],
        })
    return {"models": models}
