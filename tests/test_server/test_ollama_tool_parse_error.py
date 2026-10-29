"""gpt-oss tool calls that fail Ollama's JSON parser are rebuilt, not dropped.

Ollama ends the stream with ``{"error": "error parsing tool call: raw='…',
err=…"}`` when gpt-oss emits almost-JSON arguments, so the turn fails and the
client's retries resample the same shape.  Found 2026-10-27 recording a Claude
Code demo: ``{"command":"pytest -q"], …}`` failed one turn eight times running.
The raw arguments are in the error; the tool name is not, so it is inferred
from the request's tools -- and only when exactly one fits.

The route tests run the real routes, proxy and translators against a fake
Ollama at the HTTP layer, over every translated surface.
"""

from __future__ import annotations

import json
import logging

import pytest

from fleet_manager.server.tool_call_repair import recover_tool_call_from_ollama_error
from tests.test_server.test_upstream_incomplete import MODEL, _backend, _sse_payloads, fleet  # noqa: F401

BASH = {
    "type": "object",
    "properties": {
        "command": {"type": "string"},
        "description": {"type": "string"},
        "timeout": {"type": "number"},
    },
    "required": ["command"],
}
WRITE = {
    "type": "object",
    "properties": {"file_path": {"type": "string"}, "content": {"type": "string"}},
    "required": ["file_path", "content"],
}
TOOLS = [
    {"type": "function", "function": {"name": "Bash", "parameters": BASH}},
    {"type": "function", "function": {"name": "Write", "parameters": WRITE}},
]

# Verbatim shape from the 2026-10-27 failure.
STRAY_BRACKET = (
    'error parsing tool call: raw=\'{"command":"pytest -q"], "description":'
    '"Run pytest", "timeout": 120000}\', err=invalid character \']\' after object '
    "key:value pair"
)


class TestRecover:
    def test_stray_bracket_becomes_the_bash_call(self):
        assert recover_tool_call_from_ollama_error(STRAY_BRACKET, TOOLS) == (
            "Bash",
            {"command": "pytest -q", "description": "Run pytest", "timeout": 120000},
        )

    def test_valid_json_the_parser_still_rejected(self):
        err = 'error parsing tool call: raw=\'{"file_path":"a.py","content":"x = \'y\'"}\', err=x'
        assert recover_tool_call_from_ollama_error(err, TOOLS) == (
            "Write",
            {"file_path": "a.py", "content": "x = 'y'"},
        )

    def test_two_tools_fitting_is_no_guess(self):
        twin = [*TOOLS, {"type": "function", "function": {"name": "Shell", "parameters": BASH}}]
        assert recover_tool_call_from_ollama_error(STRAY_BRACKET, twin) is None

    def test_a_field_no_tool_declares_matches_nothing(self):
        """Dropping an unknown ``cwd`` would run the command somewhere else."""
        err = 'error parsing tool call: raw=\'{"command":"ls"], "cwd":"/"}\', err=x'
        assert recover_tool_call_from_ollama_error(err, TOOLS) is None

    def test_a_field_bled_from_another_tool_is_dropped(self):
        """gpt-oss put Bash's ``timeout`` on a Write (live, 2026-10-27)."""
        raw = '{"file_path":"/t.py","content":"x = 1\\n"], "timeout": 120000}'
        err = f"error parsing tool call: raw='{raw}', err=x"
        assert recover_tool_call_from_ollama_error(err, TOOLS) == (
            "Write",
            {"file_path": "/t.py", "content": "x = 1\n"},
        )

    def test_a_missing_required_field_matches_nothing(self):
        err = 'error parsing tool call: raw=\'{"file_path":"a.py"]}\', err=x'
        assert recover_tool_call_from_ollama_error(err, TOOLS) is None

    def test_a_wrong_type_matches_nothing(self):
        err = "error parsing tool call: raw='{\"command\": 5]}', err=x"
        assert recover_tool_call_from_ollama_error(err, TOOLS) is None

    @pytest.mark.parametrize(
        "err", ["model runner has unexpectedly stopped", "", "error parsing tool call"]
    )
    def test_other_errors_are_left_alone(self, err):
        assert recover_tool_call_from_ollama_error(err, TOOLS) is None

    def test_fields_promoted_to_required_with_a_default_do_not_block_a_match(self):
        """``fixup_tool_schemas`` makes Bash's optional fields required-with-default;
        a call without them is still a Bash call.  Missed live on the first build."""
        from fleet_manager.server.tool_schema_fixup import fixup_tool_schemas

        bash = {
            **BASH,
            "properties": {
                **BASH["properties"],
                "run_in_background": {"type": "boolean"},
                "dangerouslyDisableSandbox": {"type": "boolean"},
            },
        }
        tools = fixup_tool_schemas(
            [{"type": "function", "function": {"name": "Bash", "parameters": bash}}, TOOLS[1]]
        )
        assert len(tools[0]["function"]["parameters"]["required"]) > 1  # the premise
        err = (
            "error parsing tool call: "
            'raw=\'{"command":"python3 -m pytest -q"],"timeout":120000}\', err=x'
        )
        assert recover_tool_call_from_ollama_error(err, tools) == (
            "Bash",
            {"command": "python3 -m pytest -q", "timeout": 120000},
        )

    def test_file_content_survives_byte_for_byte(self):
        """json-repair drops a trailing newline; the targeted fix must run first."""
        content = 'import x\n\nassert f(["a"], {"k": 1}) == ["a"]\n'
        raw = '{"content":' + json.dumps(content) + '],"file_path":"/t.py"}'
        err = f"error parsing tool call: raw='{raw}', err=x"
        assert recover_tool_call_from_ollama_error(err, TOOLS) == (
            "Write",
            {"content": content, "file_path": "/t.py"},
        )

    def test_no_tools_offered(self):
        assert recover_tool_call_from_ollama_error(STRAY_BRACKET, None) is None


THINKING = {
    "model": MODEL,
    "message": {"role": "assistant", "content": "", "thinking": "run tests"},
    "done": False,
}
MESSAGES = [{"role": "user", "content": "run the tests"}]
REQUESTS = {
    "anthropic": (
        "/v1/messages",
        {
            "model": MODEL,
            "max_tokens": 256,
            "messages": MESSAGES,
            "tools": [
                {"name": t["function"]["name"], "input_schema": t["function"]["parameters"]}
                for t in TOOLS
            ],
        },
    ),
    "openai": ("/v1/chat/completions", {"model": MODEL, "messages": MESSAGES, "tools": TOOLS}),
    "responses": (
        "/v1/responses",
        {
            "model": MODEL,
            "input": "run the tests",
            "tools": [{"type": "function", **t["function"]} for t in TOOLS],
        },
    ),
}
SURFACES = list(REQUESTS)


def _post(client, surface, *, stream):
    path, body = REQUESTS[surface]
    return client.post(path, json={**body, "stream": stream})


def _tool_call(surface: str, text: str) -> tuple[str, dict]:
    events = _sse_payloads(text)
    if surface == "anthropic":
        start = next(
            e
            for e in events
            if e.get("type") == "content_block_start" and e["content_block"]["type"] == "tool_use"
        )
        args = "".join(
            e["delta"]["partial_json"]
            for e in events
            if e.get("type") == "content_block_delta"
            and e["delta"].get("type") == "input_json_delta"
        )
        return start["content_block"]["name"], json.loads(args) if args else start["content_block"][
            "input"
        ]
    if surface == "openai":
        calls = [
            c for e in events for c in (e["choices"][0].get("delta") or {}).get("tool_calls") or []
        ]
        fn = calls[0]["function"]
        return fn["name"], json.loads("".join(c["function"].get("arguments", "") for c in calls))
    done = next(
        e
        for e in events
        if e.get("type") == "response.output_item.done" and e["item"]["type"] == "function_call"
    )
    return done["item"]["name"], json.loads(done["item"]["arguments"])


class TestEverySurface:
    @pytest.mark.parametrize("surface", SURFACES)
    def test_the_rebuilt_call_reaches_the_client(self, fleet, surface, caplog):  # noqa: F811
        _backend(fleet, [THINKING, {"error": STRAY_BRACKET}])
        with caplog.at_level(logging.INFO, logger="fleet_manager.server.streaming"):
            text = _post(fleet, surface, stream=True).text
        name, args = _tool_call(surface, text)
        assert name == "Bash" and args["command"] == "pytest -q"
        assert "event: error" not in text and "response.failed" not in text
        recovered = [r for r in caplog.records if "Recovered a Bash tool call" in r.getMessage()]
        assert recovered and all(r.levelno == logging.INFO for r in recovered)

    @pytest.mark.parametrize("surface", SURFACES)
    def test_non_streaming_is_200_with_the_call(self, fleet, surface):  # noqa: F811
        _backend(fleet, [THINKING, {"error": STRAY_BRACKET}])
        resp = _post(fleet, surface, stream=False)
        assert resp.status_code == 200
        assert "pytest -q" in resp.text

    @pytest.mark.parametrize("surface", SURFACES)
    def test_an_unrecoverable_error_still_fails_and_says_why(self, fleet, surface, caplog):  # noqa: F811
        _backend(fleet, [THINKING, {"error": "model runner has unexpectedly stopped"}])
        with caplog.at_level(logging.WARNING, logger="fleet_manager.server.streaming"):
            resp = _post(fleet, surface, stream=False)
        assert resp.status_code == 502
        assert any("unexpectedly stopped" in r.getMessage() for r in caplog.records)
