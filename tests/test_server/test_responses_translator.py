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
    # 'developer' tool item must NOT leak in as a message. (A `system` message
    # carrying the turn-completion guidance is expected whenever tools exist.)
    roles = [m["role"] for m in out["messages"]]
    assert "developer" not in roles
    assert roles == ["system", "user"]
    assert "wait" not in out["messages"][0]["content"]


def test_custom_tool_is_bridged_not_dropped():
    """Codex's primary tool (`exec`) is a `custom` tool. We used to drop it,
    leaving the model with nothing that could run a command — it then
    rationalised the failure instead of reporting it. Now it's exposed as a
    function taking one freeform string."""
    body = {"model": "m", "input": [
        {"type": "additional_tools", "tools": [
            {"type": "custom", "name": "exec", "description": "Run JS",
             "format": {"type": "grammar", "syntax": "lark", "definition": "x"}},
            {"type": "function", "name": "wait", "parameters": {}},
        ]},
        {"role": "user", "content": "go"},
    ]}
    out = responses_to_openai_body(body)
    assert [t["function"]["name"] for t in out["tools"]] == ["exec", "wait"]
    assert out["_custom_tool_names"] == ["exec"]
    params = out["tools"][0]["function"]["parameters"]
    assert list(params["properties"]) == ["input"]
    assert params["properties"]["input"]["type"] == "string"
    # the grammar hint must tell the model to send raw text, not JSON
    assert "no JSON" in out["tools"][0]["function"]["description"]


def test_custom_tool_call_returns_as_custom_tool_call_item():
    """Codex requires `custom_tool_call` with freeform `input` — NOT a
    `function_call` with JSON arguments — and a distinct event family."""
    line = json.dumps({"message": {"tool_calls": [
        {"function": {"name": "exec", "arguments": {"input": "await tools.x()"}}}]}})
    state = ResponsesSSEState(model="m", custom_names={"exec"})
    types, items = [], []
    for raw in ollama_chunk_to_responses_events(line, state):
        types.append(raw.split("\n")[0].replace("event: ", ""))
        items.append(json.loads(raw.split("data: ", 1)[1].strip()))
    assert "response.custom_tool_call_input.delta" in types
    assert "response.custom_tool_call_input.done" in types
    assert "response.function_call_arguments.delta" not in types
    done = [i for i in items if i["type"] == "response.output_item.done"][0]["item"]
    assert done["type"] == "custom_tool_call"
    assert done["input"] == "await tools.x()"   # unwrapped from {"input": ...}
    assert done["call_id"] and done["id"].startswith("ctc_")


def test_non_custom_tools_still_emit_function_call():
    """Regression guard: the bridge must not hijack ordinary function tools."""
    line = json.dumps({"message": {"tool_calls": [
        {"function": {"name": "exec_command", "arguments": {"cmd": "ls"}}}]}})
    state = ResponsesSSEState(model="m", custom_names={"exec"})
    types = [r.split("\n")[0].replace("event: ", "")
             for r in ollama_chunk_to_responses_events(line, state)]
    assert "response.function_call_arguments.delta" in types
    assert "response.custom_tool_call_input.delta" not in types


def test_custom_tool_history_round_trips():
    """Codex echoes our custom_tool_call and its output back on the next turn."""
    msgs = responses_input_to_messages([
        {"type": "custom_tool_call", "call_id": "c1", "name": "exec",
         "input": "await tools.x()"},
        {"type": "custom_tool_call_output", "call_id": "c1", "output": "done"},
    ])
    assert msgs[0]["tool_calls"][0]["function"]["name"] == "exec"
    assert msgs[0]["tool_calls"][0]["function"]["arguments"] == {"input": "await tools.x()"}
    assert msgs[1] == {"role": "tool", "tool_call_id": "c1", "content": "done"}


def test_genuinely_untranslatable_tools_still_warn(caplog):
    """Hosted tools (web_search etc.) have no local equivalent — still dropped,
    still loudly."""
    import logging
    body = {"model": "m", "input": [
        {"type": "additional_tools", "tools": [
            {"type": "web_search"},
            {"type": "function", "name": "wait", "parameters": {}},
        ]},
        {"role": "user", "content": "go"},
    ]}
    with caplog.at_level(logging.WARNING):
        out = responses_to_openai_body(body)
    assert [t["function"]["name"] for t in out["tools"]] == ["wait"]
    assert "web_search" in caplog.text


# ---------------------------------------------------------------------------
# Code-mode: JSON-instead-of-JavaScript repair
# see docs/plans/codex-code-mode-escalation.md
# ---------------------------------------------------------------------------


def test_json_exec_payload_is_repaired_to_a_javascript_call():
    """Codex's code-mode `exec` takes raw JS; its grammar (`/[\\s\\S]+/`)
    constrains nothing, so a JSON object passes through and then fails as a JS
    program — silently losing any escalation the model requested."""
    from fleet_manager.server.responses_translator import _unwrap_custom_input

    out = _unwrap_custom_input(
        '{"input": "{\\"cmd\\": \\"git pull\\", '
        '\\"sandbox_permissions\\": \\"require_escalated\\"}"}'
    )
    assert out.startswith("await tools.exec_command(")
    assert "require_escalated" in out
    assert "git pull" in out


def test_single_key_exec_payload_repairs_instead_of_degrading():
    """Regression: `{"cmd": "…"}` is a single key, so the len==1 fallback used
    to unwrap it to the bare string `git pull` — not JavaScript either, but
    plausible enough to fail unnoticed."""
    from fleet_manager.server.responses_translator import _unwrap_custom_input

    assert _unwrap_custom_input('{"cmd": "git pull"}') == (
        'await tools.exec_command({"cmd": "git pull"})'
    )


def test_valid_javascript_passes_through_untouched():
    """The critical regression — repair must never rewrite working source."""
    from fleet_manager.server.responses_translator import _unwrap_custom_input

    for src in (
        'await tools.exec_command({cmd: "git pull"})',
        "const r = await tools.exec_command({cmd: 'ls'}); return r;",
    ):
        assert _unwrap_custom_input(src) == src


def test_bridge_unwrap_behaviour_is_unchanged():
    from fleet_manager.server.responses_translator import _unwrap_custom_input

    assert _unwrap_custom_input('{"input": "await tools.x()"}') == "await tools.x()"
    assert _unwrap_custom_input("bare text") == "bare text"


def test_json_object_without_cmd_is_left_alone():
    from fleet_manager.server.responses_translator import _unwrap_custom_input

    src = '{"foo": "bar", "baz": 1}'
    assert _unwrap_custom_input(src) == src


def test_unknown_sibling_keys_are_preserved():
    """Which keys `exec_command` accepts is Codex's business, not ours."""
    import json

    from fleet_manager.server.responses_translator import _unwrap_custom_input

    out = _unwrap_custom_input(
        '{"cmd": "git pull", "justification": "needs network", '
        '"prefix_rule": ["git", "pull"], "timeout_ms": 5000}'
    )
    inner = json.loads(out[len("await tools.exec_command(") : -1])
    assert inner == {
        "cmd": "git pull",
        "justification": "needs network",
        "prefix_rule": ["git", "pull"],
        "timeout_ms": 5000,
    }


def test_repair_is_audible_once_per_tool(caplog):
    import logging

    from fleet_manager.server import responses_translator as rt

    rt._LOGGED_JSON_CALL_REPAIRS.clear()
    with caplog.at_level(logging.WARNING):
        rt._unwrap_custom_input('{"cmd": "ls"}', "exec")
        rt._unwrap_custom_input('{"cmd": "pwd"}', "exec")
    assert caplog.text.count("instead of 'exec' source") == 1


def test_grammar_tool_description_shows_the_call_shape():
    """Telling the model 'no JSON' without showing the call shape is what
    produced the JSON payloads in the first place."""
    from fleet_manager.server.responses_translator import _tools_to_openai

    out = _tools_to_openai([{
        "type": "custom", "name": "exec", "description": "Run JS",
        "format": {"type": "grammar", "syntax": "lark", "definition": "start: x"},
    }])
    fn = out[0]["function"]
    assert "await tools.exec_command" in fn["description"]
    assert "require_escalated" in fn["description"]
    assert "not JSON" in fn["parameters"]["properties"]["input"]["description"]


def test_turn_completion_guidance_is_always_present():
    """Codex tells the model to send a preamble before tool calls; local models
    often send the preamble and stop, which reads as 'turn complete'. The
    counter-instruction goes in unconditionally — Lite requests carry no
    `instructions` at all, so anything keyed on Codex's wording would skip the
    exact path where this was observed."""
    from fleet_manager.server.responses_translator import TURN_COMPLETION_GUIDANCE

    tool = {"type": "function", "name": "wait", "parameters": {}}

    # No instructions from the client (the Lite shape) — a system message is
    # created to carry the guidance.
    out = responses_to_openai_body(
        {"model": "m", "tools": [tool], "input": [{"role": "user", "content": "go"}]}
    )
    assert out["messages"][0]["role"] == "system"
    assert TURN_COMPLETION_GUIDANCE.strip() in out["messages"][0]["content"]

    # Client instructions are extended, not replaced, and no second system
    # message is introduced.
    out = responses_to_openai_body({
        "model": "m", "tools": [tool], "instructions": "You are Codex.",
        "input": [{"role": "user", "content": "go"}],
    })
    assert out["messages"][0]["content"].startswith("You are Codex.")
    assert TURN_COMPLETION_GUIDANCE.strip() in out["messages"][0]["content"]
    assert [m["role"] for m in out["messages"]] == ["system", "user"]


def test_no_tools_means_no_turn_guidance():
    """A plain chat turn has no tool call to withhold — don't add noise."""
    from fleet_manager.server.responses_translator import TURN_COMPLETION_GUIDANCE

    out = responses_to_openai_body(
        {"model": "m", "input": [{"role": "user", "content": "hi"}]}
    )
    joined = " ".join(m.get("content", "") for m in out["messages"])
    assert TURN_COMPLETION_GUIDANCE.strip() not in joined


def test_nested_code_mode_tool_call_is_redirected():
    """In code mode the only callable tool is `exec`; `exec_command` lives behind
    `await tools.exec_command(...)` inside the JavaScript. Models read the 14KB
    `exec` description and call that name directly — measured 4/4 on
    gpt-oss:120b. Passing it through stalls the loop: Codex gets a call for a
    tool it never offered and cannot execute it."""
    from fleet_manager.server.responses_translator import build_responses_object

    obj = build_responses_object(
        model="m", text="",
        tool_calls=[{"function": {"name": "exec_command",
                                  "arguments": {"cmd": "ls docs"}}}],
        custom_names={"exec"}, known_names={"exec", "wait"},
        input_tokens=1, output_tokens=1,
    )
    item = obj["output"][0]
    assert item["type"] == "custom_tool_call"
    assert item["name"] == "exec"          # routed to the host tool, not the nested one
    assert item["input"] == 'await tools.exec_command({"cmd": "ls docs"})'


def test_offered_tools_are_never_redirected():
    """The critical regression — a legitimately offered tool must pass through."""
    from fleet_manager.server.responses_translator import build_responses_object

    obj = build_responses_object(
        model="m", text="",
        tool_calls=[{"function": {"name": "wait", "arguments": {"ms": 5}}}],
        custom_names={"exec"}, known_names={"exec", "wait"},
        input_tokens=1, output_tokens=1,
    )
    assert obj["output"][0]["type"] == "function_call"
    assert obj["output"][0]["name"] == "wait"


def test_unknown_offered_set_never_redirects():
    """Empty known_names means 'we don't know what was offered' — not 'nothing
    was offered'. Never redirect on absence of information."""
    from fleet_manager.server.responses_translator import build_responses_object

    obj = build_responses_object(
        model="m", text="",
        tool_calls=[{"function": {"name": "whatever", "arguments": {}}}],
        custom_names={"exec"}, known_names=set(),
        input_tokens=1, output_tokens=1,
    )
    assert obj["output"][0]["type"] == "function_call"


def test_known_tool_names_are_exported_for_the_route():
    out = responses_to_openai_body({"model": "m", "input": [
        {"type": "additional_tools", "tools": [
            {"type": "custom", "name": "exec", "description": "js",
             "format": {"type": "grammar", "syntax": "lark", "definition": "x"}},
            {"type": "function", "name": "wait", "parameters": {}},
        ]},
        {"role": "user", "content": "go"},
    ]})
    assert out["_known_tool_names"] == ["exec", "wait"]
    assert out["_custom_tool_names"] == ["exec"]


def test_apply_patch_tool_call_becomes_an_exec_command_heredoc():
    """`apply_patch` is a BINARY Codex injects at the front of the sandbox PATH,
    not a tool — verified live. Models read "Use the apply_patch tool to edit
    files" and call it as a tool, which Codex rejects outright
    (`error=unsupported call: apply_patch`), so the session reads and executes
    fine but can never write."""
    from fleet_manager.server.responses_translator import build_responses_object

    patch = "*** Begin Patch\n*** Update File: a.py\n@@\n-x\n+y\n*** End Patch"
    for shape in ({"command": ["apply_patch", patch]}, {"input": patch}, {"patch": patch}):
        obj = build_responses_object(
            model="m", text="",
            tool_calls=[{"function": {"name": "apply_patch", "arguments": shape}}],
            custom_names=set(), known_names={"exec_command", "wait"},
            input_tokens=1, output_tokens=1,
        )
        item = obj["output"][0]
        assert item["type"] == "function_call"
        assert item["name"] == "exec_command"
        cmd = json.loads(item["arguments"])["cmd"]
        # Heredoc, so quotes/backslashes/$ in the patch body survive the shell.
        assert cmd.startswith("apply_patch <<'CODEX_PATCH_EOF'")
        assert patch in cmd
        assert cmd.endswith("CODEX_PATCH_EOF")


def test_apply_patch_is_left_alone_when_it_really_is_a_tool():
    """If a future Codex offers apply_patch as a real tool, don't hijack it."""
    from fleet_manager.server.responses_translator import build_responses_object

    obj = build_responses_object(
        model="m", text="",
        tool_calls=[{"function": {"name": "apply_patch",
                                  "arguments": {"input": "*** Begin Patch\n*** End Patch"}}}],
        custom_names=set(), known_names={"apply_patch", "exec_command"},
        input_tokens=1, output_tokens=1,
    )
    assert obj["output"][0]["name"] == "apply_patch"


def test_apply_patch_without_a_recognisable_patch_is_not_rewritten():
    """Don't guess. No patch envelope means we don't know what this is."""
    from fleet_manager.server.responses_translator import build_responses_object

    obj = build_responses_object(
        model="m", text="",
        tool_calls=[{"function": {"name": "apply_patch", "arguments": {"foo": "bar"}}}],
        custom_names=set(), known_names={"exec_command"},
        input_tokens=1, output_tokens=1,
    )
    assert obj["output"][0]["name"] == "apply_patch"


# ---------------------------------------------------------------------------
# Images: don't drop them, and route them somewhere that can see
# ---------------------------------------------------------------------------


def test_input_image_reaches_the_model_as_ollama_images():
    """Codex's `view_image` sends the picture back on the next turn. Dropping it
    silently is why qwen3-coder described a 32x32 PNG as "1x1 pixel, #FF0000" —
    it never received an image and wasn't told one went missing."""
    from fleet_manager.server.responses_translator import responses_input_to_messages

    msgs = responses_input_to_messages([
        {"role": "user", "content": [
            {"type": "input_text", "text": "what colour?"},
            {"type": "input_image", "image_url": "data:image/png;base64,AAAB"},
        ]},
    ])
    assert msgs[0]["content"] == "what colour?"
    assert msgs[0]["images"] == ["AAAB"]        # Ollama's sibling list, not inline


def test_remote_image_urls_are_dropped_loudly(caplog):
    """Ollama can't fetch them — the Anthropic path skips url-type images too."""
    import logging

    from fleet_manager.server.responses_translator import responses_input_to_messages

    with caplog.at_level(logging.WARNING):
        msgs = responses_input_to_messages([
            {"role": "user", "content": [
                {"type": "input_image", "image_url": "https://example.com/a.png"},
            ]},
        ])
    assert "images" not in msgs[0]
    assert "remote image URL" in caplog.text


def test_input_has_images_drives_vision_routing():
    """The signal the route feeds to resolve_model(has_images=...), which already
    knows how to prefer a vision-capable model."""
    from fleet_manager.server.responses_translator import input_has_images

    assert input_has_images([
        {"role": "user", "content": [
            {"type": "input_image", "image_url": "data:image/png;base64,AAAB"}]},
    ])
    assert not input_has_images([
        {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
    ])
    assert not input_has_images("plain string input")


def test_text_only_messages_carry_no_images_key():
    from fleet_manager.server.responses_translator import responses_input_to_messages

    msgs = responses_input_to_messages([{"role": "user", "content": "hi"}])
    assert "images" not in msgs[0]
