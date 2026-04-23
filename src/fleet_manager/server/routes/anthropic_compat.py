"""Anthropic Messages API compat endpoints.

Lets Claude Code (and any anthropic-SDK client) point ANTHROPIC_BASE_URL at
ollama-herd:

    export ANTHROPIC_BASE_URL=http://localhost:11435
    export ANTHROPIC_AUTH_TOKEN=dummy
    claude

Translates Anthropic Messages JSON → internal InferenceRequest, runs it through
the same scoring/queue pipeline as openai_compat / ollama_compat, and translates
the streaming Ollama response back into Anthropic SSE event sequence.

Tool use is a first-class concern — Claude Code is essentially useless without it.
"""

from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from fleet_manager.models.request import InferenceRequest, QueueEntry, RequestFormat
from fleet_manager.server import debug_log
from fleet_manager.server.anthropic_models import AnthropicMessagesRequest
from fleet_manager.server.anthropic_translator import (
    AnthropicSSEState,
    accumulate_anthropic_response,
    anthropic_system_to_text,
    anthropic_to_ollama_messages,
    anthropic_tools_to_ollama,
    apply_tool_choice,
    estimate_tokens,
    flatten_text_for_count,
    map_anthropic_model,
    ollama_chunk_to_anthropic_events,
)
from fleet_manager.server.mlx_proxy import (
    MlxModelMissingError,
    MlxQueueFullError,
    build_anthropic_non_streaming_response,
    is_mlx_model,
    openai_sse_to_anthropic_events,
    record_trace_mlx,
    strip_mlx_prefix,
)
from fleet_manager.server.routes.routing import (
    check_context_overflow,
    extract_tags,
    get_all_fleet_models,
    score_with_fallbacks,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["anthropic"])

# Cap how much of the translated Ollama body we'll dump at DEBUG level.
# Large prompts + tool schemas can be huge; truncate to keep logs usable.
_DEBUG_BODY_PREVIEW_CHARS = 4000


def _request_has_images(body: AnthropicMessagesRequest) -> bool:
    """Return True iff any message contains an image content block.

    Anthropic content blocks come in as raw dicts (AnthropicMessage.content is
    typed `str | list[dict[str, Any]]`), so we scan for `{"type": "image", ...}`
    entries.  A string-form message never has images.  ToolResultBlock.content
    can itself contain image sub-blocks (per the Anthropic spec) — we check
    those too for completeness.
    """
    for msg in body.messages:
        content = msg.content
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "image":
                return True
            # tool_result blocks can nest image sub-blocks
            if btype == "tool_result":
                inner = block.get("content")
                if isinstance(inner, list):
                    for sub in inner:
                        if isinstance(sub, dict) and sub.get("type") == "image":
                            return True
    return False


def _check_auth(settings, x_api_key: str | None, client_host: str = "") -> JSONResponse | None:
    """Return a 401 JSONResponse if auth is required and the key doesn't match.

    Logs a WARNING on rejection so admins can see brute-force / misconfig attempts
    without leaking the expected key.
    """
    if not getattr(settings, "anthropic_require_key", False):
        return None
    expected = getattr(settings, "anthropic_api_key", "") or ""
    if not expected:
        return None
    if x_api_key != expected:
        # Log the prefix of what was sent so a wrong-key vs missing-key bug is
        # debuggable, but don't log full keys (could be sensitive in shared logs).
        sent_preview = (x_api_key[:6] + "…") if x_api_key else "<none>"
        logger.warning(
            f"Anthropic auth rejected: client={client_host or '?'} sent key={sent_preview}"
        )
        return JSONResponse(
            status_code=401,
            content={
                "type": "error",
                "error": {"type": "authentication_error", "message": "Invalid API key"},
            },
        )
    return None


def _summarize_ollama_body(ollama_body: dict) -> dict:
    """Compact summary of the translated body — safe to log at DEBUG.

    Returns counts + names rather than full payload so individual log lines
    stay readable. Pair with `_full_body_preview()` when you actually need
    to see the prompt content.
    """
    msgs = ollama_body.get("messages") or []
    role_counts: dict[str, int] = {}
    images_total = 0
    tool_call_msgs = 0
    for m in msgs:
        role_counts[m.get("role", "?")] = role_counts.get(m.get("role", "?"), 0) + 1
        images_total += len(m.get("images") or [])
        if m.get("tool_calls"):
            tool_call_msgs += 1
    tools = ollama_body.get("tools") or []
    return {
        "messages": len(msgs),
        "roles": role_counts,
        "images": images_total,
        "tool_call_msgs": tool_call_msgs,
        "tools": [t.get("function", {}).get("name", "?") for t in tools],
        "options": ollama_body.get("options") or {},
    }


def _full_body_preview(ollama_body: dict) -> str:
    """Return a truncated JSON dump of the body for DEBUG-level inspection."""
    text = json.dumps(ollama_body, default=str)
    if len(text) > _DEBUG_BODY_PREVIEW_CHARS:
        return text[:_DEBUG_BODY_PREVIEW_CHARS] + f"…<truncated {len(text)} chars>"
    return text


def _build_ollama_request_body(
    body: AnthropicMessagesRequest, local_model: str,
) -> dict:
    """Translate the validated Anthropic body into an Ollama-shaped dict.

    Stored in InferenceRequest.raw_body — `streaming._build_ollama_body` will
    read it back, finalize options (keep_alive, context protection, thinking
    inflate), and post it to the chosen node's Ollama.
    """
    system_text = anthropic_system_to_text(body.system)

    # Apply tool_choice forcing semantics by appending to system prompt
    raw_tools = anthropic_tools_to_ollama(
        [t.model_dump() for t in body.tools] if body.tools else None,
    )
    tool_choice_dict = body.tool_choice.model_dump() if body.tool_choice else None
    raw_tools, system_text = apply_tool_choice(raw_tools, tool_choice_dict, system_text)

    ollama_messages = anthropic_to_ollama_messages(
        [m.model_dump() for m in body.messages], system=system_text,
    )

    options: dict = {}
    if body.temperature is not None:
        options["temperature"] = body.temperature
    if body.top_p is not None:
        options["top_p"] = body.top_p
    if body.top_k is not None:
        options["top_k"] = body.top_k
    if body.max_tokens:
        options["num_predict"] = body.max_tokens
    if body.stop_sequences:
        options["stop"] = body.stop_sequences

    out: dict = {
        "model": local_model,
        "messages": ollama_messages,
        "stream": True,
        "keep_alive": -1,
    }
    if options:
        out["options"] = options
    if raw_tools:
        out["tools"] = raw_tools
    return out


async def _serve_via_mlx(
    *,
    request: Request,
    body: AnthropicMessagesRequest,
    inference_req: InferenceRequest,
    mlx_proxy,
    rid: str,
    t_start: float,
    anthropic_version: str | None,
):
    """Forward an Anthropic request to `mlx_lm.server` and stream the response back.

    Phase 1 MVP — bypasses the scoring + queue pipeline entirely since there's
    only one MLX backend per node (no routing decision needed).  Still records
    traces so the dashboard + health checks see MLX traffic.
    """
    from fleet_manager.server.mlx_proxy import _MlxToolState

    trace_store = getattr(request.app.state, "trace_store", None)
    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Fleet-Node": "mlx-local",
        "X-Fleet-Backend": "mlx",
    }
    if anthropic_version:
        headers["anthropic-version"] = anthropic_version

    settings = getattr(request.app.state, "settings", None)
    debug_enabled = bool(getattr(settings, "debug_request_bodies", False)) if settings else False
    debug_data_dir = getattr(settings, "data_dir", None) if settings else None
    debug_retention = getattr(settings, "debug_request_retention_days", 7) if settings else 7

    if not body.stream:
        # Non-streaming path — one-shot request/response translation
        ns_error: Exception | None = None
        ns_response_body: dict | None = None
        try:
            try:
                openai_resp = await mlx_proxy.completions_non_streaming(inference_req)
            except MlxQueueFullError as exc:
                # Admission control tripped — mlx backend is at capacity.
                # Return 503 + Retry-After so Claude Code (or any well-behaved
                # client) backs off instead of piling on retries that would
                # just wedge mlx's internal HTTP queue.
                ns_error = exc
                logger.warning(
                    f"Anthropic[{rid}] MLX queue full: "
                    f"{exc.queued} queued + {exc.in_flight} in-flight "
                    f"(model={exc.model_key}) — returning 503"
                )
                record_trace_mlx(
                    trace_store, inference_req, t_start, None, "failed",
                    error_message=str(exc),
                )
                return JSONResponse(
                    status_code=503,
                    headers={"Retry-After": str(exc.retry_after)},
                    content={
                        "type": "error",
                        "error": {
                            "type": "overloaded_error",
                            "message": str(exc),
                        },
                    },
                )
            except MlxModelMissingError as exc:
                # Defensive: model name went missing somewhere in the route.
                # Surface as 500 with a clear operator-facing message instead
                # of letting it fall through to a confusing 502.
                ns_error = exc
                logger.error(f"Anthropic[{rid}] MLX model-missing guard fired: {exc}")
                record_trace_mlx(
                    trace_store, inference_req, t_start, None, "failed",
                    error_message=str(exc),
                )
                return JSONResponse(
                    status_code=500,
                    content={
                        "type": "error",
                        "error": {
                            "type": "api_error",
                            "message": str(exc),
                        },
                    },
                )
            except Exception as exc:
                ns_error = exc
                logger.exception(f"Anthropic[{rid}] MLX non-streaming failed: {exc}")
                record_trace_mlx(
                    trace_store, inference_req, t_start, None, "failed",
                    error_message=str(exc),
                )
                return JSONResponse(
                    status_code=502,
                    content={
                        "type": "error",
                        "error": {
                            "type": "api_error",
                            "message": f"MLX backend error: {exc}",
                        },
                    },
                )
            record_trace_mlx(
                trace_store, inference_req, t_start, time.time(), "completed",
            )
            ns_response_body = build_anthropic_non_streaming_response(
                openai_resp, body.model,
            )
            return JSONResponse(content=ns_response_body, headers=headers)
        finally:
            if debug_enabled and debug_data_dir:
                debug_log.append_request(
                    enabled=True,
                    data_dir=str(debug_data_dir),
                    record={
                        "request_id": inference_req.request_id,
                        "timestamp": t_start,
                        "node_id": "mlx-local",
                        "model": inference_req.model,
                        "original_model": inference_req.original_model or body.model,
                        "original_format": "anthropic",
                        "tags": list(inference_req.tags),
                        "status": "failed" if ns_error else "completed",
                        "error": str(ns_error) if ns_error else None,
                        "latency_ms": int((time.time() - t_start) * 1000),
                        "ttft_ms": None,
                        # Real Anthropic body the client sent — not the
                        # internal translated form. This is what replay POSTs.
                        "client_body": body.model_dump(by_alias=True, exclude_none=True),
                        "ollama_body": None,  # MLX path doesn't translate to Ollama
                        "backend": "mlx",
                        "stream": False,
                        "response": ns_response_body,
                    },
                    retention_days=debug_retention,
                )

    # Streaming path — OpenAI SSE → Anthropic SSE translation.
    # Pre-admit BEFORE constructing the StreamingResponse.  Once FastAPI
    # starts iterating the generator it commits to a 200 status; we need
    # admission failures to surface as a clean 503 before then.
    stream_model_key = strip_mlx_prefix(inference_req.model)
    try:
        await mlx_proxy._acquire_slot(stream_model_key)
    except MlxModelMissingError as exc:
        logger.error(f"Anthropic[{rid}] MLX model-missing (stream): {exc}")
        record_trace_mlx(
            trace_store, inference_req, t_start, None, "failed",
            error_message=str(exc),
        )
        return JSONResponse(
            status_code=500,
            content={"type": "error", "error": {"type": "api_error", "message": str(exc)}},
        )
    except MlxQueueFullError as exc:
        logger.warning(
            f"Anthropic[{rid}] MLX queue full (stream): "
            f"{exc.queued} queued + {exc.in_flight} in-flight "
            f"(model={exc.model_key}) — returning 503"
        )
        record_trace_mlx(
            trace_store, inference_req, t_start, None, "failed",
            error_message=str(exc),
        )
        if debug_enabled and debug_data_dir:
            debug_log.append_request(
                enabled=True,
                data_dir=str(debug_data_dir),
                record={
                    "request_id": inference_req.request_id,
                    "timestamp": t_start,
                    "node_id": "mlx-local",
                    "model": inference_req.model,
                    "original_model": inference_req.original_model or body.model,
                    "original_format": "anthropic",
                    "tags": list(inference_req.tags),
                    "status": "rejected",
                    "error": str(exc),
                    "latency_ms": int((time.time() - t_start) * 1000),
                    "ttft_ms": None,
                    "client_body": body.model_dump(by_alias=True, exclude_none=True),
                    "backend": "mlx",
                    "stream": True,
                    "response": None,
                },
                retention_days=debug_retention,
            )
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": str(exc.retry_after)},
            content={
                "type": "error",
                "error": {
                    "type": "overloaded_error",
                    "message": str(exc),
                },
            },
        )

    async def _sse_generator():
        state = AnthropicSSEState(model=body.model)
        tools_state: dict[int, _MlxToolState] = {}
        first_token_time: float | None = None
        error: Exception | None = None
        try:
            # already_admitted=True — slot acquired above, we own the release.
            async for raw in mlx_proxy.stream_openai(inference_req, already_admitted=True):
                for event in openai_sse_to_anthropic_events(
                    raw.decode("utf-8", errors="replace"),
                    state,
                    tools_state,
                    inference_req.request_id,
                ):
                    if first_token_time is None and "content_block_delta" in event:
                        first_token_time = time.time()
                    yield event
            # Synthesize stop if mlx dropped without finish_reason
            if not state.finished:
                if state.text_open:
                    yield (
                        "event: content_block_stop\ndata: "
                        + json.dumps(
                            {
                                "type": "content_block_stop",
                                "index": state.text_block_index,
                            }
                        )
                        + "\n\n"
                    )
                yield (
                    "event: message_delta\ndata: "
                    + json.dumps(
                        {
                            "type": "message_delta",
                            "delta": {
                                "stop_reason": "end_turn",
                                "stop_sequence": None,
                            },
                            "usage": {"output_tokens": state.output_tokens},
                        }
                    )
                    + "\n\n"
                )
                yield (
                    "event: message_stop\ndata: "
                    + json.dumps({"type": "message_stop"})
                    + "\n\n"
                )
        except Exception as exc:
            error = exc
            logger.exception(
                f"Anthropic[{rid}] MLX stream aborted: {type(exc).__name__}: {exc}"
            )
            raise
        finally:
            # Release the admission slot we acquired before entering the
            # StreamingResponse — guaranteed to fire whether iteration
            # completed, raised, or was cancelled by a client disconnect.
            mlx_proxy._release_slot(stream_model_key)
            mlx_proxy.pop_token_counts(inference_req.request_id)
            status = "failed" if error else "completed"
            err_msg = str(error) if error else None
            record_trace_mlx(
                trace_store, inference_req, t_start, first_token_time,
                status, error_message=err_msg,
            )
            elapsed_ms = (time.time() - t_start) * 1000
            if error is None:
                logger.info(
                    f"Anthropic[{rid}] MLX stream done: tools={len(state.emitted_tools)} "
                    f"output_tok≈{state.output_tokens} elapsed_ms={elapsed_ms:.0f}"
                )
            if debug_enabled and debug_data_dir:
                ttft_ms = (
                    int((first_token_time - t_start) * 1000)
                    if first_token_time else None
                )
                debug_log.append_request(
                    enabled=True,
                    data_dir=str(debug_data_dir),
                    record={
                        "request_id": inference_req.request_id,
                        "timestamp": t_start,
                        "node_id": "mlx-local",
                        "model": inference_req.model,
                        "original_model": inference_req.original_model or body.model,
                        "original_format": "anthropic",
                        "tags": list(inference_req.tags),
                        "status": status,
                        "error": err_msg,
                        "latency_ms": int(elapsed_ms),
                        "ttft_ms": ttft_ms,
                        "completion_tokens": state.output_tokens,
                        "tool_calls_emitted": len(state.emitted_tools),
                        "client_body": body.model_dump(by_alias=True, exclude_none=True),
                        "ollama_body": None,
                        "backend": "mlx",
                        "stream": True,
                        # Stream output isn't reconstructed here — chunks flow
                        # straight to the client. Replay can re-execute against
                        # client_body and collect a fresh response.
                        "response": None,
                    },
                    retention_days=debug_retention,
                )

    return StreamingResponse(
        _sse_generator(),
        media_type="text/event-stream",
        headers=headers,
    )


@router.post("/v1/messages")
async def messages(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
    anthropic_version: str | None = Header(default=None, alias="anthropic-version"),
):
    """Anthropic Messages endpoint. Streaming and non-streaming."""
    settings = request.app.state.settings
    client_host = (request.client.host if request.client else "") or ""
    t_start = time.time()

    auth_err = _check_auth(settings, x_api_key, client_host=client_host)
    if auth_err is not None:
        return auth_err

    # Parse + validate
    try:
        raw = await request.json()
    except json.JSONDecodeError as exc:
        logger.warning(
            f"Anthropic 400: invalid JSON from {client_host}: {exc}"
        )
        return JSONResponse(
            status_code=400,
            content={"type": "error", "error": {"type": "invalid_request_error",
                                                "message": "Invalid JSON body"}},
        )
    try:
        body = AnthropicMessagesRequest.model_validate(raw)
    except ValidationError as exc:
        # Validation errors are noisy in pydantic — log just the message count
        # and the error so it's actionable without dumping the whole tree.
        logger.warning(
            f"Anthropic 400: validation failed from {client_host}: "
            f"{len(exc.errors())} error(s) — first: {exc.errors()[0] if exc.errors() else 'n/a'}"
        )
        return JSONResponse(
            status_code=400,
            content={
                "type": "error",
                "error": {"type": "invalid_request_error", "message": str(exc)},
            },
        )

    # Map model
    model_map = getattr(settings, "anthropic_model_map", {}) or {}
    local_model = map_anthropic_model(body.model, model_map)
    if not local_model:
        return JSONResponse(
            status_code=400,
            content={
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": (
                        f"No mapping for model '{body.model}' and no default configured. "
                        "Set FLEET_ANTHROPIC_MODEL_MAP."
                    ),
                },
            },
        )

    # Vision override — if the request contains image content blocks, route to
    # the configured vision model regardless of the Claude tier mapping.  Keeps
    # opus/sonnet on their coding models while still handling image requests
    # correctly.  No-op when FLEET_ANTHROPIC_VISION_MODEL is empty.
    vision_model = getattr(settings, "anthropic_vision_model", "") or ""
    if vision_model and _request_has_images(body):
        logger.info(
            f"Anthropic vision: {body.model} → {vision_model} "
            f"(override from {local_model} due to image content)"
        )
        local_model = vision_model

    tags = extract_tags(raw, request.headers)
    if body.metadata and isinstance(body.metadata, dict):
        user_id = body.metadata.get("user_id")
        if user_id and isinstance(user_id, str):
            tags = list(tags) + [f"user:{user_id}"]

    # Pre-translate to Ollama body — stored in raw_body for _build_ollama_body
    ollama_body = _build_ollama_request_body(body, local_model)

    inference_req = InferenceRequest(
        model=local_model,
        original_model=local_model,
        messages=ollama_body["messages"],  # already Ollama-shaped
        stream=body.stream,
        temperature=body.temperature if body.temperature is not None else 0.7,
        max_tokens=body.max_tokens,
        original_format=RequestFormat.ANTHROPIC,
        raw_body=ollama_body,
        tags=tags,
    )
    rid = inference_req.request_id[:8]

    # MLX backend fast-path — when the resolved model has an `mlx:` prefix, bypass
    # the scoring + queue pipeline and forward directly to mlx_lm.server.  This is
    # the Phase 1 MVP from `docs/plans/mlx-backend-for-large-models.md`; Phase 2
    # will integrate via the normal node registry.
    if is_mlx_model(local_model):
        mlx_proxy = getattr(request.app.state, "mlx_proxy", None)
        if mlx_proxy is None:
            logger.warning(
                f"Anthropic[{rid}] 503: model '{local_model}' requested but MLX "
                f"backend is not enabled (set FLEET_MLX_ENABLED=true)"
            )
            return JSONResponse(
                status_code=503,
                content={
                    "type": "error",
                    "error": {
                        "type": "overloaded_error",
                        "message": (
                            f"Model '{local_model}' needs the MLX backend but "
                            "FLEET_MLX_ENABLED is false. Enable it and restart herd."
                        ),
                    },
                },
            )
        logger.info(
            f"Anthropic[{rid}] MLX route: {body.model} → {local_model} "
            f"(stripped: {strip_mlx_prefix(local_model)}) stream={body.stream} "
            f"tools={len(body.tools or [])}"
        )
        return await _serve_via_mlx(
            request=request,
            body=body,
            inference_req=inference_req,
            mlx_proxy=mlx_proxy,
            rid=rid,
            t_start=t_start,
            anthropic_version=anthropic_version,
        )

    logger.info(
        f"Anthropic[{rid}] request: model={body.model} → {local_model} stream={body.stream} "
        f"tools={len(body.tools or [])} msgs={len(body.messages)} "
        f"max_tokens={body.max_tokens} from={client_host or '?'}"
        + (f" tags={tags}" if tags else "")
        + (f" v={anthropic_version}" if anthropic_version else "")
        + (f" tool_choice={body.tool_choice.type}" if body.tool_choice else "")
    )
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            f"Anthropic[{rid}] translated body summary: "
            f"{json.dumps(_summarize_ollama_body(ollama_body), default=str)}"
        )
        logger.debug(f"Anthropic[{rid}] full body: {_full_body_preview(ollama_body)}")

    scorer = request.app.state.scorer
    queue_mgr = request.app.state.queue_mgr
    proxy = request.app.state.streaming_proxy
    registry = request.app.state.registry

    results, actual_model = await score_with_fallbacks(
        inference_req, scorer, queue_mgr, registry, proxy=proxy, settings=settings,
    )

    if not results:
        all_fleet_models = get_all_fleet_models(registry)
        any_exists = local_model in all_fleet_models
        if not any_exists:
            logger.warning(
                f"Anthropic[{rid}] 404: model '{local_model}' (from '{body.model}') "
                f"not on any fleet node"
            )
            return JSONResponse(
                status_code=404,
                content={
                    "type": "error",
                    "error": {
                        "type": "not_found_error",
                        "message": (
                            f"Model '{local_model}' (mapped from '{body.model}') not "
                            f"available on any node. Run 'ollama pull {local_model}'."
                        ),
                    },
                },
            )
        logger.warning(
            f"Anthropic[{rid}] 503: model '{local_model}' exists but no node can serve "
            f"it (all at capacity / offline)"
        )
        return JSONResponse(
            status_code=503,
            content={
                "type": "error",
                "error": {
                    "type": "overloaded_error",
                    "message": (
                        f"Model '{local_model}' exists but no node can serve it right now."
                    ),
                },
            },
        )

    fallback_used = actual_model != local_model
    if fallback_used:
        logger.info(
            f"Anthropic[{rid}] fallback: {local_model} → {actual_model}"
        )
        inference_req.model = actual_model
        inference_req.raw_body["model"] = actual_model

    winner = results[0]
    logger.info(
        f"Anthropic[{rid}] routed to node={winner.node_id} score={int(winner.score)} "
        f"queue_key={winner.queue_key}"
    )
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
    if anthropic_version:
        headers["anthropic-version"] = anthropic_version

    if body.stream:
        async def _sse_generator():
            state = AnthropicSSEState(model=body.model)
            synthesized = False
            error: Exception | None = None
            try:
                async for line in stream:
                    for event in ollama_chunk_to_anthropic_events(
                        line, state, stop_sequences=body.stop_sequences,
                    ):
                        yield event
                # If Ollama dropped without done, synthesize a stop so the client doesn't hang
                if not state.finished:
                    synthesized = True
                    if state.text_open:
                        yield (
                            "event: content_block_stop\ndata: "
                            + json.dumps({
                                "type": "content_block_stop",
                                "index": state.text_block_index,
                            })
                            + "\n\n"
                        )
                    yield (
                        "event: message_delta\ndata: "
                        + json.dumps({
                            "type": "message_delta",
                            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                            "usage": {"output_tokens": state.output_tokens},
                        })
                        + "\n\n"
                    )
                    stop_data = json.dumps({"type": "message_stop"})
                    yield f"event: message_stop\ndata: {stop_data}\n\n"
            except Exception as exc:  # noqa: BLE001 — log and re-raise for FastAPI
                error = exc
                logger.exception(
                    f"Anthropic[{rid}] stream aborted on node={winner.node_id}: "
                    f"{type(exc).__name__}: {exc}"
                )
                raise
            finally:
                proxy.pop_token_counts(inference_req.request_id)
                proxy.pop_request_meta(inference_req.request_id)
                elapsed_ms = (time.time() - t_start) * 1000
                if synthesized:
                    logger.warning(
                        f"Anthropic[{rid}] stream done (SYNTHESIZED stop — Ollama "
                        f"dropped before done:true): node={winner.node_id} "
                        f"tools={len(state.emitted_tools)} "
                        f"output_tok≈{state.output_tokens} "
                        f"elapsed_ms={elapsed_ms:.0f}"
                    )
                elif error is None:
                    logger.info(
                        f"Anthropic[{rid}] stream done: node={winner.node_id} "
                        f"stop={state.stop_reason} tools={len(state.emitted_tools)} "
                        f"in_tok={state.input_tokens} out_tok={state.output_tokens} "
                        f"elapsed_ms={elapsed_ms:.0f}"
                        + (
                            f" tool_names={[t['name'] for t in state.emitted_tools]}"
                            if state.emitted_tools else ""
                        )
                    )

        return StreamingResponse(
            _sse_generator(),
            media_type="text/event-stream",
            headers=headers,
        )

    # Non-streaming: collect every NDJSON line, then build the response
    chunks: list[str] = []
    try:
        async for line in stream:
            chunks.append(line)
    except Exception as exc:  # noqa: BLE001 — surface the failure with context
        logger.exception(
            f"Anthropic[{rid}] non-streaming failed on node={winner.node_id}: "
            f"{type(exc).__name__}: {exc}"
        )
        raise
    finally:
        proxy.pop_token_counts(inference_req.request_id)
        proxy.pop_request_meta(inference_req.request_id)

    response = accumulate_anthropic_response(
        chunks, model=body.model, stop_sequences=body.stop_sequences,
    )
    elapsed_ms = (time.time() - t_start) * 1000
    blocks = response.get("content") or []
    block_types = [b.get("type") for b in blocks]
    tool_names = [b.get("name") for b in blocks if b.get("type") == "tool_use"]
    usage = response.get("usage") or {}
    logger.info(
        f"Anthropic[{rid}] done: node={winner.node_id} "
        f"stop={response.get('stop_reason')} blocks={block_types} "
        f"in_tok={usage.get('input_tokens', 0)} out_tok={usage.get('output_tokens', 0)} "
        f"elapsed_ms={elapsed_ms:.0f}"
        + (f" tool_names={tool_names}" if tool_names else "")
    )
    return JSONResponse(content=response, headers=headers)


@router.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    """Anthropic token-counting endpoint. Used by Claude Code for budget gating.

    Best-effort: tiktoken cl100k if installed, otherwise len(text)//4.
    Anthropic uses a different tokenizer in production but Claude Code only
    uses this for input budgeting, not billing.
    """
    try:
        raw = await request.json()
    except json.JSONDecodeError:
        logger.warning("Anthropic count_tokens 400: invalid JSON body")
        return JSONResponse(
            status_code=400,
            content={"type": "error", "error": {"type": "invalid_request_error",
                                                "message": "Invalid JSON body"}},
        )
    try:
        body = AnthropicMessagesRequest.model_validate(raw)
    except ValidationError as exc:
        logger.warning(
            f"Anthropic count_tokens 400: validation — "
            f"{exc.errors()[0] if exc.errors() else 'n/a'}"
        )
        return JSONResponse(
            status_code=400,
            content={
                "type": "error",
                "error": {"type": "invalid_request_error", "message": str(exc)},
            },
        )

    text = flatten_text_for_count(
        [m.model_dump() for m in body.messages], system=body.system,
    )
    n = estimate_tokens(text)
    logger.debug(
        f"Anthropic count_tokens: model={body.model} msgs={len(body.messages)} → {n} tokens"
    )
    return {"input_tokens": n}


@router.get("/v1/messages")
async def messages_get():
    """Friendly probe response — Claude Code may GET this during setup."""
    return {
        "ok": True,
        "service": "ollama-herd",
        "endpoint": "/v1/messages",
        "ts": int(time.time()),
    }
