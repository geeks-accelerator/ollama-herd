"""Ollama-compatible API endpoints."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from fleet_manager.models.request import InferenceRequest, QueueEntry, RequestFormat
from fleet_manager.server.routes.routing import (
    extract_tags,
    get_all_fleet_models,
    score_with_fallbacks,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["ollama"])


@router.post("/api/chat")
async def ollama_chat(request: Request):
    """Ollama-compatible chat endpoint. Routes to best available node."""
    body = await request.json()
    model = body.get("model", "")
    if not model:
        return JSONResponse(status_code=400, content={"error": "model is required"})

    tags = extract_tags(body, request.headers)
    inference_req = InferenceRequest(
        model=model,
        original_model=model,
        fallback_models=body.get("fallback_models", []),
        messages=body.get("messages", []),
        stream=body.get("stream", True),
        temperature=body.get("options", {}).get("temperature", 0.7),
        max_tokens=body.get("options", {}).get("num_predict"),
        original_format=RequestFormat.OLLAMA,
        raw_body=body,
        tags=tags,
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

    tags = extract_tags(body, request.headers)
    inference_req = InferenceRequest(
        model=model,
        original_model=model,
        fallback_models=body.get("fallback_models", []),
        messages=messages,
        stream=body.get("stream", True),
        temperature=body.get("options", {}).get("temperature", 0.7),
        max_tokens=body.get("options", {}).get("num_predict"),
        original_format=RequestFormat.OLLAMA,
        raw_body=body,
        tags=tags,
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
    """Shared routing logic for Ollama endpoints with holding queue + fallbacks."""
    scorer = request.app.state.scorer
    queue_mgr = request.app.state.queue_mgr
    proxy = request.app.state.streaming_proxy
    registry = request.app.state.registry
    settings = request.app.state.settings
    model = inference_req.original_model or inference_req.model
    logger.info(f"Ollama request: model={model} stream={inference_req.stream}")

    # Score with fallback support
    results, actual_model = await score_with_fallbacks(
        inference_req, scorer, queue_mgr, registry
    )

    if not results:
        logger.warning(
            f"No nodes for model={model} fallbacks={inference_req.fallback_models}"
        )
        models_tried = [model] + inference_req.fallback_models
        all_fleet_models = get_all_fleet_models(registry)
        any_exists = any(m in all_fleet_models for m in models_tried)

        if not any_exists:
            models_str = "', '".join(models_tried)
            return JSONResponse(
                status_code=404,
                content={
                    "error": f"model(s) '{models_str}' not found on any node. "
                    f"Run 'ollama pull <model>' on a fleet device."
                },
            )
        return JSONResponse(
            status_code=503,
            content={
                "error": f"model '{model}' exists but no node can serve it "
                f"right now. Try again shortly."
            },
        )

    # Apply fallback if a different model was selected
    fallback_used = actual_model != model
    if fallback_used:
        inference_req.model = actual_model
        if "model" in inference_req.raw_body:
            inference_req.raw_body["model"] = actual_model

    winner = results[0]
    entry = QueueEntry(
        request=inference_req,
        assigned_node=winner.node_id,
        routing_score=winner.score,
        routing_breakdown=winner.scores_breakdown,
        fallback_used=fallback_used,
    )
    queue_key = winner.queue_key

    process_fn = proxy.make_process_fn(
        queue_key, queue_mgr, scorer=scorer, settings=settings
    )
    response_future = await queue_mgr.enqueue(entry, process_fn)
    stream = await response_future

    # Build response headers
    headers = {
        "X-Fleet-Node": winner.node_id,
        "X-Fleet-Score": str(int(winner.score)),
    }
    if fallback_used:
        headers["X-Fleet-Fallback"] = actual_model
    if entry.retry_count > 0:
        headers["X-Fleet-Retries"] = str(entry.retry_count)

    if inference_req.stream:
        async def _stream_and_cleanup():
            async for chunk in stream:
                yield chunk
            proxy._request_tokens.pop(inference_req.request_id, None)

        return StreamingResponse(
            _stream_and_cleanup(),
            media_type="application/x-ndjson",
            headers=headers,
        )
    else:
        # Non-streaming: accumulate full response, consume entire stream
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
                except json.JSONDecodeError as e:
                    logger.debug(f"Skipping malformed Ollama chunk: {e}")

        # Clean up token tracking
        proxy._request_tokens.pop(inference_req.request_id, None)

        if final_data:
            final_data["message"] = {"role": "assistant", "content": full_response}
            return final_data
        return {"message": {"role": "assistant", "content": full_response}, "done": True}
