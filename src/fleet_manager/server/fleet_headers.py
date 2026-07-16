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
"""

from __future__ import annotations


def fleet_headers(
    *,
    node_id: str,
    served_model: str,
    requested_model: str,
    backend: str = "ollama",
    score: int | float | None = None,
    retries: int = 0,
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
    if extra:
        headers.update({k: str(v) for k, v in extra.items()})
    return headers
