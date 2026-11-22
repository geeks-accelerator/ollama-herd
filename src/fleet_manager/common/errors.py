"""One definition of how an exception becomes a human- and machine-readable string.

Exceptions reach three places that all need a *non-empty* description: the
JSONL log, the ``error_message`` column in ``request_traces``, and — via
``categorize_error`` — the anonymous community telemetry error histogram.

The trap is that several exception classes we hit constantly stringify to the
empty string.  ``httpx.ReadTimeout()``, ``httpx.ConnectTimeout()`` and
``asyncio.CancelledError()`` all have ``str(exc) == ""``, so the obvious
``error_message=str(exc)`` records a failure with no cause.  That is how the
first real community telemetry payload arrived carrying ``errors:
{"unknown": 1}``: ``categorize_error`` returns ``"unknown"`` for any falsy
message, so a missing message at the source looked like a categorisation gap
in the histogram (``docs/issues.md`` — MLX empty ``error_message``).

``describe_exception`` substitutes the class name when ``str(exc)`` is empty
— which is both the most useful thing available and, not by accident,
categorisable: ``ReadTimeout`` contains "timeout", and ``categorize_error``
grew matching rules for the rest.  For an HTTP status error it also appends
the backend's own message, because httpx's text names only the status and
the URL: ``does not support tools``, ``invalid model name`` and an MLX
context overflow all reached traces (and the telemetry histogram) as one
indistinguishable ``bad_request`` (docs/plans/telemetry-errors-2026-11.md).
``categorize_error`` reads the status first, so the backend's words can only
move a category toward the reason the backend gave.
"""

from __future__ import annotations

import re

__all__ = [
    "UPSTREAM_INCOMPLETE",
    "UPSTREAM_INCOMPLETE_MESSAGE",
    "UpstreamIncomplete",
    "backend_error_detail",
    "categorize_error",
    "client_error_message",
    "describe_exception",
    "describe_backend_stream_error",
    "describe_upstream_incomplete",
    "error_body",
    "ollama_stream_error",
]


def describe_exception(exc: BaseException | None) -> str | None:
    """Render ``exc`` as a non-empty description, or ``None`` if there is no error.

    For traces and logs only: an HTTP error's text carries the backend URL, so
    a client is told ``client_error_message`` instead.

    * ``str(exc)`` when it is non-empty, else the class name
      (``"ReadTimeout"``), so a failure never records an empty cause.
    * For an HTTP status error whose body says why, httpx's first line plus
      ``": "`` plus the backend's message.  The status prefix stays first,
      which is what ``categorize_error`` keys on; httpx's second line (a
      generic MDN link) is dropped.
    """
    if exc is None:
        return None
    text = str(exc).strip()
    reason = backend_error_detail(exc)
    if text and reason:
        first_line = text.splitlines()[0].rstrip()
        if reason not in first_line:
            return f"{first_line}: {reason}"
    return text or type(exc).__name__


def backend_error_detail(exc: BaseException | None) -> str | None:
    """The backend's own message from an HTTP error response, or ``None``.

    Ollama answers a failure with ``{"error": "..."}`` (mlx_lm.server and other
    OpenAI-shaped servers with ``{"error": {"message": ...}}`` or a ``message``
    key), and that text -- ``model 'x' not found``, ``x does not support
    tools`` -- is the most useful thing a client can be told.  ``None`` when
    ``exc`` is not an HTTP status error or its body carries nothing readable.

    Duck-typed on ``exc.response`` so this module stays free of httpx.  The
    body must already have been read, which ``stream_from_node`` does before
    ``raise_for_status``; an unread streaming body reads as ``None`` here.
    """
    response = getattr(exc, "response", None)
    if response is None or not hasattr(response, "status_code"):
        return None
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 -- empty, non-JSON or unread body
        pass
    else:
        # JSON without a message is not something to show anyone verbatim.
        if not isinstance(body, dict):
            return None
        err = body.get("error")
        if isinstance(err, dict):
            err = err.get("message")
        return str(err or body.get("message") or "").strip() or None
    try:
        text = (response.text or "").strip()
    except Exception:  # noqa: BLE001 -- unread streaming body
        return None
    return text[:300] or None


def client_error_message(exc: BaseException) -> str:
    """What a *client* is told about ``exc``: never a backend URL.

    ``describe_exception`` is the right text for logs and traces (and the
    traces feed ``categorize_error``, so they must keep it), but for an HTTP
    status error it is httpx's ``Client error '404 Not Found' for url
    'http://10.0.0.5:11434/api/chat'``, which hands a backend's address to
    whoever sent the request.  This prefers the backend's own message, then
    the bare status, and only falls back to ``describe_exception`` for errors
    that carry no response (connection failures, timeouts), whose text holds
    no URL.
    """
    detail = backend_error_detail(exc)
    if detail:
        return detail
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return f"backend returned HTTP {status}"
    return describe_exception(exc) or type(exc).__name__


# ---------------------------------------------------------------------------
# A backend stream that ended before it completed
# ---------------------------------------------------------------------------
#
# Ollama ends a successful stream with a ``done:true`` line; mlx_lm.server ends
# one with a ``finish_reason`` and ``[DONE]``.  When that terminal never arrives
# the response is truncated -- and every translated surface used to tell the
# client it had *finished normally*: OpenAI got an unconditional ``[DONE]``,
# Responses a synthesized ``response.completed``, Anthropic a synthesized
# ``end_turn``, and the non-streaming paths an HTTP 200 with the partial text.
# A coding agent then acted on truncated work with no signal to retry.
#
# The root cause was one missing value: "no done line arrived" reached every
# surface's ``done_reason`` mapper as ``""``, which each read as a normal stop.
# Each surface now checks for the missing terminal in the one place that sees
# the whole stream -- ``stream_from_node`` for OpenAI, the route's translator
# state for Anthropic and Responses, the accumulator for non-streaming -- and
# answers with its protocol's real failure.  ``UPSTREAM_INCOMPLETE`` is the
# ``done_reason`` that drives Responses' existing ``_map_status`` to ``failed``.
# See docs/plans/upstream-changes-2026-10.md, Phase 1.


def categorize_error(error_message: str | None) -> str:
    """Map a raw error message to a short category name.

    The categories are what the anonymous community telemetry publishes as
    its error histogram (``errors`` in the payload), so they are part of the
    wire contract: renaming one changes what ollamaherd.com receives.  Raw
    error text never leaves the machine; only these names do.

    Keep names short, snake_case, and descriptive enough that an
    operator reading "error_breakdown: {context_too_long: 12}" on the
    dashboard immediately knows what's wrong.
    """
    if not error_message:
        return "unknown"
    msg = error_message.lower()

    # A stated reason beats any number.  Backend text carries digits ("input
    # length (5000 tokens) exceeds the model's maximum context length"), which
    # the bare-status rules further down would file as server_error.
    if "out of memory" in msg or "vram" in msg or "cuda oom" in msg:
        return "vram_exceeded"
    if "context" in msg and ("too long" in msg or "exceeds" in msg):
        return "context_too_long"

    # Then the status, when the message says which: httpx's "Client error '400
    # ...'" or herd's own "HTTP 400: ...", categorised by itself rather than by
    # whatever numbers the backend's text happens to contain.
    status = _http_status(msg)
    if status is not None:
        if status == 404 and "not found" in msg:
            return "model_not_found"
        if status == 400:
            return "context_too_long" if ("context" in msg or "num_ctx" in msg) else "bad_request"
        if status in (500, 502, 503, 504):
            return "server_error"

    else:
        # No stated status: a bare number anywhere is the best evidence left.
        # Only here -- with a known status, the backend's own digits ("limit
        # 500 per minute" on a 429) must not decide the category.
        if "404" in msg and "not found" in msg:
            return "model_not_found"
        if "400" in msg and ("context" in msg or "num_ctx" in msg):
            return "context_too_long"
        if "400" in msg:
            return "bad_request"
        if "500" in msg or "502" in msg or "503" in msg or "504" in msg:
            return "server_error"

    if "permission" in msg:
        return "permission_error"

    # Network / timeout.  The bare-class-name forms matter: several httpx and
    # asyncio exceptions stringify to "", so describe_exception() records the
    # class name instead and these rules are what keep it out of "unknown"
    # (see fleet_manager.common.errors).  "ReadTimeout"/"ConnectTimeout" are
    # already caught by the "timeout" substring above.
    if "timeout" in msg or "timed out" in msg:
        return "timeout"
    if (
        "connecterror" in msg
        or "connectionerror" in msg
        or "remoteprotocolerror" in msg
        or "readerror" in msg
        or "writeerror" in msg
        or (
            "connection" in msg
            and ("refused" in msg or "reset" in msg or "closed" in msg or "aborted" in msg)
        )
    ):
        return "connection_error"

    # Client disconnects (streaming).  Starlette ends a disconnected streaming
    # response by cancelling the task, so CancelledError is the common shape --
    # and it is one of the exceptions whose str() is empty.
    if "disconnect" in msg or "generatorexit" in msg or "cancellederror" in msg:
        return "client_disconnected"

    return "other"


_HTTP_STATUS = re.compile(r"(?:client|server) error '(\d{3})|^http (\d{3}):")


def _http_status(lowered: str) -> int | None:
    """The HTTP status a lowered error message states, or ``None``."""
    m = _HTTP_STATUS.search(lowered)
    return int(m.group(1) or m.group(2)) if m else None


UPSTREAM_INCOMPLETE = "upstream_incomplete"
"""The ``done_reason`` meaning *the backend stream ended before completing*."""

UPSTREAM_INCOMPLETE_MESSAGE = (
    "The backend stream ended before the response completed, so the output is "
    "truncated. Retry the request."
)
"""What a client is told.  Deliberately actionable: the only useful response
to a truncated answer is to ask again."""


# Error categories that describe the request, not the backend, and the status
# a backend saying so would have answered with.  A node answering these is
# working: they are never blamed on it (``get_failing_backends``), and an
# Ollama stream that ends with only such an error reaches the client as that
# status instead of a retry on every peer (``StreamingProxy.stream_from_node``).
CLIENT_ERROR_STATUS = {
    "bad_request": 400,
    "context_too_long": 400,
    "permission_error": 403,
    "model_not_found": 404,
}
CLIENT_ERROR_CATEGORIES = frozenset(CLIENT_ERROR_STATUS)


def ollama_line_has_output(chunk) -> bool:
    """Does an Ollama stream line carry anything for the client?

    Content, thinking or tool calls, or the terminal ``done``.  An
    ``{"error": ...}`` line does not: a stream that sent only that produced
    nothing, and is retried or answered with a status rather than started.
    """
    if not isinstance(chunk, dict):
        return False
    if chunk.get("done"):
        return True
    msg = chunk.get("message") or {}
    return bool(
        msg.get("content") or msg.get("thinking") or msg.get("tool_calls") or chunk.get("response")
    )


class UpstreamIncomplete(Exception):  # noqa: N818 -- named for the condition, like the constant
    """Raised when a whole non-streaming response is built from a stream that
    never completed.  There is no honest response object to return, so the
    route answers 502 instead of a 200 carrying partial content."""


def ollama_stream_error(chunk) -> str | None:
    """The message of Ollama's ``{"error": ...}`` line, or ``None``.

    Ollama ends a stream it cannot finish with that line instead of
    ``done:true`` -- ``input length (N tokens) exceeds the model's maximum
    context length``, a load failure, an unparseable tool call.  One
    definition for every place that reads a stream: the proxy (trace and the
    OpenAI terminal) and each route's translator (Anthropic, Responses,
    native), so the client is told why instead of "retry".
    """
    if not isinstance(chunk, dict) or chunk.get("done"):
        return None
    err = chunk.get("error")
    if isinstance(err, dict):
        err = err.get("message")
    text = str(err or "").strip()
    return text or None


def describe_backend_stream_error(backend: str, error: str) -> str:
    """The trace ``error_message`` for a stream the backend ended with an error.

    Traced ``failed``, not ``incomplete``: the backend said why it stopped,
    and ``incomplete`` keeps meaning it stopped without saying (decided
    2026-11-17).  The backend's words are kept, bounded, because they are the
    reason; ``categorize_error`` reads stated reasons before any digits.
    """
    return f"{backend} ended the stream with an error: {error[:500]}"


def describe_upstream_incomplete(backend: str, *, produced_output: bool) -> str:
    """The trace ``error_message`` for a stream that ended before completing.

    **Must stay free of digits.**  ``categorize_error`` files a message by
    substring -- ``"500"``, ``"502"``, ``"400"`` anywhere in it -- so a chunk
    count of 500 would publish a truncated stream as ``server_error`` in the
    community telemetry histogram.  Exact counts go to the log line instead,
    which nothing categorises.  ``produced_output`` answers the one question
    an incident review asks: did the client receive anything before the drop?
    """
    where = "after partial output" if produced_output else "before any output"
    return f"Stream ended without completing from {backend} {where}"


def error_body(message: str, error_type: str, *, anthropic: bool = False) -> dict:
    """The error envelope each API surface expects, in one place.

    Anthropic: ``{"type": "error", "error": {"type": ..., "message": ...}}``.
    OpenAI (and Responses, which shares it): ``{"error": {"message": ..., "type": ...}}``.
    Used for HTTP error bodies *and* in-stream error events, so a failure looks
    the same to a client whichever way it arrives.
    """
    if anthropic:
        return {"type": "error", "error": {"type": error_type, "message": message}}
    return {"error": {"message": message, "type": error_type}}
