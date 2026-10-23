"""A backend stream that ends before completing must reach the client as a failure.

Ollama ends a successful stream with a ``done:true`` line.  When that line never
arrives, the answer is truncated.  Until 2026-10-22 herd recorded such a stream
as ``incomplete`` -- and then told the client it had **finished normally** on
every translated surface:

  OpenAI      unconditional ``data: [DONE]``; non-streaming HTTP 200, finish "stop"
  Responses   a synthesized ``response.completed``; non-streaming status completed
  Anthropic   a synthesized ``end_turn`` + ``message_stop``; non-streaming 200
  MLX paths   the same, and traced ``completed`` rather than ``incomplete``

A coding agent then acted on truncated work with no signal to retry.  The root
cause was one value: "no done line" reached every surface's ``done_reason``
mapper as ``""``, read as a normal stop.  Each surface now checks for the
missing terminal in the one place that sees the whole stream, and answers with
its protocol's real failure.

**The route-level tests are parametrized over surface and mode on purpose.**
The bug was eight sites each making the same wrong choice; a test per site is
the shape that lets a ninth slip through, which is exactly what happened to
context protection on the OpenAI surface (``test_context_protection_every_format``).
They run the real routes, proxy, ``stream_from_node`` and translators against a
fake Ollama at the HTTP layer, so the terminal logic itself is exercised -- not
a stub standing in for it.

See docs/plans/upstream-changes-2026-10.md, Phase 1.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from fleet_manager.common.errors import (
    UPSTREAM_INCOMPLETE,
    UPSTREAM_INCOMPLETE_MESSAGE,
    UpstreamIncomplete,
    describe_upstream_incomplete,
    error_body,
)
from fleet_manager.models.config import ServerSettings
from fleet_manager.node.daily_rollup import _categorize_error
from fleet_manager.server.anthropic_translator import (
    AnthropicSSEState,
    accumulate_anthropic_response,
    anthropic_stream_error,
    ollama_chunk_to_anthropic_events,
)
from fleet_manager.server.responses_translator import (
    ResponsesSSEState,
    accumulate_responses_object,
    ollama_chunk_to_responses_events,
    responses_failure_events,
)
from fleet_manager.server.streaming import StreamingProxy, openai_finish_reason
from tests.conftest import make_heartbeat
from tests.test_server.test_client_compat import _install_mock_ollama
from tests.test_server.test_routes import create_test_app
from tests.test_server.test_streaming_failures import (
    _content_chunk,
    _make_entry,
    _make_fake_stream,
    _mock_queue_mgr,
)

MODEL = "gpt-oss:120b"


def _line(content: str = "", *, done: bool = False, **extra) -> dict:
    out = {"model": MODEL, "message": {"role": "assistant", "content": content}, "done": done}
    out.update(extra)
    return out


TRUNCATED = [_line("Partial"), _line(" answer")]  # the backend dropped: no done line
COMPLETE = [
    _line("Full"),
    _line(" answer"),
    _line(done=True, done_reason="stop", prompt_eval_count=5, eval_count=2),
]
HIT_MAX_TOKENS = [
    _line("Cut by the budget"),
    _line(done=True, done_reason="length", prompt_eval_count=5, eval_count=4),
]


# ---------------------------------------------------------------------------
# The shared definitions
# ---------------------------------------------------------------------------


class TestTheTraceMessage:
    """The message reaches the published telemetry histogram via
    ``_categorize_error``, which matches substrings like "500" anywhere."""

    @pytest.mark.parametrize("backend", ["Ollama", "mlx_lm.server"])
    @pytest.mark.parametrize("produced_output", [True, False])
    def test_has_no_digits_and_keeps_its_category(self, backend, produced_output):
        msg = describe_upstream_incomplete(backend, produced_output=produced_output)
        assert not any(c.isdigit() for c in msg), msg
        # The previous message ("Stream ended without done:true from Ollama")
        # also filed as "other"; a fix here must not move the published category.
        assert _categorize_error(msg) == "other"

    def test_a_count_in_the_message_would_have_been_misfiled(self):
        """Pins *why* the counts go to the log line and not the trace."""
        assert _categorize_error("Stream ended after 500 chunks") == "server_error"

    def test_says_whether_the_client_got_anything(self):
        assert "partial" in describe_upstream_incomplete("Ollama", produced_output=True)
        assert "before any" in describe_upstream_incomplete("Ollama", produced_output=False)


class TestErrorEnvelopes:
    def test_anthropic_shape(self):
        assert error_body("m", "api_error", anthropic=True) == {
            "type": "error",
            "error": {"type": "api_error", "message": "m"},
        }

    def test_openai_shape(self):
        assert error_body("m", "server_error") == {
            "error": {"message": "m", "type": "server_error"}
        }


class TestOpenAIFinishReason:
    """The OpenAI surface had no mapper -- both paths hard-coded "stop"."""

    @pytest.mark.parametrize(
        ("done_reason", "tools", "expected"),
        [
            ("stop", False, "stop"),
            ("length", False, "length"),
            ("", False, "stop"),
            ("stop", True, "tool_calls"),
            ("length", True, "tool_calls"),
        ],
    )
    def test_maps(self, done_reason, tools, expected):
        assert openai_finish_reason(done_reason, tools) == expected

    def test_tool_calls_on_an_earlier_line_still_say_tool_calls(self):
        proxy = StreamingProxy(MagicMock())
        done = json.dumps(_line(done=True, done_reason="stop"))
        chunk = json.loads(proxy._ollama_to_openai_sse(done, MODEL, prior_tool_calls=True)[6:])
        assert chunk["choices"][0]["finish_reason"] == "tool_calls"


# ---------------------------------------------------------------------------
# Translators
# ---------------------------------------------------------------------------

SENTINEL = json.dumps({"done": True, "done_reason": UPSTREAM_INCOMPLETE})


class TestResponsesTranslator:
    def _run(self, lines: list[str]) -> list[str]:
        state = ResponsesSSEState(model=MODEL)
        return [e for line in lines for e in ollama_chunk_to_responses_events(line, state)]

    def test_sentinel_ends_as_response_failed(self):
        events = self._run([json.dumps(_line("Partial")), SENTINEL])
        assert events[-1].startswith("event: response.failed")
        assert not any(e.startswith("event: response.completed") for e in events)
        payload = json.loads(events[-1].split("data: ", 1)[1])
        assert payload["response"]["status"] == "failed"
        assert payload["response"]["error"]["message"] == UPSTREAM_INCOMPLETE_MESSAGE

    def test_a_real_stop_is_unchanged(self):
        events = self._run([json.dumps(line) for line in COMPLETE])
        assert events[-1].startswith("event: response.completed")
        payload = json.loads(events[-1].split("data: ", 1)[1])
        assert payload["response"]["status"] == "completed"
        assert payload["response"]["error"] is None

    def test_length_keeps_its_existing_mapping(self):
        events = self._run([json.dumps(line) for line in HIT_MAX_TOKENS])
        payload = json.loads(events[-1].split("data: ", 1)[1])
        assert events[-1].startswith("event: response.completed")
        assert payload["response"]["status"] == "incomplete"

    def test_failure_events_carry_an_exception_message(self):
        state = ResponsesSSEState(model=MODEL)
        events = list(responses_failure_events(state, "ReadTimeout"))
        payload = json.loads(events[-1].split("data: ", 1)[1])
        assert payload["response"]["error"]["message"] == "ReadTimeout"

    def test_failure_events_are_a_no_op_after_finishing(self):
        state = ResponsesSSEState(model=MODEL)
        for line in COMPLETE:
            list(ollama_chunk_to_responses_events(json.dumps(line), state))
        assert list(responses_failure_events(state, "late")) == []

    def test_accumulator_refuses_to_build_a_success(self):
        with pytest.raises(UpstreamIncomplete):
            accumulate_responses_object([json.dumps(_line("Partial"))], model=MODEL)


class TestAnthropicTranslator:
    def test_stream_error_is_anthropics_own_error_event(self):
        event = anthropic_stream_error()
        assert event.startswith("event: error\n")
        assert json.loads(event.split("data: ", 1)[1]) == error_body(
            UPSTREAM_INCOMPLETE_MESSAGE, "api_error", anthropic=True
        )

    def test_a_real_stop_is_unchanged(self):
        state = AnthropicSSEState(model=MODEL)
        events = [
            e
            for line in COMPLETE
            for e in ollama_chunk_to_anthropic_events(json.dumps(line), state)
        ]
        assert events[-1].startswith("event: message_stop")
        assert not state.failed

    def test_accumulator_refuses_to_build_a_success(self):
        with pytest.raises(UpstreamIncomplete):
            accumulate_anthropic_response([json.dumps(_line("Partial"))], model=MODEL)

    def test_a_done_line_without_done_reason_is_still_complete(self):
        """Completion is "a done line arrived", not "done_reason is set" -- Ollama
        does not always send one, and that must not read as a truncation."""
        out = accumulate_anthropic_response(
            [json.dumps(_line("ok")), json.dumps(_line(done=True))], model=MODEL
        )
        assert out["stop_reason"] == "end_turn"


# ---------------------------------------------------------------------------
# The proxy: one outcome definition for both dispatch paths
# ---------------------------------------------------------------------------


@patch("fleet_manager.server.streaming._create_logged_task")
class TestFinishStream:
    async def _drain(self, proxy, entry, queue_mgr):
        settings = ServerSettings(max_retries=2)  # the path production takes
        async for _ in proxy._stream_with_retry(
            entry, "node-a:phi4:14b", queue_mgr, MagicMock(), settings
        ):
            pass

    @pytest.mark.asyncio
    async def test_incomplete_is_traced_with_a_digit_free_message(self, _task):
        proxy = StreamingProxy(MagicMock())
        proxy._record_trace = MagicMock()
        entry = _make_entry()
        proxy.stream_from_node = _make_fake_stream(proxy, [_content_chunk("Hello")])
        proxy._request_partial[entry.request.request_id] = (1, 2)

        await self._drain(proxy, entry, _mock_queue_mgr())

        args, kwargs = proxy._record_trace.call_args
        assert args[4] == "incomplete"
        assert kwargs["error_message"] == describe_upstream_incomplete(
            "Ollama", produced_output=True
        )
        assert entry.request.request_id not in proxy._request_partial  # no leak

    @pytest.mark.asyncio
    async def test_zero_tokens_is_no_output_on_the_retry_path_too(self, _task):
        """0.11.0's no_output detection existed only in _stream_with_tracking,
        while production always takes _stream_with_retry (max_retries=2)."""
        proxy = StreamingProxy(MagicMock())
        proxy._record_trace = MagicMock()
        entry = _make_entry()
        done = json.dumps(
            {
                "model": "phi4:14b",
                "message": {"content": ""},
                "done": True,
                "prompt_eval_count": 10,
                "eval_count": 0,
            }
        )
        proxy.stream_from_node = _make_fake_stream(proxy, [done])

        await self._drain(proxy, entry, _mock_queue_mgr())

        assert proxy._record_trace.call_args[0][4] == "no_output"


# ---------------------------------------------------------------------------
# Every surface, end to end, against a fake Ollama that drops the stream
# ---------------------------------------------------------------------------


@pytest.fixture
def fleet(tmp_path):
    app = create_test_app(tmp_path=tmp_path)
    with TestClient(app) as client:
        hb = make_heartbeat(
            node_id="studio", loaded_models=[(MODEL, 65.0)], available_models=[MODEL]
        )
        client.post("/heartbeat", json=hb.model_dump())
        yield client


def _backend(client: TestClient, lines: list[dict]) -> None:
    body = "".join(json.dumps(line) + "\n" for line in lines).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in ("/api/chat", "/api/generate"):
            return httpx.Response(
                200, content=body, headers={"content-type": "application/x-ndjson"}
            )
        return httpx.Response(200, json={})

    _install_mock_ollama(client, "studio", handler)


MESSAGES = [{"role": "user", "content": "hi"}]
REQUESTS = {
    "openai": ("/v1/chat/completions", {"model": MODEL, "messages": MESSAGES}),
    "anthropic": ("/v1/messages", {"model": MODEL, "max_tokens": 64, "messages": MESSAGES}),
    "responses": ("/v1/responses", {"model": MODEL, "input": "hi"}),
}
SURFACES = list(REQUESTS)


def _post(client, surface: str, *, stream: bool):
    path, body = REQUESTS[surface]
    return client.post(path, json={**body, "stream": stream})


def _sse_payloads(text: str) -> list[dict]:
    out = []
    for raw in text.split("\n"):
        if raw.startswith("data: ") and raw.strip() != "data: [DONE]":
            out.append(json.loads(raw[6:]))
    return out


class TestTruncatedStreamIsAFailureOnEverySurface:
    @pytest.mark.parametrize("surface", SURFACES)
    def test_streaming_ends_with_an_error_terminal(self, fleet, surface):
        _backend(fleet, TRUNCATED)
        resp = _post(fleet, surface, stream=True)
        text = resp.text
        # Partial output still reaches the client before the error.
        assert "Partial" in text
        if surface == "openai":
            assert "[DONE]" not in text
            assert _sse_payloads(text)[-1]["error"]["type"] == "server_error"
        elif surface == "anthropic":
            assert "event: error" in text
            assert "message_stop" not in text and "end_turn" not in text
        else:
            assert "event: response.failed" in text
            assert "response.completed" not in text

    @pytest.mark.parametrize("surface", SURFACES)
    def test_non_streaming_is_a_502_not_a_truncated_200(self, fleet, surface):
        _backend(fleet, TRUNCATED)
        resp = _post(fleet, surface, stream=False)
        assert resp.status_code == 502
        body = resp.json()
        if surface == "anthropic":
            assert body == error_body(UPSTREAM_INCOMPLETE_MESSAGE, "api_error", anthropic=True)
        else:
            assert body == error_body(UPSTREAM_INCOMPLETE_MESSAGE, "server_error")

    def test_ollama_native_gets_nothing_synthetic(self, fleet):
        """A native client checks `done` itself; a synthetic done line would make
        the one honest surface lie."""
        _backend(fleet, TRUNCATED)
        resp = fleet.post("/api/chat", json={"model": MODEL, "messages": MESSAGES, "stream": True})
        lines = [json.loads(x) for x in resp.text.splitlines() if x.strip()]
        assert lines and not any(line.get("done") for line in lines)
        assert UPSTREAM_INCOMPLETE not in resp.text


class TestCompleteStreamsAreUnchanged:
    """The control: a real completion must still look like one everywhere."""

    @pytest.mark.parametrize("surface", SURFACES)
    def test_streaming_ends_with_the_success_terminal(self, fleet, surface):
        _backend(fleet, COMPLETE)
        text = _post(fleet, surface, stream=True).text
        if surface == "openai":
            assert text.rstrip().endswith("data: [DONE]")
            assert _sse_payloads(text)[-1]["choices"][0]["finish_reason"] == "stop"
        elif surface == "anthropic":
            assert "message_stop" in text and "event: error" not in text
        else:
            assert "event: response.completed" in text and "response.failed" not in text

    @pytest.mark.parametrize("surface", SURFACES)
    def test_non_streaming_is_200(self, fleet, surface):
        _backend(fleet, COMPLETE)
        assert _post(fleet, surface, stream=False).status_code == 200


class TestOpenAIReportsLength:
    """Hitting max_tokens is a truncation the client must be able to see."""

    def test_streaming(self, fleet):
        _backend(fleet, HIT_MAX_TOKENS)
        text = _post(fleet, "openai", stream=True).text
        assert _sse_payloads(text)[-1]["choices"][0]["finish_reason"] == "length"

    def test_non_streaming(self, fleet):
        _backend(fleet, HIT_MAX_TOKENS)
        body = _post(fleet, "openai", stream=False).json()
        assert body["choices"][0]["finish_reason"] == "length"


# ---------------------------------------------------------------------------
# The MLX paths: the same failure, and before this the trace lied too
# ---------------------------------------------------------------------------

MLX_MODEL = "mlx:test-model"
MLX_PARTIAL = b'data: {"choices":[{"index":0,"delta":{"content":"Partial"},"finish_reason":null}]}'
MLX_STOP = b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}'
MLX_TRUNCATED = [MLX_PARTIAL]  # mlx_lm.server dropped: no finish_reason, no [DONE]
MLX_COMPLETE = [MLX_PARTIAL, MLX_STOP, b"data: [DONE]"]


class _FakeMlxProxy:
    """The four methods the streaming MLX routes call, and nothing else."""

    def __init__(self, lines: list[bytes]):
        self._lines = lines

    async def _acquire_slot(self, _key):
        return None

    def _release_slot(self, _key):
        return None

    def pop_token_counts(self, _rid):
        return (None, None, None)

    async def stream_openai(self, _req, already_admitted=False):
        for line in self._lines:
            yield line


@pytest.fixture
def mlx_fleet(fleet, monkeypatch):
    """`fleet`, plus an MLX backend the routes will dispatch to, and the traces
    those routes record captured -- the trace status is half of what changed."""
    from fleet_manager.server.routes import anthropic_compat, openai_compat

    traces: list[tuple[str, str | None]] = []

    def capture(_store, _req, _t0, _ttft, status, error_message=None, **_kw):
        traces.append((status, error_message))

    monkeypatch.setattr(openai_compat, "record_trace_mlx", capture)
    monkeypatch.setattr(anthropic_compat, "record_trace_mlx", capture)
    monkeypatch.setattr(fleet.app.state.registry, "resolve_mlx_url", lambda _m: "http://mlx")
    fleet.app.state.settings.mlx_enabled = True
    # How Anthropic reaches MLX in production: an explicit `mlx:` map entry.
    fleet.app.state.settings.anthropic_model_map = {"claude-test": MLX_MODEL}
    fleet.traces = traces
    yield fleet


def _mlx_post(client, surface: str, lines: list[bytes]):
    client.app.state.mlx_proxy = _FakeMlxProxy(lines)
    if surface == "openai":
        return client.post(
            "/v1/chat/completions",
            json={"model": MLX_MODEL, "messages": MESSAGES, "stream": True},
        )
    return client.post(
        "/v1/messages",
        json={"model": "claude-test", "max_tokens": 64, "messages": MESSAGES, "stream": True},
    )


class TestMlxStreams:
    @pytest.mark.parametrize("surface", ["openai", "anthropic"])
    def test_truncated_is_an_error_terminal_and_traced_incomplete(self, mlx_fleet, surface):
        text = _mlx_post(mlx_fleet, surface, MLX_TRUNCATED).text
        assert "Partial" in text
        if surface == "openai":
            assert "[DONE]" not in text
            assert _sse_payloads(text)[-1]["error"]["type"] == "server_error"
        else:
            assert "event: error" in text and "end_turn" not in text
        status, message = mlx_fleet.traces[-1]
        assert status == "incomplete"  # was "completed"
        assert message == describe_upstream_incomplete("mlx_lm.server", produced_output=True)

    @pytest.mark.parametrize("surface", ["openai", "anthropic"])
    def test_complete_is_unchanged(self, mlx_fleet, surface):
        text = _mlx_post(mlx_fleet, surface, MLX_COMPLETE).text
        assert "event: error" not in text and '"error"' not in text
        assert mlx_fleet.traces[-1] == ("completed", None)


class TestOpenAIDeliversToolCallsFromTheDoneLine:
    """Route level, both modes: the call gpt-oss puts on its done line must reach
    the client.  Found while testing this module's fix live -- the earlier
    behaviour was finish_reason "tool_calls" with no call delivered."""

    DONE_WITH_CALL = [
        _line("", thinking="planning"),
        {
            "model": MODEL,
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "get_weather", "arguments": {"city": "Paris"}}}
                ],
            },
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": 5,
            "eval_count": 9,
        },
    ]

    def test_streaming(self, fleet):
        _backend(fleet, self.DONE_WITH_CALL)
        payloads = _sse_payloads(_post(fleet, "openai", stream=True).text)
        calls = [tc for p in payloads for tc in (p["choices"][0]["delta"].get("tool_calls") or [])]
        assert [c["function"]["name"] for c in calls] == ["get_weather"]
        assert json.loads(calls[0]["function"]["arguments"]) == {"city": "Paris"}
        assert payloads[-1]["choices"][0]["finish_reason"] == "tool_calls"

    def test_non_streaming(self, fleet):
        _backend(fleet, self.DONE_WITH_CALL)
        choice = _post(fleet, "openai", stream=False).json()["choices"][0]
        assert [c["function"]["name"] for c in choice["message"]["tool_calls"]] == ["get_weather"]
        assert choice["finish_reason"] == "tool_calls"
