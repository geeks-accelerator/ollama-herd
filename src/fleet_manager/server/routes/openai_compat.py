"""OpenAI-compatible API endpoints."""

from __future__ import annotations

import base64
import json
import logging
import time
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from fleet_manager.models.request import InferenceRequest, QueueEntry, RequestFormat
from fleet_manager.server.routes.routing import (
    check_context_overflow,
    extract_tags,
    get_all_fleet_models,
    score_with_fallbacks,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["openai"])


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
        # Include image models (mflux + DiffusionKit)
        if node.image:
            for m in node.image.models_available:
                models.add(m.name)

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

    tags = extract_tags(body, request.headers)
    logger.info(
        f"OpenAI request: model={model} stream={body.get('stream', False)}"
        + (f" tags={tags}" if tags else "")
    )

    inference_req = InferenceRequest(
        model=model,
        original_model=model,
        fallback_models=body.get("fallback_models", []),
        messages=body.get("messages", []),
        stream=body.get("stream", False),
        temperature=body.get("temperature", 0.7),
        max_tokens=body.get("max_tokens"),
        original_format=RequestFormat.OPENAI,
        raw_body=body,
        tags=tags,
    )

    scorer = request.app.state.scorer
    queue_mgr = request.app.state.queue_mgr
    proxy = request.app.state.streaming_proxy
    registry = request.app.state.registry
    settings = request.app.state.settings

    # Score with fallback support + auto-pull
    results, actual_model = await score_with_fallbacks(
        inference_req, scorer, queue_mgr, registry,
        proxy=proxy, settings=settings,
    )

    if not results:
        # Build error listing all attempted models
        logger.warning(f"No nodes for model={model} fallbacks={inference_req.fallback_models}")
        models_tried = [model] + inference_req.fallback_models
        all_fleet_models = get_all_fleet_models(registry)
        any_exists = any(m in all_fleet_models for m in models_tried)

        if not any_exists:
            models_str = "', '".join(models_tried)
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "message": f"Model(s) '{models_str}' not available on any node. "
                        f"Run 'ollama pull <model>' on a fleet device, then try again.",
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

    process_fn = proxy.make_process_fn(queue_key, queue_mgr, scorer=scorer, settings=settings)
    response_future = await queue_mgr.enqueue(entry, process_fn)
    stream = await response_future

    # Build response headers
    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Fleet-Node": winner.node_id,
        "X-Fleet-Score": str(int(winner.score)),
    }
    if fallback_used:
        headers["X-Fleet-Fallback"] = actual_model
    if entry.retry_count > 0:
        headers["X-Fleet-Retries"] = str(entry.retry_count)
    headers.update(check_context_overflow(winner, inference_req, registry))

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
            headers=headers,
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
                except (json.JSONDecodeError, IndexError) as e:
                    logger.debug(f"Skipping malformed SSE chunk: {e}")

        # Retrieve real token counts extracted from Ollama response
        tokens = proxy._request_tokens.pop(inference_req.request_id, (None, None))
        prompt_tok = tokens[0] or 0
        completion_tok = tokens[1] or 0

        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": actual_model,
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


@router.post("/v1/images/generations")
async def openai_images_generations(request: Request):
    """OpenAI-compatible image generation endpoint.

    Wraps the fleet's /api/generate-image and returns the response
    in OpenAI's image API format (base64 JSON or raw PNG).
    """
    body = await request.json()
    model = body.get("model", "")
    prompt = body.get("prompt", "")
    if not model:
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "model is required", "type": "invalid_request_error"}},
        )
    if not prompt:
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "prompt is required", "type": "invalid_request_error"}},
        )

    # Map OpenAI parameters to our image endpoint parameters
    size = body.get("size", "1024x1024")
    width, height = (int(x) for x in size.split("x")) if "x" in size else (1024, 1024)
    response_format = body.get("response_format", "b64_json")

    # Forward to the internal image generation endpoint
    from fleet_manager.server.routes.image_compat import generate_image

    image_body = {
        "model": model,
        "prompt": prompt,
        "width": width,
        "height": height,
    }
    # Pass through optional params
    for key in ("steps", "guidance", "seed", "negative_prompt"):
        if key in body:
            image_body[key] = body[key]

    request._body = json.dumps(image_body).encode()
    image_response = await generate_image(request)

    # If the image endpoint returned an error, pass it through in OpenAI format
    if hasattr(image_response, "status_code") and image_response.status_code >= 400:
        error_body = image_response.body.decode() if hasattr(image_response, "body") else "{}"
        try:
            error_data = json.loads(error_body)
            error_msg = error_data.get("error", "Image generation failed")
        except (json.JSONDecodeError, AttributeError):
            error_msg = "Image generation failed"
        return JSONResponse(
            status_code=image_response.status_code,
            content={"error": {"message": str(error_msg), "type": "server_error"}},
        )

    # Extract PNG bytes from the response
    png_bytes = image_response.body if hasattr(image_response, "body") else b""

    if response_format == "b64_json":
        return {
            "created": int(time.time()),
            "data": [
                {
                    "b64_json": base64.b64encode(png_bytes).decode(),
                    "revised_prompt": prompt,
                }
            ],
        }
    else:
        # Return raw PNG for "url" format (we don't host URLs, so return the image directly)
        return Response(
            content=png_bytes,
            media_type="image/png",
            headers={"X-Fleet-Model": model},
        )
