"""Tests for the MLX proxy — OpenAI SSE → Anthropic SSE translation + helpers."""

from __future__ import annotations

import json

import pytest

from fleet_manager.models.request import InferenceRequest, RequestFormat
from fleet_manager.server.anthropic_translator import AnthropicSSEState
from fleet_manager.server.mlx_proxy import (
    MlxModelMissingError,
    MlxProxy,
    _MlxToolState,
    build_anthropic_non_streaming_response,
    is_mlx_model,
    openai_sse_to_anthropic_events,
    strip_mlx_prefix,
)

# ---------------------------------------------------------------------------
# Simple string helpers
# ---------------------------------------------------------------------------


def test_is_mlx_model_positive():
    assert is_mlx_model("mlx:Qwen3-Coder-480B-A35B-4bit")
    assert is_mlx_model("mlx:anything")


def test_is_mlx_model_negative():
    assert not is_mlx_model("qwen3-coder:30b")
    assert not is_mlx_model("gpt-oss:120b")
    assert not is_mlx_model("")
    assert not is_mlx_model("MLX:uppercase-doesnt-match")  # prefix is lowercase


def test_strip_mlx_prefix():
    assert strip_mlx_prefix("mlx:foo") == "foo"
    assert strip_mlx_prefix("mlx:Qwen3-Coder-480B-A35B-4bit") == "Qwen3-Coder-480B-A35B-4bit"
    # Non-MLX names pass through unchanged
    assert strip_mlx_prefix("qwen3-coder:30b") == "qwen3-coder:30b"
    assert strip_mlx_prefix("") == ""


# ---------------------------------------------------------------------------
# MlxProxy._to_openai_body — Ollama → OpenAI body translation
# ---------------------------------------------------------------------------


def _make_request(**overrides) -> InferenceRequest:
    defaults = dict(
        model="mlx:Test-Model-4bit",
        original_model="mlx:Test-Model-4bit",
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
        temperature=0.7,
        max_tokens=100,
        original_format=RequestFormat.ANTHROPIC,
        raw_body={
            "model": "mlx:Test-Model-4bit",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
            "keep_alive": -1,
            "options": {"num_predict": 100, "temperature": 0.7, "top_p": 0.9},
        },
    )
    defaults.update(overrides)
    return InferenceRequest(**defaults)


def test_to_openai_body_strips_mlx_prefix():
    req = _make_request()
    body = MlxProxy._to_openai_body(req)
    assert body["model"] == "Test-Model-4bit"


def test_to_openai_body_flattens_options_to_top_level():
    req = _make_request()
    body = MlxProxy._to_openai_body(req)
    # Ollama options.num_predict → OpenAI max_tokens
    assert body["max_tokens"] == 100
    assert body["temperature"] == 0.7
    assert body["top_p"] == 0.9
    # OpenAI doesn't want the options wrapper
    assert "options" not in body
    # No keep_alive leaking through
    assert "keep_alive" not in body


def test_to_openai_body_preserves_messages_and_stream():
    req = _make_request()
    body = MlxProxy._to_openai_body(req)
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["stream"] is True


# ---------------------------------------------------------------------------
# MlxModelMissingError guard — defends against the empty-model 404 incident
# ---------------------------------------------------------------------------


def test_to_openai_body_raises_on_empty_model():
    """Empty model name must raise instead of letting mlx_lm.server 404 us."""
    req = _make_request(model="", original_model="")
    with pytest.raises(MlxModelMissingError) as exc_info:
        MlxProxy._to_openai_body(req)
    assert "empty model" in str(exc_info.value).lower()
    # The error message should include the request_id for traceability
    assert req.request_id in str(exc_info.value)


def test_to_openai_body_raises_on_just_mlx_prefix():
    """`mlx:` with nothing after strips to empty → must raise."""
    req = _make_request(model="mlx:", original_model="mlx:")
    with pytest.raises(MlxModelMissingError):
        MlxProxy._to_openai_body(req)


def test_to_openai_body_passes_with_normal_model():
    """Sanity: well-formed request must not raise."""
    req = _make_request()  # default has mlx:Test-Model-4bit
    body = MlxProxy._to_openai_body(req)
    assert body["model"] == "Test-Model-4bit"


def test_to_openai_body_converts_ollama_tools_to_openai_schema():
    req = _make_request(
        raw_body={
            "model": "mlx:foo",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
            "tools": [
                {
                    "name": "list_dir",
                    "description": "List files",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                },
            ],
        }
    )
    body = MlxProxy._to_openai_body(req)
    assert len(body["tools"]) == 1
    tool = body["tools"][0]
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "list_dir"
    assert tool["function"]["parameters"]["type"] == "object"


def test_to_openai_body_passes_through_already_wrapped_tools():
    req = _make_request(
        raw_body={
            "model": "mlx:foo",
            "messages": [],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "x", "parameters": {}},
                },
            ],
        }
    )
    body = MlxProxy._to_openai_body(req)
    assert body["tools"][0]["type"] == "function"
    assert body["tools"][0]["function"]["name"] == "x"


# ---------------------------------------------------------------------------
# openai_sse_to_anthropic_events — streaming translator
# ---------------------------------------------------------------------------


def _parse_sse(events: list[str]) -> list[tuple[str, dict]]:
    """Parse SSE event strings into (event_name, data_dict) tuples."""
    out: list[tuple[str, dict]] = []
    for raw in events:
        lines = raw.strip().split("\n")
        event_name = ""
        data_str = ""
        for line in lines:
            if line.startswith("event: "):
                event_name = line[7:]
            elif line.startswith("data: "):
                data_str = line[6:]
        out.append((event_name, json.loads(data_str) if data_str else {}))
    return out


def test_stream_first_chunk_emits_message_start_and_text_block():
    state = AnthropicSSEState(model="claude-sonnet-4-5")
    tools_state: dict[int, _MlxToolState] = {}
    line = 'data: {"id":"cmpl-1","choices":[{"delta":{"content":"Hi"},"index":0}]}'
    events = openai_sse_to_anthropic_events(line, state, tools_state, "req-1")
    parsed = _parse_sse(events)
    event_names = [e[0] for e in parsed]
    # First streaming chunk opens the message + a text content block + delta
    assert "message_start" in event_names
    assert "content_block_start" in event_names
    assert "content_block_delta" in event_names
    assert state.started is True
    assert state.text_open is True
    # The message_start should expose the Anthropic-facing model name
    msg_start = next(p for p in parsed if p[0] == "message_start")
    assert msg_start[1]["message"]["model"] == "claude-sonnet-4-5"


def test_stream_subsequent_text_chunks_only_emit_delta():
    state = AnthropicSSEState(model="claude-sonnet-4-5")
    tools_state: dict[int, _MlxToolState] = {}
    openai_sse_to_anthropic_events(
        'data: {"id":"1","choices":[{"delta":{"content":"Hi"},"index":0}]}',
        state, tools_state, "req-1",
    )
    # Second text chunk
    events = openai_sse_to_anthropic_events(
        'data: {"id":"1","choices":[{"delta":{"content":" there"},"index":0}]}',
        state, tools_state, "req-1",
    )
    parsed = _parse_sse(events)
    event_names = [e[0] for e in parsed]
    assert event_names == ["content_block_delta"]
    assert parsed[0][1]["delta"]["text"] == " there"


def test_stream_finish_reason_closes_block_and_emits_stop():
    state = AnthropicSSEState(model="m")
    tools_state: dict[int, _MlxToolState] = {}
    openai_sse_to_anthropic_events(
        'data: {"id":"1","choices":[{"delta":{"content":"Hi"},"index":0}]}',
        state, tools_state, "req-1",
    )
    events = openai_sse_to_anthropic_events(
        'data: {"id":"1","choices":[{"finish_reason":"stop","delta":{}}]}',
        state, tools_state, "req-1",
    )
    parsed = _parse_sse(events)
    event_names = [e[0] for e in parsed]
    # Should close the text block and emit delta + stop
    assert "content_block_stop" in event_names
    assert "message_delta" in event_names
    assert "message_stop" in event_names
    assert state.finished is True
    assert state.stop_reason == "end_turn"


def test_stream_tool_call_emits_tool_use_block():
    state = AnthropicSSEState(model="m")
    tools_state: dict[int, _MlxToolState] = {}
    # Opening tool call chunk
    events1 = openai_sse_to_anthropic_events(
        'data: {"id":"1","choices":[{"delta":{"tool_calls":['
        '{"index":0,"id":"call_abc","function":{"name":"list_dir","arguments":"{\\"pat"}}'
        ']},"index":0}]}',
        state, tools_state, "req-tool",
    )
    parsed = _parse_sse(events1)
    event_names = [e[0] for e in parsed]
    # Should open the message + a tool_use content block + emit a partial args delta
    assert "message_start" in event_names
    assert "content_block_start" in event_names
    cbs = next(p for p in parsed if p[0] == "content_block_start")
    assert cbs[1]["content_block"]["type"] == "tool_use"
    assert cbs[1]["content_block"]["name"] == "list_dir"
    assert cbs[1]["content_block"]["id"] == "call_abc"
    # Partial JSON arg fragment should be there
    cbd = next(p for p in parsed if p[0] == "content_block_delta")
    assert cbd[1]["delta"]["type"] == "input_json_delta"
    assert cbd[1]["delta"]["partial_json"] == '{"pat'

    # Continuation of arg JSON
    events2 = openai_sse_to_anthropic_events(
        'data: {"id":"1","choices":[{"delta":{"tool_calls":['
        '{"index":0,"function":{"arguments":"h\\":\\"/tmp\\"}"}}'
        ']},"index":0}]}',
        state, tools_state, "req-tool",
    )
    parsed2 = _parse_sse(events2)
    assert parsed2[0][0] == "content_block_delta"
    assert parsed2[0][1]["delta"]["partial_json"] == 'h":"/tmp"}'

    # Finish with tool_calls reason
    events3 = openai_sse_to_anthropic_events(
        'data: {"id":"1","choices":[{"finish_reason":"tool_calls","delta":{}}]}',
        state, tools_state, "req-tool",
    )
    parsed3 = _parse_sse(events3)
    event_names3 = [e[0] for e in parsed3]
    assert "content_block_stop" in event_names3
    assert "message_stop" in event_names3
    assert state.stop_reason == "tool_use"
    # emitted_tools should have been populated so downstream logging sees it
    assert len(state.emitted_tools) == 1
    assert state.emitted_tools[0]["name"] == "list_dir"


def test_stream_done_marker_yields_no_events():
    state = AnthropicSSEState(model="m")
    tools_state: dict[int, _MlxToolState] = {}
    events = openai_sse_to_anthropic_events(
        "data: [DONE]", state, tools_state, "req-done",
    )
    assert events == []


def test_stream_malformed_json_yields_no_events():
    state = AnthropicSSEState(model="m")
    tools_state: dict[int, _MlxToolState] = {}
    events = openai_sse_to_anthropic_events(
        "data: {not json", state, tools_state, "req-bad",
    )
    assert events == []


def test_stream_empty_line_yields_no_events():
    state = AnthropicSSEState(model="m")
    tools_state: dict[int, _MlxToolState] = {}
    assert openai_sse_to_anthropic_events("", state, tools_state, "r") == []
    assert openai_sse_to_anthropic_events("   ", state, tools_state, "r") == []


# ---------------------------------------------------------------------------
# build_anthropic_non_streaming_response — one-shot translation
# ---------------------------------------------------------------------------


def test_non_streaming_text_only():
    openai_resp = {
        "id": "cmpl-xyz",
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "Hello!"},
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }
    out = build_anthropic_non_streaming_response(openai_resp, "claude-sonnet-4-5")
    assert out["type"] == "message"
    assert out["model"] == "claude-sonnet-4-5"
    assert out["stop_reason"] == "end_turn"
    assert len(out["content"]) == 1
    assert out["content"][0] == {"type": "text", "text": "Hello!"}
    assert out["usage"]["input_tokens"] == 5
    assert out["usage"]["output_tokens"] == 2


def test_non_streaming_tool_call():
    openai_resp = {
        "id": "cmpl-xyz",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {
                                "name": "list_dir",
                                "arguments": '{"path":"/tmp"}',
                            },
                        }
                    ],
                },
            }
        ],
    }
    out = build_anthropic_non_streaming_response(openai_resp, "claude-opus-4-7")
    assert out["stop_reason"] == "tool_use"
    assert len(out["content"]) == 1
    block = out["content"][0]
    assert block["type"] == "tool_use"
    assert block["name"] == "list_dir"
    assert block["input"] == {"path": "/tmp"}
    assert block["id"] == "call_1"


def test_non_streaming_length_finish_maps_to_max_tokens():
    openai_resp = {
        "id": "1",
        "choices": [
            {"finish_reason": "length", "message": {"content": "cut off"}},
        ],
    }
    out = build_anthropic_non_streaming_response(openai_resp, "claude-haiku-4-5")
    assert out["stop_reason"] == "max_tokens"


def test_non_streaming_malformed_tool_arguments_does_not_crash():
    # When arguments isn't valid JSON, we should fall back to a _raw wrapper rather than raising
    openai_resp = {
        "id": "1",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "tool_calls": [
                        {"id": "c", "function": {"name": "x", "arguments": "not json"}}
                    ],
                },
            }
        ],
    }
    out = build_anthropic_non_streaming_response(openai_resp, "m")
    assert out["content"][0]["type"] == "tool_use"
    assert out["content"][0]["input"] == {"_raw": "not json"}


# ---------------------------------------------------------------------------
# MlxProxy async methods (mocked HTTP)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_healthy_returns_true_on_200(httpx_mock):
    httpx_mock.add_response(
        url="http://localhost:11440/v1/models",
        json={"object": "list", "data": []},
        status_code=200,
    )
    proxy = MlxProxy("http://localhost:11440")
    try:
        assert await proxy.is_healthy() is True
    finally:
        await proxy.close()


@pytest.mark.asyncio
async def test_is_healthy_returns_false_on_non_200(httpx_mock):
    httpx_mock.add_response(
        url="http://localhost:11440/v1/models",
        status_code=503,
    )
    proxy = MlxProxy("http://localhost:11440")
    try:
        # 503 responses return False (not raising)
        assert await proxy.is_healthy() is False
    finally:
        await proxy.close()


@pytest.mark.asyncio
async def test_list_models_returns_ids(httpx_mock):
    httpx_mock.add_response(
        url="http://localhost:11440/v1/models",
        json={
            "object": "list",
            "data": [
                {"id": "mlx-community/Qwen3-Coder-30B-A3B-4bit", "object": "model"},
                {"id": "mlx-community/Other-Model", "object": "model"},
            ],
        },
    )
    proxy = MlxProxy("http://localhost:11440")
    try:
        models = await proxy.list_models()
        assert models == [
            "mlx-community/Qwen3-Coder-30B-A3B-4bit",
            "mlx-community/Other-Model",
        ]
    finally:
        await proxy.close()


@pytest.mark.asyncio
async def test_list_models_returns_empty_on_error(httpx_mock):
    httpx_mock.add_response(
        url="http://localhost:11440/v1/models",
        status_code=500,
    )
    proxy = MlxProxy("http://localhost:11440")
    try:
        assert await proxy.list_models() == []
    finally:
        await proxy.close()


# ---------------------------------------------------------------------------
# _ollama_messages_to_openai — strict format conversion for mlx_lm.server
# ---------------------------------------------------------------------------


def test_ollama_to_openai_passthrough_simple_messages():
    """Plain string-content messages pass through unchanged."""
    from fleet_manager.server.mlx_proxy import _ollama_messages_to_openai
    msgs = [
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello back"},
    ]
    out = _ollama_messages_to_openai(msgs)
    assert out == msgs


def test_ollama_to_openai_stringifies_tool_call_arguments():
    """The historical 33-failure trigger: arguments dict → JSON string."""
    from fleet_manager.server.mlx_proxy import _ollama_messages_to_openai
    msgs = [
        {
            "role": "assistant",
            "content": "calling Read",
            "tool_calls": [{
                "function": {"name": "Read", "arguments": {"path": "/foo"}},
            }],
        },
    ]
    out = _ollama_messages_to_openai(msgs)
    tc = out[0]["tool_calls"][0]
    # arguments must be a JSON-encoded STRING for mlx_lm.server
    assert isinstance(tc["function"]["arguments"], str)
    import json
    assert json.loads(tc["function"]["arguments"]) == {"path": "/foo"}
    # Must have id and type (OpenAI required wrappers)
    assert tc["type"] == "function"
    assert tc["id"].startswith("call_")


def test_ollama_to_openai_preserves_string_arguments():
    """If arguments is already a string (rare but valid), keep it."""
    from fleet_manager.server.mlx_proxy import _ollama_messages_to_openai
    msgs = [{
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": "call_abc",
            "type": "function",
            "function": {"name": "X", "arguments": '{"already":"string"}'},
        }],
    }]
    out = _ollama_messages_to_openai(msgs)
    assert out[0]["tool_calls"][0]["function"]["arguments"] == '{"already":"string"}'
    assert out[0]["tool_calls"][0]["id"] == "call_abc"  # preserved
    # Null content gets normalized to "" (OpenAI quirk)
    assert out[0]["content"] == ""


def test_ollama_to_openai_drops_images_field():
    """Ollama-only `images` array isn't accepted by mlx_lm.server."""
    from fleet_manager.server.mlx_proxy import _ollama_messages_to_openai
    msgs = [{"role": "user", "content": "see this", "images": ["base64..."]}]
    out = _ollama_messages_to_openai(msgs)
    assert "images" not in out[0]
    assert out[0]["content"] == "see this"
