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
from typing import NamedTuple

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

# Activation memory for one ONNX run grows with batch x seq_len^2 (the attention
# scores), and ONNX Runtime never hands its high-water mark back to the OS: the
# largest run a process ever does is what it holds from then on, plus a second
# copy for each input shape it sees twice (memory patterns).  Unbounded, that
# pinned 28 GB in one node agent on 2026-10-02 (docs/observations.md).  The
# budget is one full-context nomic sequence (~1 GB measured), so long inputs
# run one at a time and short ones still batch _MAX_BATCH wide.
_ATTENTION_BUDGET = 2048 * 2048
_MAX_BATCH = 32

# The budget only holds one run at a time: concurrent runs on one session each
# take a full peak from the same arena, so four at once hold four peaks.  Keyed
# like _slots, so the embedder and the reranker still run in parallel.
_run_locks: dict[type, asyncio.Lock] = {}


async def _load(cls, fastembed_name: str, max_tokens: int | None = None):
    """Return a loaded ``cls(fastembed_name)``, loading or swapping as needed.

    ``max_tokens`` replaces the tokenizer's truncation limit, which fastembed
    takes from the model card — 8192 for nomic, four times the context Ollama
    serves it at, and sixteen times the attention memory.
    """
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
        def build():
            model = cls(
                model_name=fastembed_name,
                cache_dir=str(TEXT_EMBEDDING_CACHE_DIR),
                threads=_DEFAULT_THREADS,
                lazy_load=False,
            )
            if max_tokens:
                model.model.tokenizer.enable_truncation(max_length=max_tokens)
            return model

        loop = asyncio.get_running_loop()
        loaded = await loop.run_in_executor(None, build)
        _slots[cls] = (fastembed_name, loaded)
        logger.info(f"{cls.__name__} model loaded: {fastembed_name}")
        return loaded


async def _get_model(ollama_model_name: str):
    """Return the loaded fastembed TextEmbedding for an embedding model."""
    from fastembed import TextEmbedding

    return await _load(
        TextEmbedding,
        get_fastembed_name(ollama_model_name),
        max_tokens=TEXT_EMBEDDING_MODELS[ollama_model_name]["max_tokens"],
    )


async def _get_reranker(model: str):
    """Return the loaded fastembed TextCrossEncoder for a rerank model."""
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    return await _load(TextCrossEncoder, get_fastembed_name(model))


async def _run_onnx(backend, fn):
    """Run blocking ONNX work for ``backend`` in a thread, one call per model."""
    async with _run_locks.setdefault(type(backend), asyncio.Lock()):
        return await asyncio.get_running_loop().run_in_executor(None, fn)


class _Batching(NamedTuple):
    batch_size: int
    tokens: int
    truncated: bool


def _plan_batches(backend, inputs: list) -> _Batching:
    """Size ONNX batches to ``_ATTENTION_BUDGET`` with the model's own tokenizer.

    fastembed pads each batch to its longest member, so the request's longest
    input sets the cost of every batch.  Tokenizing up front costs little next
    to inference, and gives real token counts for the response.  ``inputs`` are
    strings, or ``(query, document)`` pairs for a cross-encoder.
    """
    encodings = backend.model.tokenizer.encode_batch(inputs)
    longest = max(len(e.ids) for e in encodings)
    return _Batching(
        batch_size=max(1, min(_MAX_BATCH, _ATTENTION_BUDGET // longest**2)),
        tokens=sum(len(e.ids) for e in encodings),
        truncated=any(e.overflowing for e in encodings),
    )


@router.post("/embed")
async def embed_text(request: Request):
    """Generate text embeddings for one or more strings.

    Request (Ollama /api/embed compatible):
        {
            "model": "nomic-embed-text",     // required
            "input": "text here",            // string or list[str]
            "prompt": "legacy field",        // also accepted
            "truncate": true                 // false: 400 instead of truncating
        }

    Response (Ollama /api/embed compatible):
        {
            "model": "nomic-embed-text",
            "embeddings": [[0.012, -0.034, ...]],
            "total_duration": 45123456,      // nanoseconds
            "load_duration": 0,
            "prompt_eval_count": 5
        }

    Inputs are truncated to the registry's ``max_tokens``, as Ollama truncates
    to the model's context, and ``prompt_eval_count`` is the tokens embedded.

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

    loop = asyncio.get_running_loop()
    plan = await loop.run_in_executor(None, _plan_batches, backend, texts)
    # Ollama truncates to the context by default and errors only on request.
    if plan.truncated and body.get("truncate") is False:
        limit = TEXT_EMBEDDING_MODELS[model]["max_tokens"]
        return JSONResponse(
            status_code=400,
            content={"error": f"input exceeds the context length ({limit} tokens)"},
        )

    # Run inference in thread pool — ONNX Runtime is blocking.
    start_ns = time.perf_counter_ns()
    try:
        embeddings_raw = await _run_onnx(
            backend, lambda: list(backend.embed(texts, batch_size=plan.batch_size)),
        )
    except Exception as exc:
        logger.error(f"Text embedding inference failed for model '{model}': {exc}")
        return JSONResponse(
            status_code=500,
            content={"error": f"Inference failed: {exc}"},
        )
    elapsed_ns = time.perf_counter_ns() - start_ns

    embeddings = [v.tolist() for v in embeddings_raw]

    logger.info(
        f"Text embed: {len(texts)} string(s) → {len(embeddings[0])}d "
        f"via {model} in {elapsed_ns // 1_000_000}ms"
    )

    return JSONResponse({
        "model": model,
        "embeddings": embeddings,
        "total_duration": elapsed_ns,
        "load_duration": 0,
        "prompt_eval_count": plan.tokens,
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

    loop = asyncio.get_running_loop()
    plan = await loop.run_in_executor(
        None, _plan_batches, backend, [(query, d) for d in documents],
    )
    start_ns = time.perf_counter_ns()
    try:
        logits = await _run_onnx(
            backend, lambda: list(backend.rerank(query, documents, batch_size=plan.batch_size)),
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

    logger.info(
        f"Rerank: {len(documents)} doc(s) via {model} in {elapsed_ns // 1_000_000}ms"
    )
    return JSONResponse({
        "model": model,
        "results": results,
        "usage": {"total_tokens": plan.tokens},
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
