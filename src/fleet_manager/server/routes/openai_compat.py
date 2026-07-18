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
from fleet_manager.server.fleet_headers import fleet_headers
from fleet_manager.server.mlx_proxy import (
    MlxModelMissingError,
    MlxQueueFullError,
    is_mlx_model,
    record_trace_mlx,
    strip_mlx_prefix,
)
from fleet_manager.server.queue_manager import ClientConcurrencyExceeded
from fleet_manager.server.routes.ollama_compat import _build_thinking_headers
from fleet_manager.server.routes.routing import (
    check_context_overflow,
    client_concurrency_response,
    client_error_passthrough,
    extract_tags,
    get_all_fleet_models,
    parse_allow_fallback,
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
        # Include healthy MLX models (advertised with the `mlx:` prefix that
        # routes them to mlx_lm.server).  Without this, an OpenAI client that
        # validates against /v1/models can't discover the fast MLX models it's
        # now allowed to request via /v1/chat/completions.
        for s in node.mlx_servers:
            if s.status == "healthy":
                models.add(f"mlx:{s.model}")

    now = int(time.time())
    # `data` — the OpenAI standard shape. Kept pure.
    entries = [
        {"id": m, "object": "model", "created": now, "owned_by": "ollama"}
        for m in sorted(models)
    ]
    # `models` — Codex CLI's model manager requires its own richer schema and
    # rejects the OpenAI one, logging "failed to decode models response:
    # missing field `models`" then "... missing field `slug`" on every refresh,
    # which leaves its model picker empty.  Discovered by running a real
    # codex-cli 0.145 against the herd (2026-07-18); each added field revealed
    # the next required one.  Emitting a second key keeps `data` OpenAI-clean
    # while satisfying Codex — neither client notices the other's key.
    codex_entries = [
        {
            "id": m,
            "slug": m,
            "display_name": m,
            "description": f"Local model served by Ollama Herd ({m})",
            "object": "model",
            "created": now,
            "owned_by": "ollama",
            "context_window": 131072,
            "max_output_tokens": 32768,
            "supported_reasoning_efforts": [],
            "supported_reasoning_levels": [],
            # Codex requires this and rejects anything outside its enum
            # (default|local|unified_exec|disabled|shell_command). It does NOT
            # affect which tools Codex offers — all five values were tested and
            # produce an identical 12-tool payload. Present purely to stop a
            # models-refresh error on every turn.
            "shell_type": "default",
        }
        for m in sorted(models)
    ]
    return {"object": "list", "data": entries, "models": codex_entries}


async def _serve_openai_via_mlx(
    *,
    request: Request,
    inference_req: InferenceRequest,
    mlx_proxy,
    model: str,
):
    """Forward an OpenAI chat-completions request straight to ``mlx_lm.server``.

    ``mlx_lm.server`` speaks the OpenAI API natively, so this is a passthrough:
    non-streaming returns its response dict verbatim, streaming forwards its raw
    SSE chunks.  No Anthropic-style translation (that's the anthropic route's
    job).  Admission control, queue-full 503s, and trace recording match the
    Anthropic MLX path so the dashboard + health checks see MLX traffic.
    """
    trace_store = getattr(request.app.state, "trace_store", None)
    t_start = time.time()
    headers = fleet_headers(
        node_id="mlx-local",
        served_model=model,
        requested_model=model,
        backend="mlx",
    )
    headers["Cache-Control"] = "no-cache"

    if not inference_req.stream:
        # Non-streaming — one-shot request/response, no translation needed.
        try:
            openai_resp = await mlx_proxy.completions_non_streaming(inference_req)
        except MlxQueueFullError as exc:
            logger.warning(f"OpenAI MLX queue full: {exc} — returning 503")
            record_trace_mlx(
                trace_store, inference_req, t_start, None, "failed",
                error_message=str(exc),
            )
            return JSONResponse(
                status_code=503,
                headers={"Retry-After": str(exc.retry_after)},
                content={"error": {"message": str(exc), "type": "model_overloaded"}},
            )
        except (MlxModelMissingError, Exception) as exc:  # noqa: BLE001
            status = 500 if isinstance(exc, MlxModelMissingError) else 502
            logger.error(f"OpenAI MLX error ({status}): {type(exc).__name__}: {exc}")
            record_trace_mlx(
                trace_store, inference_req, t_start, None, "failed",
                error_message=str(exc),
            )
            return JSONResponse(
                status_code=status,
                content={"error": {"message": str(exc), "type": "api_error"}},
            )
        # pop_token_counts folds cached_tokens into rolling stats as a side effect.
        mlx_proxy.pop_token_counts(inference_req.request_id)
        record_trace_mlx(trace_store, inference_req, t_start, t_start, "completed")
        return JSONResponse(content=openai_resp, headers=headers)

    # Streaming — pre-admit before StreamingResponse locks in a 200 status, so
    # admission failures surface as a clean 503.
    model_key = strip_mlx_prefix(inference_req.model)
    try:
        await mlx_proxy._acquire_slot(model_key)
    except MlxModelMissingError as exc:
        record_trace_mlx(
            trace_store, inference_req, t_start, None, "failed", error_message=str(exc),
        )
        return JSONResponse(
            status_code=500,
            content={"error": {"message": str(exc), "type": "api_error"}},
        )
    except MlxQueueFullError as exc:
        logger.warning(f"OpenAI MLX queue full (stream): {exc} — returning 503")
        record_trace_mlx(
            trace_store, inference_req, t_start, None, "failed", error_message=str(exc),
        )
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": str(exc.retry_after)},
            content={"error": {"message": str(exc), "type": "model_overloaded"}},
        )

    async def _passthrough():
        first_token_time: float | None = None
        error: Exception | None = None
        try:
            # already_admitted=True — slot acquired above, we own the release.
            # stream_openai yields bare SSE lines (aiter_lines strips the "\n\n"
            # event framing and drops empty separator lines); re-add "\n\n" so
            # the client receives well-formed `data: {...}\n\n` events.
            async for raw in mlx_proxy.stream_openai(inference_req, already_admitted=True):
                if first_token_time is None:
                    first_token_time = time.time()
                yield raw + b"\n\n"
        except Exception as exc:  # noqa: BLE001
            error = exc
            logger.exception(f"OpenAI MLX stream aborted: {type(exc).__name__}: {exc}")
            raise
        finally:
            mlx_proxy._release_slot(model_key)
            mlx_proxy.pop_token_counts(inference_req.request_id)
            record_trace_mlx(
                trace_store, inference_req, t_start, first_token_time,
                "failed" if error else "completed",
                error_message=str(error) if error else None,
            )

    return StreamingResponse(
        _passthrough(), media_type="text/event-stream", headers=headers,
    )


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
        client_ip=request.client.host if request.client else "",
    )

    # MLX backend fast-path — mirrors the Anthropic route (anthropic_compat.py).
    # `mlx:`-prefixed models live in an mlx_lm.server subprocess, not Ollama, so
    # the scoring + Ollama-streaming pipeline below can't reach them.  Because
    # mlx_lm.server is itself OpenAI-compatible, this path is a clean passthrough
    # (no response translation) — unlike the Anthropic route, which translates.
    # This is what lets OpenAI-only clients (e.g. a scanner that speaks only
    # /v1/chat/completions) reach the fast MLX models instead of the slow Ollama
    # fallback.  See docs/plans/mlx-backend-for-large-models.md.
    if is_mlx_model(model):
        mlx_proxy = getattr(request.app.state, "mlx_proxy", None)
        if mlx_proxy is None:
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "message": (
                            f"Model '{model}' needs the MLX backend but "
                            "FLEET_MLX_ENABLED is false. Enable it and restart herd."
                        ),
                        "type": "model_not_available",
                    }
                },
            )
        return await _serve_openai_via_mlx(
            request=request, inference_req=inference_req, mlx_proxy=mlx_proxy, model=model,
        )

    scorer = request.app.state.scorer
    queue_mgr = request.app.state.queue_mgr
    proxy = request.app.state.streaming_proxy
    registry = request.app.state.registry
    settings = request.app.state.settings

    # Score with fallback support + auto-pull.  A per-request strict-mode
    # signal (X-Fleet-No-Fallback header / "fallback" body field) overrides
    # the global vram_fallback setting for this call only.
    allow_fallback = parse_allow_fallback(body, request.headers)
    results, actual_model = await score_with_fallbacks(
        inference_req, scorer, queue_mgr, registry,
        proxy=proxy, settings=settings, allow_fallback=allow_fallback,
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
    try:
        response_future = await queue_mgr.enqueue(entry, process_fn)
    except ClientConcurrencyExceeded as e:
        return client_concurrency_response(e)
    stream = await response_future

    # Build response headers — canonical X-Fleet-* set via the shared builder.
    headers = {"Cache-Control": "no-cache", "Connection": "keep-alive"}
    headers.update(fleet_headers(
        node_id=winner.node_id,
        served_model=actual_model,
        requested_model=model,
        backend="mlx" if actual_model.startswith("mlx:") else "ollama",
        score=winner.score,
        retries=entry.retry_count,
        extra=check_context_overflow(winner, inference_req, registry),
    ))

    if inference_req.stream:

        async def _stream_and_cleanup():
            """Yield all chunks, then clean up token tracking."""
            async for chunk in stream:
                yield chunk
            # Streaming callers don't use token counts in the response,
            # so clean up the side-channel dict here.
            proxy.pop_token_counts(inference_req.request_id)

        return StreamingResponse(
            _stream_and_cleanup(),
            media_type="text/event-stream",
            headers=headers,
        )
    else:
        # Non-streaming: accumulate full response
        full_content = ""
        try:
            async for chunk in stream:
                if chunk.startswith("data: ") and chunk.strip() != "data: [DONE]":
                    try:
                        data = json.loads(chunk[6:])
                        delta = data.get("choices", [{}])[0].get("delta", {})
                        full_content += delta.get("content", "")
                    except (json.JSONDecodeError, IndexError) as e:
                        logger.debug(f"Skipping malformed SSE chunk: {e}")
        except Exception as exc:  # noqa: BLE001 — 4xx passes through, 5xx re-raises
            # A backend client-error (e.g. "model does not support tools") must
            # reach the caller as that 4xx, not an opaque 500. See
            # client_error_passthrough.
            client_err = client_error_passthrough(exc, model=actual_model)
            if client_err is not None:
                logger.info(
                    f"OpenAI: backend client-error for {actual_model} → "
                    f"passing through {client_err.status_code}"
                )
                return client_err
            raise

        # Retrieve real token counts extracted from Ollama response
        tokens = proxy.pop_token_counts(inference_req.request_id)
        prompt_tok = tokens[0] or 0
        completion_tok = tokens[1] or 0

        # Add thinking-aware headers
        headers.update(_build_thinking_headers(proxy, inference_req.request_id))

        return JSONResponse(
            content={
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
            },
            headers=headers,
        )


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
