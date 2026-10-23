"""One definition of how an exception becomes a human- and machine-readable string.

Exceptions reach three places that all need a *non-empty* description: the
JSONL log, the ``error_message`` column in ``request_traces``, and — via
``_categorize_error`` — the anonymous community telemetry error histogram.

The trap is that several exception classes we hit constantly stringify to the
empty string.  ``httpx.ReadTimeout()``, ``httpx.ConnectTimeout()`` and
``asyncio.CancelledError()`` all have ``str(exc) == ""``, so the obvious
``error_message=str(exc)`` records a failure with no cause.  That is how the
first real community telemetry payload arrived carrying ``errors:
{"unknown": 1}``: ``_categorize_error`` returns ``"unknown"`` for any falsy
message, so a missing message at the source looked like a categorisation gap
in the histogram (``docs/issues.md`` — MLX empty ``error_message``).

``describe_exception`` is deliberately a no-op when the exception already has
a message, so paths that previously recorded ``str(exc)`` keep recording the
exact same bytes.  It only changes the empty case, where it substitutes the
class name — which is both the most useful thing available and, not by
accident, categorisable: ``ReadTimeout`` contains "timeout", and
``_categorize_error`` grew matching rules for the rest.
"""

from __future__ import annotations

__all__ = [
    "UPSTREAM_INCOMPLETE",
    "UPSTREAM_INCOMPLETE_MESSAGE",
    "UpstreamIncomplete",
    "describe_exception",
    "describe_upstream_incomplete",
    "error_body",
]


def describe_exception(exc: BaseException | None) -> str | None:
    """Render ``exc`` as a non-empty description, or ``None`` if there is no error.

    Returns ``str(exc)`` **unchanged** whenever that is non-empty.  That is the
    point: this drops into an existing ``error_message=str(exc)`` without
    shifting a single message that already worked, so no already-correct
    category in the published telemetry histogram can move.  Only the empty
    case changes, where the class name (``"ReadTimeout"``) replaces ``""``.
    """
    if exc is None:
        return None
    detail = str(exc).strip()
    return detail or type(exc).__name__


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

UPSTREAM_INCOMPLETE = "upstream_incomplete"
"""The ``done_reason`` meaning *the backend stream ended before completing*."""

UPSTREAM_INCOMPLETE_MESSAGE = (
    "The backend stream ended before the response completed, so the output is "
    "truncated. Retry the request."
)
"""What a client is told.  Deliberately actionable: the only useful response
to a truncated answer is to ask again."""


class UpstreamIncomplete(Exception):  # noqa: N818 -- named for the condition, like the constant
    """Raised when a whole non-streaming response is built from a stream that
    never completed.  There is no honest response object to return, so the
    route answers 502 instead of a 200 carrying partial content."""


def describe_upstream_incomplete(backend: str, *, produced_output: bool) -> str:
    """The trace ``error_message`` for a stream that ended before completing.

    **Must stay free of digits.**  ``_categorize_error`` files a message by
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
