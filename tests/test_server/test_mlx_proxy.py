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


# ---------------------------------------------------------------------------
# Admission control — MlxQueueFullError + per-model semaphore
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acquire_slot_first_request_goes_through():
    """First request acquires immediately, counters reflect in-flight=1."""
    from fleet_manager.server.mlx_proxy import MlxProxy
    proxy = MlxProxy("http://test", max_queue_depth=3)
    await proxy._acquire_slot("model-a")
    assert proxy._inflight["model-a"] == 1
    assert proxy._queued.get("model-a", 0) == 0
    proxy._release_slot("model-a")
    assert proxy._inflight["model-a"] == 0


@pytest.mark.asyncio
async def test_acquire_slot_blocks_second_until_first_releases():
    """Second concurrent request waits on the semaphore."""
    import asyncio as aio
    from fleet_manager.server.mlx_proxy import MlxProxy
    proxy = MlxProxy("http://test", max_queue_depth=3)

    # First request: acquire and hold
    await proxy._acquire_slot("model-a")

    # Second request: start but don't await — should be in queue
    task = aio.create_task(proxy._acquire_slot("model-a"))
    await aio.sleep(0.05)  # let task run until it blocks
    # Second is queued, not yet in-flight
    assert proxy._queued["model-a"] == 1
    assert proxy._inflight["model-a"] == 1
    assert not task.done()

    # Release first; task should complete
    proxy._release_slot("model-a")
    await aio.wait_for(task, timeout=1.0)
    assert proxy._inflight["model-a"] == 1  # now the 2nd is in-flight
    assert proxy._queued["model-a"] == 0
    proxy._release_slot("model-a")


@pytest.mark.asyncio
async def test_queue_full_raises_when_exceeding_depth():
    """Nth+1 concurrent request (1 in-flight + N queued) → MlxQueueFullError."""
    import asyncio as aio
    from fleet_manager.server.mlx_proxy import MlxProxy, MlxQueueFullError
    proxy = MlxProxy("http://test", max_queue_depth=2, retry_after_seconds=5)

    # 1 in-flight
    await proxy._acquire_slot("model-a")
    # 2 queued — both should block, not raise
    t1 = aio.create_task(proxy._acquire_slot("model-a"))
    t2 = aio.create_task(proxy._acquire_slot("model-a"))
    await aio.sleep(0.05)
    assert proxy._queued["model-a"] == 2
    assert not t1.done()
    assert not t2.done()

    # 3rd queued attempt → overflow → raise
    with pytest.raises(MlxQueueFullError) as exc_info:
        await proxy._acquire_slot("model-a")
    assert exc_info.value.queued == 2
    assert exc_info.value.in_flight == 1
    assert exc_info.value.retry_after == 5
    assert exc_info.value.model_key == "model-a"
    # rejected counter incremented
    assert proxy._rejected["model-a"] == 1

    # Cleanup: release the first, let the queued ones finish
    proxy._release_slot("model-a")
    await aio.wait_for(t1, timeout=1.0)
    proxy._release_slot("model-a")
    await aio.wait_for(t2, timeout=1.0)
    proxy._release_slot("model-a")


@pytest.mark.asyncio
async def test_per_model_semaphores_are_independent():
    """Two different MLX models can each have one in-flight simultaneously."""
    from fleet_manager.server.mlx_proxy import MlxProxy
    proxy = MlxProxy("http://test", max_queue_depth=1)
    await proxy._acquire_slot("model-a")
    await proxy._acquire_slot("model-b")
    assert proxy._inflight["model-a"] == 1
    assert proxy._inflight["model-b"] == 1
    proxy._release_slot("model-a")
    proxy._release_slot("model-b")


@pytest.mark.asyncio
async def test_queue_full_does_not_affect_inflight_counter():
    """A rejected request must not leak into the queued or inflight counts."""
    from fleet_manager.server.mlx_proxy import MlxProxy, MlxQueueFullError
    import asyncio as aio
    proxy = MlxProxy("http://test", max_queue_depth=1)
    await proxy._acquire_slot("model-a")
    t1 = aio.create_task(proxy._acquire_slot("model-a"))
    await aio.sleep(0.05)
    assert proxy._queued["model-a"] == 1

    with pytest.raises(MlxQueueFullError):
        await proxy._acquire_slot("model-a")
    # Queued count still 1 (the legitimate waiter), not 2 or 0
    assert proxy._queued["model-a"] == 1
    assert proxy._inflight["model-a"] == 1

    # Cleanup
    proxy._release_slot("model-a")
    await aio.wait_for(t1, timeout=1.0)
    proxy._release_slot("model-a")


def test_get_queue_info_surfaces_rejected_count():
    """The /fleet/queue endpoint must expose admission rejections."""
    from fleet_manager.server.mlx_proxy import MlxProxy
    proxy = MlxProxy("http://test", max_queue_depth=1)
    # Fake some state
    proxy._rejected["model-a"] = 5
    proxy._completed["model-a"] = 10
    info = proxy.get_queue_info()
    entry = info["mlx-local:mlx:model-a"]
    assert entry["rejected"] == 5
    assert entry["completed"] == 10
    assert entry["backend"] == "mlx"
    assert entry["max_queue_depth"] == 1


# ---------------------------------------------------------------------------
# Cache-hit-rate observability — Phase 2 of mlx-prompt-cache-optimization
# ---------------------------------------------------------------------------


def test_pop_token_counts_returns_three_tuple():
    """Tuple is (prompt, completion, cached) — cached may be None for older mlx."""
    from fleet_manager.server.mlx_proxy import MlxProxy, _mlx_request_tokens
    proxy = MlxProxy("http://test")
    _mlx_request_tokens["req-1"] = (1234, 56, 1100)
    result = proxy.pop_token_counts("req-1")
    assert result == (1234, 56, 1100)
    # Drained from the global dict
    assert "req-1" not in _mlx_request_tokens


def test_pop_token_counts_missing_request_returns_all_none():
    """Missing request_id returns the canonical (None, None, None) tuple."""
    from fleet_manager.server.mlx_proxy import MlxProxy
    proxy = MlxProxy("http://test")
    assert proxy.pop_token_counts("never-existed") == (None, None, None)


def test_cache_hit_rate_none_when_no_observations():
    """Fresh proxy has no observations → returns None, not 0%."""
    from fleet_manager.server.mlx_proxy import MlxProxy
    proxy = MlxProxy("http://test")
    assert proxy.get_cache_hit_rate() is None


def test_cache_hit_rate_computed_from_pop():
    """pop_token_counts() folds cached observations into rolling stats."""
    from fleet_manager.server.mlx_proxy import MlxProxy, _mlx_request_tokens
    proxy = MlxProxy("http://test")
    # Three requests: 80% hit, 90% hit, 50% hit
    for rid, (p, c) in [("a", (10000, 8000)), ("b", (10000, 9000)), ("c", (10000, 5000))]:
        _mlx_request_tokens[rid] = (p, 50, c)
        proxy.pop_token_counts(rid)
    # Weighted: (8000 + 9000 + 5000) / (10000*3) = 22000/30000 ≈ 73.3%
    rate = proxy.get_cache_hit_rate()
    assert rate is not None
    assert abs(rate - 22000/30000) < 0.001


def test_cache_hit_rate_excludes_observations_with_no_cached_tokens():
    """If mlx didn't report cached_tokens, observation is skipped (not 0%)."""
    from fleet_manager.server.mlx_proxy import MlxProxy, _mlx_request_tokens
    proxy = MlxProxy("http://test")
    _mlx_request_tokens["a"] = (10000, 50, None)  # mlx didn't report
    proxy.pop_token_counts("a")
    # Should NOT register as 0% hit — should remain "no data"
    assert proxy.get_cache_hit_rate() is None


def test_cache_hit_rate_rolling_window_caps_at_50():
    """Window keeps the most recent 50 observations to reflect current state."""
    from fleet_manager.server.mlx_proxy import MlxProxy, _mlx_request_tokens
    proxy = MlxProxy("http://test")
    # First 50 observations: 0% hit
    for i in range(50):
        _mlx_request_tokens[f"old-{i}"] = (1000, 50, 0)
        proxy.pop_token_counts(f"old-{i}")
    assert proxy.get_cache_hit_rate() == 0.0
    # 51st observation: 100% hit — should evict the oldest 0%
    _mlx_request_tokens["new-1"] = (1000, 50, 1000)
    proxy.pop_token_counts("new-1")
    # Window now has 49 zeros + 1 perfect → rate = 1000 / (49*1000 + 1000) = 0.02
    rate = proxy.get_cache_hit_rate()
    assert rate is not None
    assert abs(rate - 0.02) < 0.001


def test_cache_hit_rate_surfaced_in_get_queue_info():
    """The /fleet/queue endpoint must expose cache_hit_rate per MLX entry."""
    from fleet_manager.server.mlx_proxy import MlxProxy, _mlx_request_tokens
    proxy = MlxProxy("http://test", max_queue_depth=3)
    proxy._completed["model-a"] = 5
    # Seed cache observations
    _mlx_request_tokens["x"] = (1000, 10, 800)
    proxy.pop_token_counts("x")
    info = proxy.get_queue_info()
    entry = info["mlx-local:mlx:model-a"]
    assert "cache_hit_rate" in entry
    assert entry["cache_hit_rate"] == 0.8


def test_cache_hit_rate_is_none_in_queue_info_before_observations():
    """Before any cache-fold, the field is present but None (not 0)."""
    from fleet_manager.server.mlx_proxy import MlxProxy
    proxy = MlxProxy("http://test")
    proxy._completed["model-a"] = 1
    info = proxy.get_queue_info()
    assert info["mlx-local:mlx:model-a"]["cache_hit_rate"] is None


def test_to_openai_body_sets_include_usage_when_streaming():
    """Streaming requests must ask mlx_lm.server for the final usage chunk
    so we can measure cache_hit_rate on the path Claude Code actually uses."""
    req = _make_request(
        raw_body={
            "model": "mlx:foo", "messages": [], "stream": True,
        },
    )
    body = MlxProxy._to_openai_body(req)
    assert body["stream"] is True
    assert body.get("stream_options") == {"include_usage": True}


def test_to_openai_body_no_stream_options_for_non_streaming():
    """Non-streaming responses already include usage at top level — don't
    add the stream_options field for those (server-side no-op but cleaner)."""
    req = _make_request(
        raw_body={
            "model": "mlx:foo", "messages": [], "stream": False,
        },
    )
    body = MlxProxy._to_openai_body(req)
    assert body["stream"] is False
    assert "stream_options" not in body


# ---------------------------------------------------------------------------
# Phase 3 enhancements: deterministic tool sort + warm/cold split
# ---------------------------------------------------------------------------


def test_to_openai_body_sorts_tools_deterministically():
    """Tools array must be byte-stable across requests so mlx prefix cache
    hits.  Even if Claude Code shuffles tool order between turns (currently
    doesn't, but defensive), our translator outputs alphabetical."""
    req = _make_request(
        raw_body={
            "model": "mlx:foo", "messages": [], "stream": True,
            "tools": [
                {"name": "Zebra", "description": "z", "parameters": {}},
                {"name": "Apple", "description": "a", "parameters": {}},
                {"name": "Mango", "description": "m", "parameters": {}},
            ],
        },
    )
    body = MlxProxy._to_openai_body(req)
    names = [t["function"]["name"] for t in body["tools"]]
    assert names == ["Apple", "Mango", "Zebra"]


def test_to_openai_body_sort_preserves_when_tools_already_wrapped():
    """Pre-wrapped (OpenAI shape) tools also get sorted."""
    req = _make_request(
        raw_body={
            "model": "mlx:foo", "messages": [],
            "tools": [
                {"type": "function", "function": {"name": "B", "parameters": {}}},
                {"type": "function", "function": {"name": "A", "parameters": {}}},
            ],
        },
    )
    body = MlxProxy._to_openai_body(req)
    names = [t["function"]["name"] for t in body["tools"]]
    assert names == ["A", "B"]


def test_get_cache_stats_no_observations():
    """Fresh proxy returns None hit rates + 0 samples."""
    from fleet_manager.server.mlx_proxy import MlxProxy
    proxy = MlxProxy("http://test")
    stats = proxy.get_cache_stats()
    assert stats["warm_hit_rate"] is None
    assert stats["cold_request_pct"] is None
    assert stats["sample_count"] == 0


def test_get_cache_stats_distinguishes_warm_from_cold():
    """Warm hit rate and cold-request fraction are reported separately —
    the simple avg can be misleading when sessions mix cold + warm turns."""
    from fleet_manager.server.mlx_proxy import MlxProxy, _mlx_request_tokens
    proxy = MlxProxy("http://test")
    # 3 cold (<10% hit), 2 warm (>80% hit)
    for rid, (p, c) in [
        ("cold1", (1000, 5)),    # 0.5% — cold
        ("cold2", (1000, 0)),    # 0%   — cold
        ("cold3", (1000, 10)),   # 1%   — cold
        ("warm1", (1000, 950)),  # 95%  — warm
        ("warm2", (1000, 990)),  # 99%  — warm
    ]:
        _mlx_request_tokens[rid] = (p, 50, c)
        proxy.pop_token_counts(rid)
    stats = proxy.get_cache_stats()
    assert stats["sample_count"] == 5
    # cold = 3 of 5 = 60%
    assert abs(stats["cold_request_pct"] - 0.6) < 0.001
    # warm rate = (950+990)/(1000+1000) = 0.97
    assert abs(stats["warm_hit_rate"] - 0.97) < 0.001


def test_get_queue_info_exposes_warm_cold_split():
    """Dashboard can show 'CACHE 100% on warm, 30% of requests are cold'."""
    from fleet_manager.server.mlx_proxy import MlxProxy, _mlx_request_tokens
    proxy = MlxProxy("http://test")
    proxy._completed["m"] = 1
    _mlx_request_tokens["x"] = (1000, 50, 1000)  # 100% hit
    proxy.pop_token_counts("x")
    info = proxy.get_queue_info()["mlx-local:mlx:m"]
    assert "warm_hit_rate" in info
    assert "cold_request_pct" in info
    assert "cache_sample_count" in info
    assert info["warm_hit_rate"] == 1.0
    assert info["cold_request_pct"] == 0.0


# ---------------------------------------------------------------------------
# _collect_openai_stream — stream accumulator used by completions_non_streaming
#
# Regression guard: a non-streaming client request to a large MLX model
# (480B) used to fire httpx.ReadTimeout because the direct POST path held
# the connection silent during prefill.  We now forward stream=True to
# mlx_lm.server internally and accumulate chunks — each token keeps the
# read timer alive.  These tests cover the accumulator's fidelity for
# text-only, tool-calls, finish_reason, and trailing usage chunks.
# ---------------------------------------------------------------------------


import pytest as _pytest

from fleet_manager.server.mlx_proxy import _collect_openai_stream


async def _as_aiter(lines):
    for line in lines:
        yield line


@_pytest.mark.asyncio
async def test_collect_text_only_concatenates_content():
    lines = [
        'data: {"id":"c1","object":"chat.completion.chunk","created":100,"model":"mlx-480b",'
        '"choices":[{"index":0,"delta":{"role":"assistant","content":"Hello"},"finish_reason":null}]}',
        'data: {"id":"c1","choices":[{"index":0,"delta":{"content":", "},"finish_reason":null}]}',
        'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"world!"},"finish_reason":"stop"}]}',
        "data: [DONE]",
    ]
    out = await _collect_openai_stream(_as_aiter(lines))
    assert out["id"] == "c1"
    assert out["model"] == "mlx-480b"
    assert out["object"] == "chat.completion"
    assert len(out["choices"]) == 1
    msg = out["choices"][0]["message"]
    assert msg["role"] == "assistant"
    assert msg["content"] == "Hello, world!"
    assert out["choices"][0]["finish_reason"] == "stop"


@_pytest.mark.asyncio
async def test_collect_captures_trailing_usage_chunk():
    """mlx_lm emits a final chunk with empty choices + populated usage when
    include_usage=True.  Must land on the response."""
    lines = [
        'data: {"id":"c2","choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":"stop"}]}',
        'data: {"id":"c2","choices":[],"usage":{"prompt_tokens":10,"completion_tokens":1,'
        '"prompt_tokens_details":{"cached_tokens":8}}}',
        "data: [DONE]",
    ]
    out = await _collect_openai_stream(_as_aiter(lines))
    assert out["usage"]["prompt_tokens"] == 10
    assert out["usage"]["completion_tokens"] == 1
    assert out["usage"]["prompt_tokens_details"]["cached_tokens"] == 8
    assert out["choices"][0]["message"]["content"] == "hi"


@_pytest.mark.asyncio
async def test_collect_accumulates_tool_call_arguments_across_chunks():
    """Tool-call arguments arrive as partial-JSON string deltas; the
    accumulator must concatenate them in order per (choice, tool_index)."""
    lines = [
        'data: {"id":"c3","choices":[{"index":0,"delta":{"role":"assistant"}}]}',
        'data: {"id":"c3","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_1",'
        '"function":{"name":"list_dir","arguments":"{\\"pa"}}]}}]}',
        'data: {"id":"c3","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,'
        '"function":{"arguments":"th\\":\\"/tmp\\"}"}}]}}]}',
        'data: {"id":"c3","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    ]
    out = await _collect_openai_stream(_as_aiter(lines))
    choice = out["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    tool_calls = choice["message"]["tool_calls"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["id"] == "call_1"
    assert tool_calls[0]["type"] == "function"
    assert tool_calls[0]["function"]["name"] == "list_dir"
    # Arguments reassembled to valid JSON string
    assert tool_calls[0]["function"]["arguments"] == '{"path":"/tmp"}'


@_pytest.mark.asyncio
async def test_collect_handles_malformed_and_empty_lines():
    """Malformed lines must not poison the stream — keep consuming."""
    lines = [
        "",
        "data: not-json-at-all",
        'data: {"id":"c4","choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":"stop"}]}',
        "data: [DONE]",
    ]
    out = await _collect_openai_stream(_as_aiter(lines))
    assert out["choices"][0]["message"]["content"] == "ok"


@_pytest.mark.asyncio
async def test_collect_output_matches_build_anthropic_non_streaming_shape():
    """The accumulator's dict must be consumable by
    ``build_anthropic_non_streaming_response`` — that's the contract
    ``completions_non_streaming`` preserves for the non-streaming route."""
    from fleet_manager.server.mlx_proxy import (
        build_anthropic_non_streaming_response,
    )

    lines = [
        'data: {"id":"c5","model":"mlx-480b","created":500,"choices":['
        '{"index":0,"delta":{"role":"assistant","content":"Sure."},"finish_reason":"stop"}]}',
        'data: {"id":"c5","choices":[],"usage":{"prompt_tokens":3,"completion_tokens":1}}',
        "data: [DONE]",
    ]
    collected = await _collect_openai_stream(_as_aiter(lines))
    # Must not blow up — that's the contract we care about
    anthropic = build_anthropic_non_streaming_response(
        collected, "claude-sonnet-4-5",
    )
    assert anthropic["type"] == "message"
    assert anthropic["stop_reason"] == "end_turn"
    # Content was accumulated into a text block
    assert any(
        b.get("type") == "text" and b.get("text") == "Sure."
        for b in anthropic["content"]
    )
    assert anthropic["usage"]["input_tokens"] == 3
    assert anthropic["usage"]["output_tokens"] == 1


# ---------------------------------------------------------------------------
# MlxWallClockTimeoutError — bound on wedged-request syndrome
# ---------------------------------------------------------------------------


def test_wall_clock_timeout_exception_has_model_and_elapsed():
    from fleet_manager.server.mlx_proxy import MlxWallClockTimeoutError
    exc = MlxWallClockTimeoutError("qwen-next", 310.5, 300.0)
    assert exc.model_key == "qwen-next"
    assert exc.elapsed_s == 310.5
    assert exc.limit_s == 300.0
    # Message points user at /compact — the whole point of the 413 path
    assert "/compact" in str(exc)


def test_mlx_proxy_accepts_wall_clock_timeout_config():
    from fleet_manager.server.mlx_proxy import MlxProxy
    proxy = MlxProxy("http://test", wall_clock_timeout_s=60.0)
    assert proxy.wall_clock_timeout_s == 60.0


def test_mlx_proxy_defaults_wall_clock_timeout_to_300s():
    from fleet_manager.server.mlx_proxy import MlxProxy
    proxy = MlxProxy("http://test")
    assert proxy.wall_clock_timeout_s == 300.0
