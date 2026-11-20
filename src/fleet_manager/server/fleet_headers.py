"""Canonical ``X-Fleet-*`` response headers — one builder for every route.

Before this, each route (openai/ollama/anthropic/image/transcription/embedding)
copy-pasted its own header block with a *different* subset of keys and
semantics: some set ``X-Fleet-Fallback`` (to a model *name*, only when a
fallback happened), some set ``X-Fleet-Model`` vs ``X-Fleet-Node``, only the
anthropic-mlx path set ``X-Fleet-Backend``.  A client couldn't rely on any of
it.

This module defines the single canonical set every proxied inference response
emits, so a caller reads the same three headers — ``X-Fleet-Served-Model``,
``X-Fleet-Requested-Model``, ``X-Fleet-Fallback`` — on *any* endpoint and knows
exactly what ran.  See ``docs/plans/client-ergonomics-from-agent-feedback.md``.

``X-Request-Id`` is set separately, by ``RequestIdMiddleware``, because it
belongs on error responses too: a route binds the id it traces under with
``bind_request_id`` and every response that request gets carries it.
"""

from __future__ import annotations

import uuid

from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "X-Request-Id"


def fleet_headers(
    *,
    node_id: str,
    served_model: str,
    requested_model: str,
    backend: str = "ollama",
    score: int | float | None = None,
    retries: int = 0,
    affinity: str | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build the canonical ``X-Fleet-*`` header set.

    Always present (every route, streaming and non-streaming):
      - ``X-Fleet-Node``            — node id that served the request
      - ``X-Fleet-Served-Model``    — the model that actually ran
      - ``X-Fleet-Requested-Model`` — what the client asked for
      - ``X-Fleet-Fallback``        — ``"true"``/``"false"`` (a real signal,
                                       derived from served != requested)
      - ``X-Fleet-Backend``         — ``ollama`` / ``mlx`` / ``native`` / ``vision``
      - ``X-Fleet-Retries``         — retry count (``"0"`` when none)
      - ``X-Fleet-Affinity``        — ``matched`` when the request was routed
        back to the node already holding this conversation, ``new`` otherwise.
        Omitted entirely when affinity did not apply (no session key, or a
        route that does not score, such as a direct MLX passthrough).

        **This reports our routing decision, not a cache hit.**  We cannot
        observe whether the backend actually reused its prefix cache: Ollama
        folds llama.cpp's ``cache_n`` back into ``prompt_n`` on purpose
        (ollama/ollama#16428), so ``prompt_eval_count`` cannot yield a hit
        ratio.  Inferring one from it produced a false "zero prefix-cache
        reuse" report here once already.  TTFT is the honest proof: a matched
        turn shows a large drop.

    Conditional:
      - ``X-Fleet-Score``           — only when scorer-routed (omit on
                                       direct-proxy paths like embeddings)

    ``extra`` merges caller-specific headers (e.g. context-overflow, thinking).
    All values are coerced to ``str`` so the mapping is drop-in for Starlette
    responses.
    """
    headers: dict[str, str] = {
        "X-Fleet-Node": str(node_id),
        "X-Fleet-Served-Model": str(served_model),
        "X-Fleet-Requested-Model": str(requested_model),
        "X-Fleet-Fallback": "true" if served_model != requested_model else "false",
        "X-Fleet-Backend": str(backend),
        "X-Fleet-Retries": str(retries),
    }
    if score is not None:
        headers["X-Fleet-Score"] = str(int(score))
    if affinity:
        headers["X-Fleet-Affinity"] = affinity
    if extra:
        headers.update({k: str(v) for k, v in extra.items()})
    return headers


def affinity_from_breakdown(breakdown: dict | None) -> str | None:
    """Map a scoring breakdown to the ``X-Fleet-Affinity`` value.

    The scorer already records signal 8 as ``session_affinity`` in every
    ``RoutingResult``, so this needs no new plumbing: a non-zero value means
    the winning node was the one already holding this conversation.

    Returns ``None`` — omitting the header — when the request never went
    through scoring at all (direct MLX passthrough, embeddings), because
    "affinity did not apply" and "affinity missed" are different facts and
    collapsing them is the same mistake as reporting ``cached_tokens: 0``
    when the backend cannot measure it.
    """
    if not breakdown:
        return None
    if breakdown.get("session_affinity", 0):
        return "matched"
    # A new conversation sent where its shared system prompt + tools were warm.
    return "prefix" if breakdown.get("prefix_affinity", 0) else "new"


def usage_with_cached_tokens(usage: dict, cached_tokens: int | None) -> dict:
    """Add ``prompt_tokens_details.cached_tokens`` only when it is real.

    ``cached_tokens`` is a genuine OpenAI-compatible convention -- llama.cpp,
    ``mlx_lm.server``, vLLM and SGLang all emit it -- but it carries a trap
    that two of those shipped as bugs: **omit the field when the number is
    unknown, never send ``0``.**

    Zero means "the backend measured, and nothing was reused".  Absent means
    "we cannot measure".  Those are different facts, and this fleet is
    routinely in the second case: MLX reports real cached tokens, while Ollama
    folds llama.cpp's ``cache_n`` back into ``prompt_n`` on purpose
    (ollama/ollama#16428) so no hit ratio exists to report.  Emitting ``0``
    there would quietly claim every Ollama request was a cache miss.

    vLLM shipped exactly this conflation (fixed in #44383) and SGLang still
    has it.  Returning ``usage`` untouched is the honest answer.
    """
    if cached_tokens is None:
        return usage
    usage = dict(usage)
    usage["prompt_tokens_details"] = {"cached_tokens": int(cached_tokens)}
    return usage


def bind_request_id(request: Request, request_id: str | None = None) -> str:
    """Make ``request_id`` the id this request's response reports.

    Call it where the route creates the id it records in ``request_traces``
    (an ``InferenceRequest``'s, or a fresh one for the direct-proxy paths), so
    ``X-Request-Id`` and ``request_traces.request_id`` are the same value and
    ``/dashboard/api/traces?request_id=`` finds the row.  Returns the id, a new
    uuid4 when none is given.  A route that delegates to another (``/api/embed``
    to the native or vision server) leaves the binding to the route that traces.
    """
    rid = request_id or str(uuid.uuid4())
    request.state.request_id = rid
    return rid


class RequestIdMiddleware:
    """Add ``X-Request-Id`` to any response whose route bound one.

    Pure ASGI rather than ``BaseHTTPMiddleware`` so streaming responses pass
    through untouched: the header goes on ``http.response.start``, which a
    ``StreamingResponse`` sends after the route has returned and so after the
    id is bound.  Responses that never bound an id (dashboard, fleet API, model
    management) are left alone: an id nothing records would point nowhere.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # Created here so the route's ``request.state`` writes into this dict
        # even if a later layer copies the scope.
        state = scope.setdefault("state", {})

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                rid = state.get("request_id")
                if rid:
                    message.setdefault("headers", [])
                    MutableHeaders(scope=message)[REQUEST_ID_HEADER] = rid
            await send(message)

        await self.app(scope, receive, send_with_request_id)


def install_request_id_header(app: Starlette) -> None:
    """Put ``RequestIdMiddleware`` outside the whole middleware stack.

    ``app.add_middleware`` cannot: Starlette always wraps user middleware in
    ``ServerErrorMiddleware``, which answers an unhandled exception's 500 by
    calling the server's ``send`` directly -- past every layer inside it.  So
    the response most worth looking up carried no ``X-Request-Id``.  Wrapping
    what ``build_middleware_stack`` returns puts this layer outermost.  It is
    still safe there: it only adds a header on ``http.response.start`` and
    passes exceptions through untouched for ``ServerErrorMiddleware`` (inside
    it) to have already answered.
    """
    build = app.build_middleware_stack

    def build_with_request_id() -> ASGIApp:
        return RequestIdMiddleware(build())

    app.build_middleware_stack = build_with_request_id
