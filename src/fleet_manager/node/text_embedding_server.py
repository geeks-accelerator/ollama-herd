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
import math
import time

from fastapi import APIRouter
from fastapi.requests import Request
from fastapi.responses import JSONResponse

from fleet_manager.node.text_embedding_models import (
    DEFAULT_RERANK_MODEL,
    RERANK_MODELS,
    TEXT_EMBEDDING_CACHE_DIR,
    TEXT_EMBEDDING_MODELS,
    get_fastembed_name,
    is_model_cached,
    is_rerank_model,
)

logger = logging.getLogger(__name__)

# Suppress filelock DEBUG chatter from fastembed's model-download path.
# filelock emits one DEBUG line per file lock acquired/released during the
# HuggingFace snapshot download, which floods herd-node.jsonl with hundreds
# of lines on first-run.  WARNING keeps "couldn't acquire lock" errors visible.
logging.getLogger("filelock").setLevel(logging.WARNING)

router = APIRouter()

# One loaded model per fastembed class, loaded lazily on first request and
# swapped only by a request for another model *of the same class* — so loading
# a reranker never evicts the embedder.  fastembed models are thread-safe once
# loaded; the asyncio.Lock guards the swap window so concurrent requests don't
# race on loading.
_slots: dict[type, tuple[str, object]] = {}
_load_lock = asyncio.Lock()

# Default threads for ONNX Runtime — tune to M3 Ultra's performance-core count.
# fastembed default is all cores; 8 is a conservative starting point that
# leaves headroom for the two concurrent LLM inference processes.
_DEFAULT_THREADS = 8


async def _load(cls, fastembed_name: str):
    """Return a loaded ``cls(fastembed_name)``, loading or swapping as needed."""
    slot = _slots.get(cls)
    if slot is not None and slot[0] == fastembed_name:
        return slot[1]  # fast path — already loaded

    async with _load_lock:
        # Re-check inside the lock (another coroutine may have loaded it)
        slot = _slots.get(cls)
        if slot is not None and slot[0] == fastembed_name:
            return slot[1]

        logger.info(f"Loading {cls.__name__} model: {fastembed_name}")
        TEXT_EMBEDDING_CACHE_DIR.mkdir(parents=True, exist_ok=True)

        # Run blocking model load in a thread pool so we don't block the event loop.
        # lazy_load=False so the model is fully loaded before the first request.
        loop = asyncio.get_running_loop()
        loaded = await loop.run_in_executor(
            None,
            lambda: cls(
                model_name=fastembed_name,
                cache_dir=str(TEXT_EMBEDDING_CACHE_DIR),
                threads=_DEFAULT_THREADS,
                lazy_load=False,
            ),
        )
        _slots[cls] = (fastembed_name, loaded)
        logger.info(f"{cls.__name__} model loaded: {fastembed_name}")
        return loaded


async def _get_model(ollama_model_name: str):
    """Return the loaded fastembed TextEmbedding for an embedding model."""
    from fastembed import TextEmbedding

    return await _load(TextEmbedding, get_fastembed_name(ollama_model_name))


async def _get_reranker(model: str):
    """Return the loaded fastembed TextCrossEncoder for a rerank model."""
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    return await _load(TextCrossEncoder, get_fastembed_name(model))


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


# A cross-encoder runs one forward pass per document, unlike an embedder, so an
# unbounded list is how a single request takes the node down.  Rejected with a
# clean 400 instead.
MAX_RERANK_DOCUMENTS = 1000
MAX_RERANK_DOCUMENT_CHARS = 32_000


@router.post("/rerank")
async def rerank(request: Request):
    """Score documents against a query with a cross-encoder (Jina/Cohere shape).

    Request:
        {
            "model": "bge-reranker-base",   // optional, default ms-marco-minilm-l-6-v2
            "query": "how do I stop herd?",
            "documents": ["...", "..."],    // strings, or {"text": "..."} objects
            "top_n": 3,                     // optional
            "return_documents": false       // optional
        }

    Response:
        {
            "model": "bge-reranker-base",
            "results": [{"index": 1, "relevance_score": 0.97, "document"?: {"text": ...}}],
            "usage": {"total_tokens": 42}
        }

    ``relevance_score`` is the cross-encoder's logit passed through a sigmoid:
    same ordering, but in 0..1 like Cohere's and Jina's, so clients that apply
    a relevance threshold (Open WebUI does) behave as they would against those.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body"})

    model = (body.get("model") or DEFAULT_RERANK_MODEL).lower().strip()
    if not is_rerank_model(model):
        return JSONResponse(
            status_code=404,
            content={"error": f"Model '{model}' not in rerank registry. "
                     f"Available: {sorted(RERANK_MODELS)}"},
        )

    query = body.get("query")
    if not isinstance(query, str) or not query.strip():
        return JSONResponse(
            status_code=400, content={"error": "'query' must be a non-empty string"},
        )
    raw_docs = body.get("documents")
    if not isinstance(raw_docs, list) or not raw_docs:
        return JSONResponse(
            status_code=400, content={"error": "'documents' must be a non-empty list"},
        )
    documents: list[str] = []
    for d in raw_docs:
        text = d.get("text") if isinstance(d, dict) else d
        if not isinstance(text, str):
            return JSONResponse(
                status_code=400,
                content={"error": "each document must be a string or {\"text\": string}"},
            )
        documents.append(text)
    if len(documents) > MAX_RERANK_DOCUMENTS:
        return JSONResponse(
            status_code=400,
            content={"error": f"at most {MAX_RERANK_DOCUMENTS} documents per request"},
        )
    if any(len(t) > MAX_RERANK_DOCUMENT_CHARS for t in documents):
        return JSONResponse(
            status_code=400,
            content={"error": f"each document must be at most "
                     f"{MAX_RERANK_DOCUMENT_CHARS} characters"},
        )
    top_n = body.get("top_n")
    if top_n is not None and (not isinstance(top_n, int) or top_n < 1):
        return JSONResponse(
            status_code=400, content={"error": "'top_n' must be a positive integer"},
        )

    try:
        backend = await _get_reranker(model)
    except Exception as exc:
        logger.error(f"Failed to load rerank model '{model}': {exc}")
        return JSONResponse(
            status_code=500, content={"error": f"Failed to load model '{model}': {exc}"},
        )

    start_ns = time.perf_counter_ns()
    try:
        loop = asyncio.get_running_loop()
        logits = await loop.run_in_executor(
            None, lambda: list(backend.rerank(query, documents, batch_size=32)),
        )
    except Exception as exc:
        logger.error(f"Rerank inference failed for model '{model}': {exc}")
        return JSONResponse(status_code=500, content={"error": f"Inference failed: {exc}"})
    elapsed_ns = time.perf_counter_ns() - start_ns

    ranked = sorted(
        ((i, 1.0 / (1.0 + math.exp(-float(x)))) for i, x in enumerate(logits)),
        key=lambda r: r[1],
        reverse=True,
    )
    if top_n is not None:
        ranked = ranked[:top_n]
    with_docs = bool(body.get("return_documents"))
    results = [
        {"index": i, "relevance_score": score}
        | ({"document": {"text": documents[i]}} if with_docs else {})
        for i, score in ranked
    ]
    # Word-count approximation, as /embed reports: each pair is query + document.
    q_words = len(query.split())
    total_tokens = sum(q_words + len(t.split()) for t in documents)

    logger.info(
        f"Rerank: {len(documents)} doc(s) via {model} in {elapsed_ns // 1_000_000}ms"
    )
    return JSONResponse({
        "model": model,
        "results": results,
        "usage": {"total_tokens": total_tokens},
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
