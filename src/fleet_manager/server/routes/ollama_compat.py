"""Ollama-compatible API endpoints."""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from fleet_manager.models.request import InferenceRequest, QueueEntry, RequestFormat

router = APIRouter(tags=["ollama"])

HOLD_TIMEOUT = 30.0
HOLD_RETRY_INTERVAL = 2.0


@router.post("/api/chat")
async def ollama_chat(request: Request):
    """Ollama-compatible chat endpoint. Routes to best available node."""
    body = await request.json()
    model = body.get("model", "")
    if not model:
        return JSONResponse(status_code=400, content={"error": "model is required"})

    inference_req = InferenceRequest(
        model=model,
        messages=body.get("messages", []),
        stream=body.get("stream", True),
        temperature=body.get("options", {}).get("temperature", 0.7),
        max_tokens=body.get("options", {}).get("num_predict"),
        original_format=RequestFormat.OLLAMA,
        raw_body=body,
    )

    return await _route_and_stream(request, inference_req)


@router.post("/api/generate")
async def ollama_generate(request: Request):
    """Ollama-compatible generate endpoint."""
    body = await request.json()
    model = body.get("model", "")
    if not model:
        return JSONResponse(status_code=400, content={"error": "model is required"})

    prompt = body.get("prompt", "")
    messages = [{"role": "user", "content": prompt}] if prompt else []

    inference_req = InferenceRequest(
        model=model,
        messages=messages,
        stream=body.get("stream", True),
        temperature=body.get("options", {}).get("temperature", 0.7),
        max_tokens=body.get("options", {}).get("num_predict"),
        original_format=RequestFormat.OLLAMA,
        raw_body=body,
    )

    return await _route_and_stream(request, inference_req)


@router.get("/api/tags")
async def ollama_tags(request: Request):
    """Ollama-compatible: list all models across the fleet."""
    registry = request.app.state.registry
    seen = {}
    for node in registry.get_online_nodes():
        if not node.ollama:
            continue
        for m in node.ollama.models_loaded:
            if m.name not in seen:
                seen[m.name] = {
                    "name": m.name,
                    "model": m.name,
                    "size": int(m.size_gb * (1024**3)),
                    "details": {"fleet_nodes": [node.node_id]},
                }
            else:
                seen[m.name]["details"]["fleet_nodes"].append(node.node_id)
        for name in node.ollama.models_available:
            if name not in seen:
                seen[name] = {
                    "name": name,
                    "model": name,
                    "size": 0,
                    "details": {"fleet_nodes": [node.node_id]},
                }
            elif node.node_id not in seen[name]["details"]["fleet_nodes"]:
                seen[name]["details"]["fleet_nodes"].append(node.node_id)

    return {"models": list(seen.values())}


@router.get("/api/ps")
async def ollama_ps(request: Request):
    """Fleet-wide: all currently loaded models across all nodes."""
    registry = request.app.state.registry
    models = []
    for node in registry.get_online_nodes():
        if not node.ollama:
            continue
        for m in node.ollama.models_loaded:
            models.append({
                "name": m.name,
                "model": m.name,
                "size": int(m.size_gb * (1024**3)),
                "fleet_node": node.node_id,
            })
    return {"models": models}


async def _route_and_stream(request: Request, inference_req: InferenceRequest):
    """Shared routing logic for Ollama endpoints with holding queue."""
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
        model_exists = any(
            inference_req.model in (n.ollama.models_available if n.ollama else [])
            or inference_req.model in [m.name for m in (n.ollama.models_loaded if n.ollama else [])]
            for n in registry.get_all_nodes()
        )
        if not model_exists:
            break
        await asyncio.sleep(HOLD_RETRY_INTERVAL)

    if not results:
        all_models = set()
        for n in registry.get_all_nodes():
            if n.ollama:
                all_models.update(m.name for m in n.ollama.models_loaded)
                all_models.update(n.ollama.models_available)
        if inference_req.model not in all_models:
            return JSONResponse(
                status_code=404,
                content={
                    "error": f"model '{inference_req.model}' not found on any node. "
                    f"Run 'ollama pull {inference_req.model}' on a fleet device."
                },
            )
        return JSONResponse(
            status_code=503,
            content={
                "error": f"model '{inference_req.model}' exists but no node can serve it "
                f"right now. Try again shortly."
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
            async for chunk in stream:
                yield chunk
            proxy._request_tokens.pop(inference_req.request_id, None)

        return StreamingResponse(
            _stream_and_cleanup(),
            media_type="application/x-ndjson",
            headers={
                "X-Fleet-Node": winner.node_id,
                "X-Fleet-Score": str(int(winner.score)),
            },
        )
    else:
        # Non-streaming: accumulate full response, consume entire stream
        import json

        full_response = ""
        final_data = None
        async for chunk in stream:
            chunk = chunk.strip()
            if chunk:
                try:
                    data = json.loads(chunk)
                    full_response += data.get("message", {}).get("content", "")
                    full_response += data.get("response", "")
                    if data.get("done"):
                        final_data = data
                except json.JSONDecodeError:
                    pass

        # Clean up token tracking
        proxy._request_tokens.pop(inference_req.request_id, None)

        if final_data:
            final_data["message"] = {"role": "assistant", "content": full_response}
            return final_data
        return {"message": {"role": "assistant", "content": full_response}, "done": True}
