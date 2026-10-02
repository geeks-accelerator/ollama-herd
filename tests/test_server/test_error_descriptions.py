"""A recorded failure must carry its reason.

The MLX path wrote status='failed' with an empty error_message, which surfaced
as a dashboard failure with no cause and, worse, as an `unknown` bucket in the
published community telemetry histogram -- `_categorize_error` returns
"unknown" for any falsy message, so a missing message at the source reads as a
categorisation gap in the stats. Found via the first real community payload
carrying `errors: {"unknown": 1}`.

The cause is that the obvious `error_message=str(exc)` is empty for whole
classes of exception we hit constantly: httpx's timeouts and asyncio's
CancelledError all stringify to "".
"""

import asyncio
import pathlib

import httpx
import pytest

from fleet_manager.common.errors import describe_exception
from fleet_manager.node.daily_rollup import _categorize_error


class TestDescribeException:
    @pytest.mark.parametrize(
        "exc",
        [
            httpx.ReadTimeout(""),
            httpx.ConnectTimeout(""),
            httpx.WriteTimeout(""),
            httpx.PoolTimeout(""),
            asyncio.CancelledError(),
            httpx.RemoteProtocolError(""),
        ],
    )
    def test_the_empty_str_classes_still_produce_a_reason(self, exc):
        out = describe_exception(exc)
        assert out, f"{type(exc).__name__} must not describe as empty"
        assert out == type(exc).__name__

    def test_an_existing_message_is_returned_byte_for_byte(self):
        """This is the point of the helper being a no-op on the common path.

        It drops into the Ollama path's existing `error_message=str(exc)`
        without shifting any message that already worked, so no already-correct
        category in the published telemetry histogram can move.
        """
        exc = ValueError("Model not found: 404 not found")
        assert describe_exception(exc) == "Model not found: 404 not found"
        assert describe_exception(exc) == str(exc)

    def test_no_error_stays_no_error(self):
        assert describe_exception(None) is None

    def test_whitespace_only_counts_as_empty(self):
        assert describe_exception(ValueError("   \n ")) == "ValueError"


class TestCategorisation:
    """Every description the helper can produce must land in a real bucket."""

    @pytest.mark.parametrize(
        "exc,expected",
        [
            (httpx.ReadTimeout(""), "timeout"),
            (httpx.ConnectTimeout(""), "timeout"),
            (httpx.PoolTimeout(""), "timeout"),
            (httpx.ConnectError(""), "connection_error"),
            (httpx.RemoteProtocolError(""), "connection_error"),
            (httpx.ReadError(""), "connection_error"),
            (asyncio.CancelledError(), "client_disconnected"),
        ],
    )
    def test_bare_class_names_do_not_fall_through_to_unknown(self, exc, expected):
        assert _categorize_error(describe_exception(exc)) == expected

    @pytest.mark.parametrize(
        "msg,expected",
        [
            ("Model not found: 404 not found", "model_not_found"),
            ("400 context length exceeded", "context_too_long"),
            ("connection refused", "connection_error"),
            ("read timed out", "timeout"),
            ("client disconnected before stream completed", "client_disconnected"),
            ("502 bad gateway", "server_error"),
        ],
    )
    def test_messages_that_already_categorised_still_do(self, msg, expected):
        """Regression guard: the new rules must not capture existing messages."""
        assert _categorize_error(msg) == expected

    def test_unknown_is_reserved_for_a_genuinely_absent_message(self):
        assert _categorize_error(None) == "unknown"
        assert _categorize_error("") == "unknown"


class TestMlxTraceNeverRecordsACauselessFailure:
    def test_a_failed_status_with_no_message_gets_a_placeholder(self):
        """Chokepoint guard, so a future call site cannot reintroduce the bug.

        'other' is a truthful bucket; 'unknown' is a lie that reads as a
        categorisation gap in the published stats.
        """
        from fleet_manager.server.mlx_proxy import record_trace_mlx

        recorded = {}

        class _Store:
            async def record_trace(self, **kw):
                recorded.update(kw)

        req = type(
            "R", (),
            {"request_id": "r1", "model": "mlx:m", "original_model": None,
             "original_format": None, "tags": None},
        )()

        async def _run():
            record_trace_mlx(_Store(), req, 0.0, None, "failed", error_message="")
            await asyncio.sleep(0.05)

        asyncio.run(_run())
        assert recorded["error_message"]
        assert _categorize_error(recorded["error_message"]) != "unknown"

    def test_a_completed_status_is_not_given_a_fake_error(self):
        from fleet_manager.server.mlx_proxy import record_trace_mlx

        recorded = {}

        class _Store:
            async def record_trace(self, **kw):
                recorded.update(kw)

        req = type(
            "R", (),
            {"request_id": "r2", "model": "mlx:m", "original_model": None,
             "original_format": None, "tags": None},
        )()

        async def _run():
            record_trace_mlx(_Store(), req, 0.0, 0.0, "completed")
            await asyncio.sleep(0.05)

        asyncio.run(_run())
        assert recorded["error_message"] is None


class TestCallSitesUseTheHelper:
    """No MLX trace call site may be left on the bare `str(exc)` that caused this."""

    @pytest.mark.parametrize(
        "path",
        [
            "src/fleet_manager/server/routes/anthropic_compat.py",
            "src/fleet_manager/server/routes/openai_compat.py",
            "src/fleet_manager/server/streaming.py",
        ],
    )
    def test_no_bare_stringification_remains(self, path):
        src = pathlib.Path(path).read_text()
        assert "error_message=str(exc)" not in src
        assert "error_message=str(e) or repr(e)" not in src
        assert "describe_exception" in src
