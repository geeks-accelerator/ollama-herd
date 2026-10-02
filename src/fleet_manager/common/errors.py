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

__all__ = ["describe_exception"]


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
