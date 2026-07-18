"""Tests for the OpenAI Responses API translator (Codex support).

Codex speaks only /v1/responses since Feb 2026. These pin both directions:
Responses request → OpenAI-shaped body (which Ollama accepts natively), and
Ollama NDJSON → the Responses object / SSE event sequence.

Most were written against the *documented* spec; the `additional_tools` cases at
the bottom come from a **real captured codex-cli 0.145 request** (2026-07-18),
which revealed Codex ignores the documented top-level `tools` field entirely.
That capture is why "spec-complete" and "client-verified" are different things.
"""

from __future__ import annotations

import json

from fleet_manager.server.responses_translator import (
    ResponsesSSEState,
    accumulate_responses_object,
    ollama_chunk_to_responses_events,
    responses_input_to_messages,
    responses_to_openai_body,
)

# ---------------------------------------------------------------------------
# Request:  Responses → OpenAI-shaped body
# ---------------------------------------------------------------------------


def test_string_input_becomes_user_message():
    assert responses_input_to_messages("hello") == [
        {"role": "user", "content": "hello"}
    ]


def test_instructions_become_leading_system_message():
    msgs = responses_input_to_messages("hi", instructions="Be terse.")
    assert msgs[0] == {"role": "system", "content": "Be terse."}
    assert msgs[1]["role"] == "user"


def test_typed_content_parts_are_flattened():
    msgs = responses_input_to_messages([
        {"role": "user", "content": [
            {"type": "input_text", "text": "list "},
            {"type": "input_text", "text": "files"},
        ]},
    ])
    assert msgs == [{"role": "user", "content": "list files"}]


def test_unknown_content_part_types_are_skipped_not_fatal():
    """Forward-compat: the API keeps gaining part types; don't crash on them."""
    msgs = responses_input_to_messages([
        {"role": "user", "content": [
            {"type": "input_text", "text": "keep"},
            {"type": "some_future_part", "blob": "???"},
        ]},
    ])
    assert msgs == [{"role": "user", "content": "keep"}]


def test_tool_call_round_trip_threads_call_id():
    """A prior tool call + its result must come back as an assistant tool_calls
    message and a `tool` message sharing the same id — that threading is what
    lets the model connect a result to its request."""
    msgs = responses_input_to_messages([
        {"role": "user", "content": "run ls"},
        {"type": "function_call", "call_id": "call_1", "name": "bash",
         "arguments": '{"cmd":"ls"}'},
        {"type": "function_call_output", "call_id": "call_1", "output": "a.txt"},
    ])
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["tool_calls"][0]["id"] == "call_1"
    assert msgs[1]["tool_calls"][0]["function"]["name"] == "bash"
    assert msgs[1]["tool_calls"][0]["function"]["arguments"] == {"cmd": "ls"}
    assert msgs[2] == {"role": "tool", "tool_call_id": "call_1", "content": "a.txt"}


def test_tool_call_arguments_are_objects_not_json_strings():
    """Ollama's chat templates want `arguments` as an OBJECT. The Responses wire
    format sends a JSON *string*; passing it through 400s Ollama with "Value
    looks like object, but can't find closing '}' symbol" and kills the second
    turn of every agentic session."""
    from_str = responses_input_to_messages([
        {"type": "function_call", "call_id": "c1", "name": "f",
         "arguments": '{"a": 1}'},
    ])
    assert from_str[0]["tool_calls"][0]["function"]["arguments"] == {"a": 1}
    from_dict = responses_input_to_messages([
        {"type": "function_call", "call_id": "c1", "name": "f", "arguments": {"a": 1}},
    ])
    assert from_dict[0]["tool_calls"][0]["function"]["arguments"] == {"a": 1}


def test_reasoning_items_are_dropped():
    msgs = responses_input_to_messages([
        {"type": "reasoning", "summary": []},
        {"role": "user", "content": "hi"},
    ])
    assert msgs == [{"role": "user", "content": "hi"}]


def test_tools_are_nested_under_function_for_openai():
    """Responses tools are flat; OpenAI chat tools nest under `function`."""
    body = responses_to_openai_body({
        "model": "m", "input": "hi",
        "tools": [{"type": "function", "name": "bash",
                   "description": "run", "parameters": {"type": "object"}}],
    })
    assert body["tools"] == [{
        "type": "function",
        "function": {"name": "bash", "description": "run",
                     "parameters": {"type": "object"}},
    }]


def test_hosted_tools_are_dropped():
    """web_search/file_search have no local equivalent — drop, don't confuse."""
    body = responses_to_openai_body({
        "model": "m", "input": "hi",
        "tools": [{"type": "web_search"},
                  {"type": "function", "name": "bash", "parameters": {}}],
    })
    assert [t["function"]["name"] for t in body["tools"]] == ["bash"]


def test_max_output_tokens_maps_to_max_tokens():
    body = responses_to_openai_body({"model": "m", "input": "hi",
                                     "max_output_tokens": 256})
    assert body["max_tokens"] == 256


# ---------------------------------------------------------------------------
# Response:  Ollama → Responses object (non-streaming)
# ---------------------------------------------------------------------------


def _ollama(**kw) -> str:
    return json.dumps(kw)


def test_accumulate_builds_message_item_and_usage():
    resp = accumulate_responses_object([
        _ollama(message={"content": "Hel"}),
        _ollama(message={"content": "lo"}),
        _ollama(done=True, done_reason="stop", prompt_eval_count=7, eval_count=3),
    ], model="qwen3-coder:30b")
    assert resp["object"] == "response"
    assert resp["status"] == "completed"
    assert resp["output"][0]["type"] == "message"
    assert resp["output"][0]["content"][0]["text"] == "Hello"
    assert resp["usage"] == {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}


def test_tool_calls_are_top_level_output_items_with_string_arguments():
    resp = accumulate_responses_object([
        _ollama(message={"tool_calls": [
            {"function": {"name": "bash", "arguments": {"cmd": "ls"}}}]}),
        _ollama(done=True, done_reason="stop"),
    ], model="m")
    fc = [i for i in resp["output"] if i["type"] == "function_call"]
    assert len(fc) == 1
    assert fc[0]["name"] == "bash"
    # The Responses API carries arguments as a JSON *string*, not an object.
    assert isinstance(fc[0]["arguments"], str)
    assert json.loads(fc[0]["arguments"]) == {"cmd": "ls"}
    assert fc[0]["call_id"]


def test_length_stop_maps_to_incomplete_status():
    resp = accumulate_responses_object(
        [_ollama(done=True, done_reason="length")], model="m")
    assert resp["status"] == "incomplete"


def test_non_json_tool_arguments_are_wrapped_not_dropped():
    resp = accumulate_responses_object([
        _ollama(message={"tool_calls": [
            {"function": {"name": "bash", "arguments": "not json at all"}}]}),
        _ollama(done=True),
    ], model="m")
    fc = [i for i in resp["output"] if i["type"] == "function_call"][0]
    assert json.loads(fc["arguments"]) == {"_raw": "not json at all"}


def test_malformed_ollama_lines_are_skipped():
    resp = accumulate_responses_object(
        ["{not json", "", _ollama(message={"content": "ok"}), _ollama(done=True)],
        model="m")
    assert resp["output"][0]["content"][0]["text"] == "ok"


# ---------------------------------------------------------------------------
# Response:  Ollama → Responses SSE events (streaming)
# ---------------------------------------------------------------------------


def _events(lines: list[str], model: str = "m") -> list[dict]:
    state = ResponsesSSEState(model=model)
    out: list[dict] = []
    for ln in lines:
        for raw in ollama_chunk_to_responses_events(ln, state):
            assert raw.startswith("event: ")
            payload = raw.split("data: ", 1)[1].strip()
            out.append(json.loads(payload))
    return out


def _types(evts: list[dict]) -> list[str]:
    return [e["type"] for e in evts]


def test_text_only_stream_emits_the_documented_sequence():
    evts = _events([
        _ollama(message={"content": "Hi"}),
        _ollama(done=True, done_reason="stop", prompt_eval_count=2, eval_count=1),
    ])
    assert _types(evts) == [
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.done",
        "response.output_item.done",
        "response.completed",
    ]


def test_tool_call_stream_closes_message_then_opens_function_call():
    evts = _events([
        _ollama(message={"content": "working"}),
        _ollama(message={"tool_calls": [
            {"function": {"name": "bash", "arguments": {"cmd": "ls"}}}]}),
        _ollama(done=True, done_reason="stop"),
    ])
    t = _types(evts)
    # message item must be closed before the function_call item opens
    assert t.index("response.output_item.done") < t.index(
        "response.function_call_arguments.delta")
    assert "response.function_call_arguments.done" in t
    assert t[-1] == "response.completed"


def test_sequence_numbers_are_monotonic():
    evts = _events([
        _ollama(message={"content": "a"}),
        _ollama(message={"content": "b"}),
        _ollama(done=True),
    ])
    seqs = [e["sequence_number"] for e in evts]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)  # strictly unique


def test_completed_event_carries_full_output_and_usage():
    evts = _events([
        _ollama(message={"content": "Hello"}),
        _ollama(done=True, done_reason="stop", prompt_eval_count=5, eval_count=2),
    ])
    final = evts[-1]
    assert final["type"] == "response.completed"
    resp = final["response"]
    assert resp["status"] == "completed"
    assert resp["output"][0]["content"][0]["text"] == "Hello"
    assert resp["usage"]["total_tokens"] == 7


def test_deltas_carry_item_and_content_index():
    evts = _events([_ollama(message={"content": "x"}), _ollama(done=True)])
    delta = next(e for e in evts if e["type"] == "response.output_text.delta")
    assert delta["delta"] == "x"
    assert delta["output_index"] == 0
    assert delta["content_index"] == 0
    assert delta["item_id"].startswith("msg_")


def test_tool_only_response_still_completes():
    """A turn that is pure tool-call (no text) must not emit an empty message
    item and must still terminate."""
    evts = _events([
        _ollama(message={"tool_calls": [
            {"function": {"name": "f", "arguments": {}}}]}),
        _ollama(done=True),
    ])
    t = _types(evts)
    assert t[-1] == "response.completed"
    output = evts[-1]["response"]["output"]
    assert [i["type"] for i in output] == ["function_call"]


# ---------------------------------------------------------------------------
# Codex's real tool delivery — `additional_tools` inside `input`
#
# Captured from a real codex-cli 0.145 run (2026-07-18): Codex does NOT use the
# documented top-level `tools` field. Dropping this item silently left the model
# with no tools, so it narrated work it never did.
# ---------------------------------------------------------------------------


def test_tools_are_extracted_from_additional_tools_input_item():
    from fleet_manager.server.responses_translator import collect_tools_from_input

    inp = [
        {"type": "additional_tools", "role": "developer", "tools": [
            {"type": "function", "name": "wait", "parameters": {}},
            {"type": "custom", "name": "exec", "format": {}},
        ]},
        {"role": "user", "content": "hi"},
    ]
    found = collect_tools_from_input(inp)
    assert [(t["type"], t["name"]) for t in found] == [
        ("function", "wait"), ("custom", "exec"),
    ]


def test_additional_tools_reach_the_model_and_are_not_a_message():
    """The item must become tools, not conversation content."""
    body = {
        "model": "m",
        "input": [
            {"type": "additional_tools", "role": "developer", "tools": [
                {"type": "function", "name": "wait", "parameters": {"type": "object"}},
            ]},
            {"role": "user", "content": "hi"},
        ],
    }
    out = responses_to_openai_body(body)
    assert [t["function"]["name"] for t in out["tools"]] == ["wait"]
    # 'developer' tool item must NOT leak in as a message
    assert [m["role"] for m in out["messages"]] == ["user"]


def test_untranslatable_codex_tool_types_are_dropped_loudly(caplog):
    """`custom`/`namespace` have no function-calling equivalent. Dropping them
    is correct, but must be visible — silence is what let the model fake
    success."""
    import logging

    body = {"model": "m", "input": [
        {"type": "additional_tools", "tools": [
            {"type": "custom", "name": "exec", "format": {}},
            {"type": "namespace", "name": "collaboration", "tools": []},
            {"type": "function", "name": "wait", "parameters": {}},
        ]},
        {"role": "user", "content": "go"},
    ]}
    with caplog.at_level(logging.WARNING):
        out = responses_to_openai_body(body)
    assert [t["function"]["name"] for t in out["tools"]] == ["wait"]
    assert "dropped 2 untranslatable tool" in caplog.text
    assert "custom:exec" in caplog.text
