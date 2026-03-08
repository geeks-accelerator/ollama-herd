"""OpenAI-compatible API endpoints."""

from __future__ import annotations

import asyncio
import json
import time
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from fleet_manager.models.request import InferenceRequest, QueueEntry, RequestFormat

router = APIRouter(tags=["openai"])

HOLD_TIMEOUT = 30.0  # Max seconds to wait for a node to become available
HOLD_RETRY_INTERVAL = 2.0


@router.get("/v1/models")
async def list_models(request: Request):
    """OpenAI-compatible model listing. Aggregates across all fleet nodes."""
    registry = request.app.state.registry
    models = set()
    for node in registry.get_online_nodes():
        if node.ollama:
            for m in node.ollama.models_loaded:
                models.add(m.name)
            for m in node.ollama.models_available:
                models.add(m)

    return {
        "object": "list",
        "data": [
            {
                "id": m,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "ollama",
            }
            for m in sorted(models)
        ],
    }


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """OpenAI-compatible chat completions with streaming support."""
    body = await request.json()
    model = body.get("model", "")
    if not model:
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "model is required", "type": "invalid_request_error"}},
        )

    inference_req = InferenceRequest(
        model=model,
        messages=body.get("messages", []),
        stream=body.get("stream", False),
        temperature=body.get("temperature", 0.7),
        max_tokens=body.get("max_tokens"),
        original_format=RequestFormat.OPENAI,
        raw_body=body,
    )

    scorer = request.app.state.scorer
    queue_mgr = request.app.state.queue_mgr
    proxy = request.app.state.streaming_proxy
    registry = request.app.state.registry

    # Holding queue: retry scoring until a node becomes available
    results = None
    deadline = time.time() + HOLD_TIMEOUT
    while time.time() < deadline:
        queue_depths = queue_mgr.get_queue_depths()
        results = scorer.score_request(inference_req.model, queue_depths)
        if results:
            break
        # Check if the model exists anywhere (even offline nodes)
        model_exists = any(
            model in (n.ollama.models_available if n.ollama else [])
            or model in [m.name for m in (n.ollama.models_loaded if n.ollama else [])]
            for n in registry.get_all_nodes()
        )
        if not model_exists:
            break  # Model doesn't exist at all, no point waiting
        await asyncio.sleep(HOLD_RETRY_INTERVAL)

    if not results:
        # Check if model exists on any node (including offline)
        all_models = set()
        for n in registry.get_all_nodes():
            if n.ollama:
                all_models.update(m.name for m in n.ollama.models_loaded)
                all_models.update(n.ollama.models_available)
        if model not in all_models:
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "message": f"Model '{model}' is not available on any node. "
                        f"Run 'ollama pull {model}' on a fleet device, then try again.",
                        "type": "model_not_found",
                    }
                },
            )
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "message": f"Model '{model}' exists but no node can serve it right now "
                    f"(all nodes offline or at capacity). Try again shortly.",
                    "type": "model_not_available",
                }
            },
        )

    winner = results[0]
    entry = QueueEntry(request=inference_req, assigned_node=winner.node_id)
    queue_key = winner.queue_key

    process_fn = proxy.make_process_fn(queue_key, queue_mgr)
    response_future = await queue_mgr.enqueue(entry, process_fn)
    stream = await response_future

    if inference_req.stream:
        async def _stream_and_cleanup():
            """Yield all chunks, then clean up token tracking."""
            async for chunk in stream:
                yield chunk
            # Streaming callers don't use token counts in the response,
            # so clean up the side-channel dict here.
            proxy._request_tokens.pop(inference_req.request_id, None)

        return StreamingResponse(
            _stream_and_cleanup(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Fleet-Node": winner.node_id,
                "X-Fleet-Score": str(int(winner.score)),
            },
        )
    else:
        # Non-streaming: accumulate full response
        full_content = ""
        async for chunk in stream:
            if chunk.startswith("data: ") and chunk.strip() != "data: [DONE]":
                try:
                    data = json.loads(chunk[6:])
                    delta = data.get("choices", [{}])[0].get("delta", {})
                    full_content += delta.get("content", "")
                except (json.JSONDecodeError, IndexError):
                    pass

        # Retrieve real token counts extracted from Ollama response
        tokens = proxy._request_tokens.pop(inference_req.request_id, (None, None))
        prompt_tok = tokens[0] or 0
        completion_tok = tokens[1] or 0

        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": full_content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tok,
                "completion_tokens": completion_tok,
                "total_tokens": prompt_tok + completion_tok,
            },
        }
