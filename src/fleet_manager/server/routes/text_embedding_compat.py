"""Native text embedding routes — proxies text embed requests to the best node.

Clients hit POST /api/embed with a text embedding model name (e.g.,
"nomic-embed-text") and this route transparently forwards to whichever
node is running the fastembed server on port 11439, returning an
Ollama-compatible response.

Node selection prefers idle nodes with more available memory, mirroring
the vision embedding scoring in embedding_compat.py.

The public API surface is:
  is_text_embedding_model()  — re-exported for import in ollama_compat.py
  embed_text()               — FastAPI route handler
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from fleet_manager.node.text_embedding_models import is_text_embedding_model

logger = logging.getLogger(__name__)

router = APIRouter(tags=["text-embedding"])

# Re-export so ollama_compat.py only needs one import from this module
__all__ = ["is_text_embedding_model", "embed_text", "router"]


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


@router.post("/api/embed-text")
async def embed_text(request: Request):
    """Proxy a text embedding request to the best node's fastembed server.

    This endpoint is also called internally by ollama_compat.ollama_embed()
    when the requested model is in the text embedding registry — the caller
    sees the same Ollama-compatible JSON shape regardless of which path was
    taken.

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
            "prompt_eval_count": 5
        }
    """
    # Use cached body from /api/embed redirect, or parse fresh
    body = getattr(request.state, "_parsed_body", None)
    if body is None:
        body = await request.json()

    model = body.get("model", "nomic-embed-text")

    registry = request.app.state.registry

    # Find nodes with text embedding server running
    candidates = [
        n
        for n in registry.get_online_nodes()
        if n.text_embedding_port > 0
    ]

    if not candidates:
        return JSONResponse(
            status_code=503,
            content={
                "error": (
                    f"No node is running the native text embedding server for '{model}'. "
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

    logger.info(
        f"Text embedding: model={model} → {best.node_id} "
        f"({te_url})"
    )

    # Forward the request body as-is to the node's text embedding server
    timeout = httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0)
    async with httpx.AsyncClient(base_url=te_url, timeout=timeout) as client:
        try:
            resp = await client.post("/embed", json=body)
        except httpx.ReadTimeout:
            logger.error(
                f"Text embedding timeout on {best.node_id} — "
                f"model may still be downloading (130 MB on first request)"
            )
            return JSONResponse(
                status_code=504,
                content={
                    "error": (
                        f"Text embedding timed out on {best.node_id}. "
                        "If this is the first request, the model (130 MB) may still be "
                        "downloading — retry in 30 seconds."
                    ),
                    "node": best.node_id,
                },
            )
        except Exception as exc:
            logger.error(f"Text embedding transport error on {best.node_id}: {exc}")
            return JSONResponse(
                status_code=502,
                content={
                    "error": f"Text embedding failed: {exc}",
                    "node": best.node_id,
                },
            )

    if not resp.is_success:
        try:
            downstream_body = resp.json()
        except Exception:
            downstream_body = {"error": resp.text[:500]}
        downstream_body.setdefault("node", best.node_id)
        return JSONResponse(status_code=resp.status_code, content=downstream_body)

    result = resp.json()
    result["node"] = best.node_id
    return JSONResponse(
        content=result,
        headers={"X-Fleet-Node": best.node_id},
    )
