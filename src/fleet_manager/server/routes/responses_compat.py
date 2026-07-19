"""OpenAI **Responses API** compat endpoint — what Codex CLI speaks.

    # ~/.codex/config.toml
    [model_providers.herd]
    base_url = "http://localhost:11435/v1"
    wire_api = "responses"

Codex removed chat/completions support in Feb 2026, so `/v1/chat/completions`
is not reachable from it — this route is the only way Codex can use the herd.

Structure mirrors ``anthropic_compat.py`` (translate → the shared
scorer/queue/streaming pipeline → translate back), but the *wire* translation
goes through the OpenAI shape rather than the Anthropic one: Responses is
OpenAI's own newer API, and Ollama's ``/api/chat`` already accepts OpenAI-shaped
messages and tools.  See ``docs/plans/codex-responses-api-support.md``.
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from fleet_manager.models.request import InferenceRequest, QueueEntry, RequestFormat
from fleet_manager.server.anthropic_autoroute import resolve_model
from fleet_manager.server.fleet_headers import fleet_headers
from fleet_manager.server.mlx_proxy import is_mlx_model
from fleet_manager.server.queue_manager import ClientConcurrencyExceeded
from fleet_manager.server.responses_translator import (
    ResponsesSSEState,
    accumulate_responses_object,
    input_has_images,
    looks_like_abandoned_preamble,
    ollama_chunk_to_responses_events,
    responses_to_openai_body,
)
from fleet_manager.server.routes.routing import (
    check_context_overflow,
    client_concurrency_response,
    client_error_passthrough,
    extract_tags,
    get_all_fleet_models,
    get_fleet_loaded_and_ondisk,
    parse_allow_fallback,
    score_with_fallbacks,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["responses"])


def _error(status: int, message: str, etype: str = "invalid_request_error") -> JSONResponse:
    """Responses-shaped error envelope."""
    return JSONResponse(
        status_code=status,
        content={"error": {"type": etype, "message": message}},
    )


@router.post("/v1/responses")
async def responses(request: Request):
    """Serve a Codex (Responses API) request from the local fleet."""
    t_start = time.time()
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed body is a client error
        return _error(400, "Invalid JSON body")
    if not isinstance(body, dict):
        return _error(400, "Request body must be a JSON object")

    requested_model = body.get("model") or ""
    stream = bool(body.get("stream", False))
    client_host = request.client.host if request.client else ""

    # Stateful chaining isn't supported — we never persist responses, so we
    # cannot reconstruct history from an id.  Fail loudly rather than silently
    # answering with only the latest turn's context.
    if body.get("previous_response_id"):
        return _error(
            400,
            "previous_response_id (server-side conversation state) is not supported. "
            "Send the full conversation in `input` (stateless chaining) — set "
            "store=false in your client.",
        )

    # Model resolution — the same shared resolver the Anthropic route uses.
    # A Codex id like `gpt-5-codex` isn't on the fleet, so it auto-routes to the
    # best loaded local model; a real local name passes through untouched.
    settings = request.app.state.settings
    registry = request.app.state.registry
    model_map = getattr(settings, "anthropic_model_map", {}) or {}
    auto_route = getattr(settings, "anthropic_auto_route", True)
    loaded_names, ondisk_names = get_fleet_loaded_and_ondisk(registry)
    # v1 serves Ollama-backed models only, so keep `mlx:` out of the auto-routing
    # candidates — otherwise a Codex request could resolve to a backend this
    # endpoint can't serve and 503 for no good reason.  An *explicit* mlx: map
    # entry still reaches the guard below and gets a clear error.  Supporting MLX
    # here needs an OpenAI-SSE → Responses front-end; see the plan's v2 note.
    loaded_names = {m for m in loaded_names if not m.startswith("mlx:")}
    ondisk_names = {m for m in ondisk_names if not m.startswith("mlx:")}
    # Codex's `view_image` puts an image in the next request. Routing that to a
    # text-only model gets a confident answer about a picture the model never
    # received — measured: qwen3-coder reported a 32x32 PNG as "1x1 pixel,
    # #FF0000". `resolve_model` already knows how to prefer a vision model; the
    # Responses route just never told it.
    has_images = input_has_images(body.get("input"))
    local_model, route_reason = resolve_model(
        requested_model, model_map, loaded_names, ondisk_names, auto_route=auto_route,
        has_images=has_images,
    )
    if not local_model:
        return _error(
            404,
            f"No local model available to serve '{requested_model}'. Pull a "
            "chat/coding model (e.g. 'ollama pull qwen3-coder:30b').",
            etype="not_found_error",
        )
    logger.info(
        f"Responses resolved {requested_model or '(none)'} → {local_model} ({route_reason})"
    )

    # Translate to an OpenAI-shaped body — the pipeline treats it like a
    # /v1/chat/completions request from here on.
    openai_body = responses_to_openai_body({**body, "model": local_model})
    # Which tools arrived as Responses `custom` tools. Pulled off the body so it
    # never reaches Ollama; used below so their calls come back as
    # `custom_tool_call` items, which is what Codex requires.
    custom_tool_names = set(openai_body.pop("_custom_tool_names", []) or [])
    # Every tool we actually offered the model. A call to anything outside this
    # set is a nested code-mode tool the model reached for directly — see
    # `redirect_nested_tool_call`.
    known_tool_names = set(openai_body.pop("_known_tool_names", []) or [])
    if not openai_body.get("messages"):
        return _error(400, "`input` produced no messages — nothing to send to the model")

    tags = extract_tags(body, request.headers)
    inference_req = InferenceRequest(
        model=local_model,
        original_model=local_model,
        messages=openai_body["messages"],
        stream=stream,
        temperature=openai_body.get("temperature", 0.7),
        max_tokens=openai_body.get("max_tokens"),
        original_format=RequestFormat.RESPONSES,
        raw_body=openai_body,
        tags=tags,
        client_ip=client_host,
    )
    rid = inference_req.request_id[:8]

    logger.info(
        f"Responses[{rid}] request: model={requested_model} → {local_model} "
        f"stream={stream} tools={len(openai_body.get('tools') or [])}"
        + (f" custom={sorted(custom_tool_names)}" if custom_tool_names else "")
        + (f"{[t['function']['name'] for t in openai_body['tools']]} "
           if openai_body.get("tools") else " ")
        + 
        f"msgs={len(openai_body['messages'])} from={client_host or '?'}"
        + (f" tags={tags}" if tags else "")
    )

    # MLX is not served here in v1.  Auto-routing already filters `mlx:` out of
    # the candidates, so this only fires when an operator *explicitly* mapped a
    # Codex alias to an mlx: model — worth a precise error rather than a
    # confusing downstream failure.  Serving MLX needs an OpenAI-SSE →
    # Responses front-end (mlx_lm.server speaks OpenAI SSE, not Ollama NDJSON);
    # see docs/plans/codex-responses-api-support.md § v2.
    if is_mlx_model(local_model):
        return _error(
            503,
            f"'{local_model}' is an MLX-backed model, which /v1/responses does "
            "not serve yet. Map this Codex alias to an Ollama-backed model, or "
            "remove the explicit mapping to let auto-routing choose one.",
            etype="overloaded_error",
        )

    scorer = request.app.state.scorer
    queue_mgr = request.app.state.queue_mgr
    proxy = request.app.state.streaming_proxy

    allow_fallback = parse_allow_fallback(body, request.headers)
    results, actual_model = await score_with_fallbacks(
        inference_req, scorer, queue_mgr, registry, proxy=proxy, settings=settings,
        allow_fallback=allow_fallback,
    )
    if not results:
        if local_model not in get_all_fleet_models(registry):
            return _error(
                404,
                f"Model '{local_model}' (resolved from '{requested_model}') is not "
                f"available on any node. Run 'ollama pull {local_model}'.",
                etype="not_found_error",
            )
        return _error(
            503,
            f"Model '{local_model}' exists but no node can serve it right now.",
            etype="overloaded_error",
        )

    if actual_model != local_model:
        logger.info(f"Responses[{rid}] fallback: {local_model} → {actual_model}")
        inference_req.model = actual_model
        inference_req.raw_body["model"] = actual_model

    winner = results[0]
    logger.info(
        f"Responses[{rid}] routed to node={winner.node_id} score={int(winner.score)}"
    )
    entry = QueueEntry(
        request=inference_req,
        assigned_node=winner.node_id,
        routing_score=winner.score,
        routing_breakdown=winner.scores_breakdown,
        fallback_used=actual_model != local_model,
    )
    queue_key = winner.queue_key

    process_fn = proxy.make_process_fn(
        queue_key, queue_mgr, scorer=scorer, settings=settings
    )
    try:
        response_future = await queue_mgr.enqueue(entry, process_fn)
    except ClientConcurrencyExceeded as e:
        return client_concurrency_response(e)
    ollama_stream = await response_future

    _extra = check_context_overflow(winner, inference_req, registry)
    headers = {"Cache-Control": "no-cache", "Connection": "keep-alive"}
    headers.update(fleet_headers(
        node_id=winner.node_id,
        served_model=actual_model,
        requested_model=local_model,
        backend="mlx" if actual_model.startswith("mlx:") else "ollama",
        score=winner.score,
        retries=entry.retry_count,
        extra=_extra,
    ))

    if stream:
        async def _sse_generator():
            state = ResponsesSSEState(
                model=actual_model, custom_names=custom_tool_names,
                known_names=known_tool_names
            )
            try:
                async for line in ollama_stream:
                    for event in ollama_chunk_to_responses_events(line, state):
                        yield event
                if not state.finished:
                    # Ollama dropped before done:true — synthesize a terminal
                    # event so Codex doesn't hang waiting for response.completed.
                    logger.warning(
                        f"Responses[{rid}] stream ended without done:true on "
                        f"node={winner.node_id} — synthesizing response.completed"
                    )
                    for event in ollama_chunk_to_responses_events(
                        '{"done":true,"done_reason":"stop"}', state
                    ):
                        yield event
            except Exception as exc:  # noqa: BLE001 — log and re-raise for FastAPI
                logger.exception(
                    f"Responses[{rid}] stream aborted on node={winner.node_id}: "
                    f"{type(exc).__name__}: {exc}"
                )
                raise
            finally:
                proxy.pop_token_counts(inference_req.request_id)
                proxy.pop_request_meta(inference_req.request_id)
                logger.info(
                    f"Responses[{rid}] stream done: node={winner.node_id} "
                    f"tools={len(state.emitted_tools)} "
                    f"out_tok={state.output_tokens} "
                    f"elapsed_ms={(time.time() - t_start) * 1000:.0f}"
                )
                if looks_like_abandoned_preamble(
                    "".join(state.text_parts), len(state.emitted_tools),
                    len(openai_body.get("tools") or []),
                ):
                    # The turn succeeded by every server-side measure; it is the
                    # agentic loop that ended early. Nothing else surfaces this.
                    logger.warning(
                        f"Responses[{rid}] ABANDONED PREAMBLE: the model announced "
                        f"an action and ended the turn without calling a tool, so "
                        f"Codex will stop here. Text: "
                        f"{''.join(state.text_parts).strip()[:120]!r}"
                    )

        return StreamingResponse(
            _sse_generator(), media_type="text/event-stream", headers=headers,
        )

    # Non-streaming: collect the NDJSON lines, then build one Responses object.
    chunks: list[str] = []
    try:
        async for line in ollama_stream:
            chunks.append(line)
    except Exception as exc:  # noqa: BLE001 — surface the failure with context
        logger.exception(
            f"Responses[{rid}] non-streaming failed on node={winner.node_id}: "
            f"{type(exc).__name__}: {exc}"
        )
        client_err = client_error_passthrough(exc, model=requested_model)
        if client_err is not None:
            return client_err
        raise
    finally:
        proxy.pop_token_counts(inference_req.request_id)
        proxy.pop_request_meta(inference_req.request_id)

    response = accumulate_responses_object(
        chunks, model=actual_model, custom_names=custom_tool_names,
        known_names=known_tool_names
    )
    _text = "".join(
        c.get("text", "")
        for o in (response.get("output") or []) if o.get("type") == "message"
        for c in (o.get("content") or [])
    )
    _calls = sum(
        1 for o in (response.get("output") or [])
        if o.get("type") in ("function_call", "custom_tool_call")
    )
    if looks_like_abandoned_preamble(
        _text, _calls, len(openai_body.get("tools") or [])
    ):
        logger.warning(
            f"Responses[{rid}] ABANDONED PREAMBLE: the model announced an action "
            f"and ended the turn without calling a tool, so Codex will stop here. "
            f"Text: {_text.strip()[:120]!r}"
        )
    usage = response.get("usage") or {}
    logger.info(
        f"Responses[{rid}] done: node={winner.node_id} "
        f"status={response.get('status')} items={len(response.get('output') or [])} "
        f"in_tok={usage.get('input_tokens', 0)} out_tok={usage.get('output_tokens', 0)} "
        f"elapsed_ms={(time.time() - t_start) * 1000:.0f}"
    )
    return JSONResponse(content=response, headers=headers)
