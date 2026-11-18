"""The backend's reason reaches the trace, the telemetry category and the client.

Two gaps, one effect.  An HTTP error was traced as httpx's text alone
(``Client error '400 Bad Request' for url ...``), so "does not support
tools", "invalid model name" and an MLX context overflow all reached the
telemetry histogram as one ``bad_request``.  And when Ollama ended a stream
with ``{"error": ...}`` instead of ``done:true``, the trace said
``incomplete`` with a generic message while every translated surface told the
client to retry, which is the wrong advice for a prompt over the model's
context limit.  See docs/plans/telemetry-errors-2026-11.md item 1 and
docs/plans/silent-failures-2026-11.md issue 2.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from fleet_manager.common.errors import (
    UPSTREAM_INCOMPLETE_MESSAGE,
    categorize_error,
    describe_backend_stream_error,
    describe_exception,
    error_body,
    ollama_stream_error,
)
from tests.conftest import make_heartbeat
from tests.test_server.test_routes import create_test_app
from tests.test_server.test_upstream_incomplete import (
    MODEL,
    SURFACES,
    _backend,
    _line,
    _post,
    _sse_payloads,
)


@pytest.fixture
def fleet(tmp_path):
    """One node serving MODEL behind a fake Ollama (``_backend`` sets its lines)."""
    app = create_test_app(tmp_path=tmp_path)
    with TestClient(app) as client:
        hb = make_heartbeat(
            node_id="studio", loaded_models=[(MODEL, 65.0)], available_models=[MODEL]
        )
        client.post("/heartbeat", json=hb.model_dump())
        yield client


OVER_CONTEXT = "input length (5000 tokens) exceeds the model's maximum context length (4096 tokens)"


def _status_error(status: int, body) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://10.0.0.5:11434/api/chat")
    content = json.dumps(body).encode() if not isinstance(body, bytes) else body
    response = httpx.Response(status, content=content, request=request)
    response.read()
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return exc
    raise AssertionError("expected an error status")


class TestDescribeException:
    def test_appends_the_backend_reason_after_the_status(self):
        exc = _status_error(400, {"error": "gemma3:27b does not support tools"})
        text = describe_exception(exc)
        assert text.startswith("Client error '400 Bad Request'")
        assert text.endswith(": gemma3:27b does not support tools")
        assert "\n" not in text  # httpx's generic MDN line is dropped

    def test_no_body_keeps_httpx_text(self):
        exc = _status_error(500, b"")
        assert describe_exception(exc) == str(exc).strip()

    def test_non_http_errors_unchanged(self):
        assert describe_exception(httpx.ConnectError("refused")) == "refused"
        assert describe_exception(httpx.ReadTimeout("")) == "ReadTimeout"


class TestCategorizeWithTheReason:
    """Ollama's real texts, each with the status it arrives with."""

    @pytest.mark.parametrize(
        "status,reason,category",
        [
            (400, "registry.ollama.ai/library/gemma3:27b does not support tools", "bad_request"),
            (400, "invalid model name", "bad_request"),
            (400, '"nomic-embed-text" does not support chat', "bad_request"),
            (404, "model 'qwen2.5-coder:14b' not found", "model_not_found"),
            (404, 'model "x" not found, try pulling it first', "model_not_found"),
            (400, OVER_CONTEXT, "context_too_long"),
            (500, OVER_CONTEXT, "context_too_long"),
            (500, "llama runner process has terminated: out of memory", "vram_exceeded"),
            # A 429 whose text holds "5000" must not become server_error.
            (429, "rate limited after 5000 tokens", "other"),
            (503, "server busy, 5000 requests queued", "server_error"),
        ],
    )
    def test_table(self, status, reason, category):
        message = describe_exception(_status_error(status, {"error": reason}))
        assert categorize_error(message) == category

    def test_herds_own_http_prefix_reads_the_same(self):
        assert categorize_error("HTTP 404: model 'x' not found") == "model_not_found"
        assert categorize_error("HTTP 429: limit 500 per minute") == "other"

    def test_stream_error_message_is_categorised_by_its_reason(self):
        assert categorize_error(describe_backend_stream_error("Ollama", OVER_CONTEXT)) == (
            "context_too_long"
        )


class TestOllamaStreamError:
    @pytest.mark.parametrize(
        "chunk,expected",
        [
            ({"error": "boom"}, "boom"),
            ({"error": {"message": "nested"}}, "nested"),
            ({"error": "  "}, None),
            ({"error": "x", "done": True}, None),
            ({"message": {"content": "hi"}}, None),
            ("not a dict", None),
        ],
    )
    def test_table(self, chunk, expected):
        assert ollama_stream_error(chunk) == expected


ENDS_WITH_ERROR = [_line("Partial"), {"error": OVER_CONTEXT}]


class TestStreamEndedWithAnErrorLine:
    @pytest.mark.parametrize("surface", SURFACES)
    def test_streaming_terminal_carries_the_reason(self, fleet, surface):
        _backend(fleet, ENDS_WITH_ERROR)
        text = _post(fleet, surface, stream=True).text
        assert OVER_CONTEXT in text
        assert UPSTREAM_INCOMPLETE_MESSAGE not in text
        if surface == "openai":
            assert "[DONE]" not in text
            assert _sse_payloads(text)[-1]["error"]["message"] == OVER_CONTEXT
        elif surface == "anthropic":
            assert "event: error" in text and "message_stop" not in text
        else:
            assert "event: response.failed" in text and "response.completed" not in text

    @pytest.mark.parametrize("surface", SURFACES)
    def test_non_streaming_502_carries_the_reason(self, fleet, surface):
        _backend(fleet, ENDS_WITH_ERROR)
        resp = _post(fleet, surface, stream=False)
        assert resp.status_code == 502
        expected = error_body(
            OVER_CONTEXT,
            "api_error" if surface == "anthropic" else "server_error",
            anthropic=surface == "anthropic",
        )
        assert resp.json() == expected

    def test_native_non_streaming_502_carries_the_reason(self, fleet):
        _backend(fleet, ENDS_WITH_ERROR)
        resp = fleet.post(
            "/api/chat",
            json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "stream": False},
        )
        assert resp.status_code == 502
        assert resp.json() == {"error": OVER_CONTEXT}

    def test_traced_failed_with_the_reason(self, fleet):
        proxy = fleet.app.state.streaming_proxy
        proxy._record_trace = MagicMock()
        _backend(fleet, ENDS_WITH_ERROR)
        _post(fleet, "openai", stream=False)
        args, kwargs = proxy._record_trace.call_args
        assert args[4] == "failed"
        assert kwargs["error_message"] == describe_backend_stream_error("Ollama", OVER_CONTEXT)
        assert categorize_error(kwargs["error_message"]) == "context_too_long"
        assert not proxy._request_backend_error  # popped, no leak

    def test_a_plain_truncation_is_still_incomplete(self, fleet):
        """`incomplete` keeps meaning: stopped without saying why."""
        proxy = fleet.app.state.streaming_proxy
        proxy._record_trace = MagicMock()
        _backend(fleet, [_line("Partial")])
        _post(fleet, "openai", stream=False)
        assert proxy._record_trace.call_args[0][4] == "incomplete"
