"""Ollama-compatible API endpoints."""

from __future__ import annotations

import json
import logging
import time

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from fleet_manager.models.request import InferenceRequest, QueueEntry, RequestFormat
from fleet_manager.server.model_knowledge import is_image_model
from fleet_manager.server.routes.routing import (
    check_context_overflow,
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

    # Detect Ollama native image generation models
    image_model = is_image_model(model)

    # Prefer mflux over Ollama native for image generation.
    # mflux runs as a separate subprocess and doesn't evict LLMs from Ollama's VRAM.
    if image_model:
        registry = request.app.state.registry
        # Map Ollama native model names to their mflux equivalents
        _OLLAMA_TO_MFLUX = {
            "x/z-image-turbo": "z-image-turbo",
            "x/z-image-turbo:latest": "z-image-turbo",
        }
        mflux_model = _OLLAMA_TO_MFLUX.get(model)
        if mflux_model:
            # Check if any node has this model via mflux (image server on port 11436)
            mflux_available = any(
                n.image
                and n.image_port > 0
                and any(m.name == mflux_model for m in n.image.models_available)
                for n in registry.get_online_nodes()
            )
            if mflux_available:
                logger.info(
                    f"Preferring mflux '{mflux_model}' over Ollama native '{model}' "
                    f"(avoids LLM eviction from VRAM)"
                )
                # Redirect to the image endpoint with the mflux model name
                from fleet_manager.server.routes.image_compat import generate_image

                image_body = {
                    "model": mflux_model,
                    "prompt": prompt,
                }
                # Forward image-specific params
                for key in ("width", "height", "steps", "guidance", "seed",
                            "negative_prompt", "quantize"):
                    val = body.get(key)
                    if val is not None:
                        image_body[key] = val
                request._body = __import__("json").dumps(image_body).encode()
                return await generate_image(request)

    tags = extract_tags(body, request.headers)
    inference_req = InferenceRequest(
        model=model,
        original_model=model,
        fallback_models=body.get("fallback_models", []),
        messages=messages,
        stream=False if image_model else body.get("stream", True),
        temperature=body.get("options", {}).get("temperature", 0.7),
        max_tokens=body.get("options", {}).get("num_predict"),
        original_format=RequestFormat.OLLAMA,
        raw_body=body,
        tags=tags,
        request_type="image" if image_model else "text",
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

    # Include image models (mflux + DiffusionKit) in the unified list
    for node in registry.get_online_nodes():
        if not node.image:
            continue
        for m in node.image.models_available:
            if m.name not in seen:
                seen[m.name] = {
                    "name": m.name,
                    "model": m.name,
                    "size": 0,
                    "details": {"fleet_nodes": [node.node_id], "type": "image"},
                }
            elif node.node_id not in seen[m.name]["details"].get("fleet_nodes", []):
                seen[m.name]["details"]["fleet_nodes"].append(node.node_id)

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
            models.append(
                {
                    "name": m.name,
                    "model": m.name,
                    "size": int(m.size_gb * (1024**3)),
                    "fleet_node": node.node_id,
                }
            )
    return {"models": models}


@router.post("/api/embed")
@router.post("/api/embeddings")
async def ollama_embed(request: Request):
    """Ollama-compatible embeddings endpoint. Routes to best node with the model.

    Unlike chat/generate, embeddings are non-streaming — we proxy the request
    directly to Ollama's /api/embed endpoint and return the JSON response.
    """
    body = await request.json()
    model = body.get("model", "")
    if not model:
        return JSONResponse(status_code=400, content={"error": "model is required"})

    tags = extract_tags(body, request.headers)
    inference_req = InferenceRequest(
        model=model,
        original_model=model,
        messages=[],
        stream=False,
        original_format=RequestFormat.OLLAMA,
        raw_body=body,
        tags=tags,
        request_type="embed",
    )

    scorer = request.app.state.scorer
    queue_mgr = request.app.state.queue_mgr
    proxy = request.app.state.streaming_proxy
    registry = request.app.state.registry
    settings = request.app.state.settings

    results, actual_model = await score_with_fallbacks(
        inference_req, scorer, queue_mgr, registry,
        proxy=proxy, settings=settings,
    )

    if not results:
        all_fleet_models = get_all_fleet_models(registry)
        if model not in all_fleet_models:
            return JSONResponse(
                status_code=404,
                content={"error": f"model '{model}' not found on any node. "
                         f"Run 'ollama pull {model}' on a fleet device."},
            )
        return JSONResponse(
            status_code=503,
            content={"error": f"model '{model}' exists but no node can serve it "
                     f"right now. Try again shortly."},
        )

    winner = results[0]
    node = registry.get_node(winner.node_id)
    if not node:
        return JSONResponse(status_code=503, content={"error": "Selected node unavailable"})

    # Proxy directly to Ollama's /api/embed endpoint using the proxy's
    # managed HTTP client (handles LAN IP rewriting, connection pooling).
    embed_body = dict(body)
    embed_body.pop("metadata", None)
    embed_body.setdefault("keep_alive", -1)

    try:
        client = proxy._get_client(winner.node_id)
        start = time.time()
        resp = await client.post(
            "/api/embed", json=embed_body,
            timeout=httpx.Timeout(connect=10.0, read=600.0, write=10.0, pool=10.0),
        )
        elapsed_ms = (time.time() - start) * 1000

        resp.raise_for_status()
        result = resp.json()

        logger.info(
            f"Embed {inference_req.request_id[:8]} completed on {winner.node_id} "
            f"in {elapsed_ms:.0f}ms model={actual_model}"
        )

        return JSONResponse(
            content=result,
            headers={
                "X-Fleet-Node": winner.node_id,
                "X-Fleet-Score": str(int(winner.score)),
            },
        )
    except httpx.HTTPStatusError as e:
        logger.error(f"Embed failed on {winner.node_id}: HTTP {e.response.status_code}")
        return JSONResponse(
            status_code=e.response.status_code,
            content={"error": f"Ollama returned {e.response.status_code}: "
                     f"{e.response.text[:200]}"},
        )
    except Exception as e:
        error_detail = str(e) or repr(e)
        logger.error(f"Embed failed on {winner.node_id}: {type(e).__name__}: {error_detail}")
        return JSONResponse(
            status_code=502,
            content={"error": f"Failed to reach Ollama on {winner.node_id}: "
                     f"{type(e).__name__}: {error_detail}"},
        )


async def _route_and_stream(request: Request, inference_req: InferenceRequest):
    """Shared routing logic for Ollama endpoints with holding queue + fallbacks."""
    scorer = request.app.state.scorer
    queue_mgr = request.app.state.queue_mgr
    proxy = request.app.state.streaming_proxy
    registry = request.app.state.registry
    settings = request.app.state.settings
    model = inference_req.original_model or inference_req.model
    logger.info(f"Ollama request: model={model} stream={inference_req.stream}")

    # Score with fallback support + auto-pull
    results, actual_model = await score_with_fallbacks(
        inference_req, scorer, queue_mgr, registry,
        proxy=proxy, settings=settings,
    )

    if not results:
        logger.warning(f"No nodes for model={model} fallbacks={inference_req.fallback_models}")
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

    process_fn = proxy.make_process_fn(queue_key, queue_mgr, scorer=scorer, settings=settings)
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
    headers.update(check_context_overflow(winner, inference_req, registry))

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

        # Handle Ollama native image generation response
        if final_data and final_data.get("image"):
            import base64
            import time as time_mod

            from fleet_manager.server.routes.image_compat import _record_image_gen

            png_bytes = base64.b64decode(final_data["image"])
            elapsed_ms = int((time_mod.time() - inference_req.created_at) * 1000)
            _record_image_gen(
                model=inference_req.model,
                node_id=winner.node_id,
                status="completed",
                generation_ms=elapsed_ms,
            )
            logger.info(
                f"Ollama native image gen: model={inference_req.model} "
                f"node={winner.node_id} {len(png_bytes)} bytes in {elapsed_ms}ms"
            )
            return Response(
                content=png_bytes,
                media_type="image/png",
                headers={
                    **headers,
                    "X-Generation-Time": str(elapsed_ms),
                },
            )

        if final_data:
            final_data["message"] = {"role": "assistant", "content": full_response}
            return final_data
        return {"message": {"role": "assistant", "content": full_response}, "done": True}
