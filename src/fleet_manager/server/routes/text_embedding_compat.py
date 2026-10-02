"""Native text embedding routes — proxies text embed requests to the best node.

Clients hit POST /api/embed with a text embedding model name (e.g.,
"nomic-embed-text") and this route transparently forwards to whichever
node is running the fastembed server on port 11439, returning an
Ollama-compatible response.

Node selection prefers idle nodes with more available memory, mirroring
the vision embedding scoring in embedding_compat.py.

The public API surface is:
  is_text_embedding_model()     — re-exported for import in ollama_compat.py
  embed_text()                  — embed handler, reached via /api/embed
  proxy_to_native_text_server() — the shared proxy core; /v1/rerank uses it too
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from fleet_manager.node.text_embedding_models import get_model_spec, is_text_embedding_model
from fleet_manager.server.fleet_headers import fleet_headers

logger = logging.getLogger(__name__)

router = APIRouter(tags=["text-embedding"])

# Re-export so ollama_compat.py only needs one import from this module
__all__ = [
    "RERANK", "is_text_embedding_model", "embed_text",
    "proxy_to_native_text_server", "router",
]


def _score_text_embedding_candidates(candidates):
    """Score nodes for text embedding — prefer idle, more available memory."""
    scored = []
    for node in candidates:
        score = 0.0
        if node.text_embedding and node.text_embedding.processing:
            score -= 50.0
        if node.memory:
            score += node.memory.available_gb * 0.5
        if node.cpu:
            score -= node.cpu.utilization_pct * 0.2
        scored.append((score, node))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


@dataclass(frozen=True)
class _NativeKind:
    """What differs between the requests the native text server answers."""

    label: str                          # used in messages: "text embedding"
    path: str                           # node route
    original_format: str                # trace field
    tags: tuple[str, ...]               # trace tags
    serves: Callable                    # node -> bool: can this node answer?
    count_tokens: Callable              # backend result -> int | None
    node_in_body: bool                  # add "node" to the JSON body?


def _int_or_none(value) -> int | None:
    try:
        return int(value or 0) or None
    except (TypeError, ValueError):
        return None


EMBED = _NativeKind(
    label="text embedding",
    path="/embed",
    original_format="embed",
    tags=("embed", "text-embed"),
    serves=lambda n: n.text_embedding_port > 0,
    # The backend computes `prompt_eval_count` (a word-count approximation —
    # see node/text_embedding_server.py).  Before it was traced, every embed row
    # had prompt_tokens NULL, which made embed latency unexplainable: on
    # 2026-10-01 `nomic-embed-text:latest` averaged 324 ms and the bare name
    # 1,624 ms, same node, same backend, with no recorded way to tell whether
    # batch size was the reason.
    count_tokens=lambda result: _int_or_none(result.get("prompt_eval_count")),
    node_in_body=True,
)

RERANK = _NativeKind(
    label="rerank",
    path="/rerank",
    original_format="rerank",
    tags=("rerank",),
    # Only nodes that advertise a reranker: an older agent's server has no
    # /rerank route, so routing to it would 404 for no reason.
    serves=lambda n: n.text_embedding_port > 0 and bool(n.text_embedding) and any(
        getattr(m, "kind", "embed") == "rerank" for m in n.text_embedding.models_available
    ),
    count_tokens=lambda result: _int_or_none((result.get("usage") or {}).get("total_tokens")),
    # Jina/Cohere response shape — the node is in X-Fleet-Node, not the body,
    # so strict clients decoding the documented fields see nothing extra.
    node_in_body=False,
)


def _size_mb(model: str) -> int | None:
    """Download size of a known model — first requests fetch the weights."""
    try:
        return get_model_spec(model)["size_mb"]
    except KeyError:
        return None


async def proxy_to_native_text_server(
    request: Request, *, model: str, body: dict, kind: _NativeKind
) -> JSONResponse:
    """Send ``body`` to the best node's native fastembed server; trace the outcome.

    Shared by embedding (``embed_text``) and reranking (``/v1/rerank``): pick a
    node that ``kind.serves``, POST to ``kind.path``, and map timeouts (504),
    transport errors (502) and backend errors (passed through) the same way for
    both, leaving one trace per outcome.
    """
    registry = request.app.state.registry
    candidates = [n for n in registry.get_online_nodes() if kind.serves(n)]
    if not candidates:
        return JSONResponse(
            status_code=503,
            content={
                "error": (
                    f"No node is running the native {kind.label} server for '{model}'. "
                    "Install fastembed on a node: `uv sync --extra embedding`, "
                    "then restart herd-node."
                )
            },
        )

    best = _score_text_embedding_candidates(candidates)

    parsed = urlparse(best.ollama_base_url)
    host = parsed.hostname or "localhost"
    te_port = best.text_embedding_port or 11439
    te_url = f"http://{host}:{te_port}"

    logger.info(f"{kind.label.capitalize()}: model={model} → {best.node_id} ({te_url})")

    trace_store = request.app.state.trace_store
    client_ip = request.client.host if request.client else ""
    request_id = str(uuid.uuid4())
    start_ms = time.time() * 1000

    def trace(status: str, *, error: str | None = None, prompt_tokens=None) -> None:
        if trace_store:
            asyncio.ensure_future(trace_store.record_trace(
                request_id=request_id,
                model=model, original_model=model,
                node_id=best.node_id, score=None,
                status=status, latency_ms=time.time() * 1000 - start_ms,
                prompt_tokens=prompt_tokens,
                client_ip=client_ip, original_format=kind.original_format,
                error_message=error,
                tags=list(kind.tags),
            ))

    size = _size_mb(model)
    # Forward the request body as-is to the node's native server
    timeout = httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0)
    async with httpx.AsyncClient(base_url=te_url, timeout=timeout) as client:
        try:
            resp = await client.post(kind.path, json=body)
        except httpx.ReadTimeout:
            err_msg = "ReadTimeout — model may still be downloading" + (
                f" ({size} MB on first request)" if size else " on first request"
            )
            logger.error(f"{kind.label.capitalize()} timeout on {best.node_id} — {err_msg}")
            trace("failed", error=err_msg)
            return JSONResponse(
                status_code=504,
                content={
                    "error": (
                        f"{kind.label.capitalize()} timed out on {best.node_id}. "
                        "If this is the first request, the model"
                        + (f" ({size} MB)" if size else "")
                        + " may still be "
                        "downloading — retry in 30 seconds."
                    ),
                    "node": best.node_id,
                },
            )
        except Exception as exc:
            logger.error(f"{kind.label.capitalize()} transport error on {best.node_id}: {exc}")
            trace("failed", error=f"{type(exc).__name__}: {exc}")
            return JSONResponse(
                status_code=502,
                content={
                    "error": f"{kind.label.capitalize()} failed: {exc}",
                    "node": best.node_id,
                },
            )

    if not resp.is_success:
        try:
            downstream_body = resp.json()
        except Exception:
            downstream_body = {"error": resp.text[:500]}
        downstream_body.setdefault("node", best.node_id)
        trace("failed", error=f"HTTP {resp.status_code}: {resp.text[:200]}")
        return JSONResponse(status_code=resp.status_code, content=downstream_body)

    result = resp.json()
    trace("completed", prompt_tokens=kind.count_tokens(result))
    if kind.node_in_body:
        result["node"] = best.node_id
    return JSONResponse(
        content=result,
        headers=fleet_headers(
            node_id=best.node_id,
            served_model=model,
            requested_model=model,
            backend="native",
        ),
    )


@router.post("/api/embed-text")
async def embed_text(request: Request):
    """Proxy a text embedding request to the best node's fastembed server.

    Reached via ``ollama_compat.dispatch_embed`` when the requested model is in
    the text embedding registry — the caller sees the same Ollama-compatible
    JSON shape regardless of which path was taken.  (This module's router is
    not mounted, so the decorator path is not served on its own.)

    Request (Ollama /api/embed compatible):
        {
            "model": "nomic-embed-text",
            "input": "text or list of texts"
        }

    Response (Ollama /api/embed compatible):
        {
            "model": "nomic-embed-text",
            "embeddings": [[...]],
            "total_duration": 45123456,
            "load_duration": 0,
            "prompt_eval_count": 5,
            "node": "<node_id>"
        }
    """
    # Use cached body from /api/embed redirect, or parse fresh
    body = getattr(request.state, "_parsed_body", None)
    if body is None:
        body = await request.json()
    model = body.get("model", "nomic-embed-text")
    return await proxy_to_native_text_server(request, model=model, body=body, kind=EMBED)
