"""Tests for anthropic_translator.anthropic_to_ollama_messages.

Focus: the tool_use / tool_result id-preservation path that keeps
multi-call agent turns correlatable when the Ollama-shape body is then
forwarded to a strict OpenAI backend (mlx_lm.server).  Without id
preservation the assistant can't reason about "which tool result
belongs to which of my tool calls" in the same turn.
"""

from __future__ import annotations

from fleet_manager.server.anthropic_translator import (
    _normalize_cache_busting_tokens,
    anthropic_system_to_text,
    anthropic_to_ollama_messages,
)

# ---------------------------------------------------------------------------
# Cache-busting token normalization (cch=X → NORMALIZED)
# ---------------------------------------------------------------------------


def test_normalize_cch_token_stripped():
    """Claude Code's per-request ``cch=<hex>`` fingerprint gets normalized."""
    inp = "x-anthropic-billing-header: cc_version=2.1.117.bc2; cc_entrypoint=cli; cch=3247f;"
    out = _normalize_cache_busting_tokens(inp)
    assert "cch=3247f" not in out
    assert "cch=NORMALIZED" in out
    # Rest of the header is preserved
    assert "cc_version=2.1.117.bc2" in out
    assert "cc_entrypoint=cli" in out


def test_normalize_cch_stable_across_invocations():
    """Different fingerprints collapse to the same normalized string — this
    is what unlocks mlx_lm.server prompt cache hits on subsequent turns."""
    a = _normalize_cache_busting_tokens("cch=3247f;")
    b = _normalize_cache_busting_tokens("cch=f1ace;")
    c = _normalize_cache_busting_tokens("cch=40156;")
    assert a == b == c == "cch=NORMALIZED;"


def test_normalize_cch_leaves_unmatched_text_alone():
    """Prompts without a cch= pass through unchanged."""
    inp = "You are a helpful assistant. Use tools when needed."
    assert _normalize_cache_busting_tokens(inp) == inp


def test_system_to_text_normalizes_cch_in_string_form():
    system = "billing cch=abc12; you are a helpful assistant"
    out = anthropic_system_to_text(system)
    assert "cch=abc12" not in out
    assert "cch=NORMALIZED" in out


def test_system_to_text_normalizes_cch_in_block_array():
    """The text-block-array form of system prompt also gets normalized."""
    system = [
        {"type": "text", "text": "header cch=deadbeef;"},
        {"type": "text", "text": "rules"},
    ]
    out = anthropic_system_to_text(system)
    assert "cch=deadbeef" not in out
    assert "cch=NORMALIZED" in out
    assert "rules" in out  # other text preserved

# ---------------------------------------------------------------------------
# Basic shape — plain text
# ---------------------------------------------------------------------------


def test_plain_text_roundtrip():
    msgs = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]
    out = anthropic_to_ollama_messages(msgs)
    assert out == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]


def test_system_prompt_prepended_when_provided():
    out = anthropic_to_ollama_messages(
        [{"role": "user", "content": "hi"}],
        system="you are helpful",
    )
    assert out[0] == {"role": "system", "content": "you are helpful"}
    assert out[1] == {"role": "user", "content": "hi"}


def test_structured_text_blocks_concatenate():
    """Anthropic multi-text-block messages flatten to a single content string."""
    msgs = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "first line"},
            {"type": "text", "text": "second line"},
        ],
    }]
    out = anthropic_to_ollama_messages(msgs)
    assert out[0]["role"] == "user"
    assert "first line" in out[0]["content"]
    assert "second line" in out[0]["content"]


# ---------------------------------------------------------------------------
# tool_use id preservation — the gap we just closed
# ---------------------------------------------------------------------------


def test_tool_use_id_preserved_on_assistant_message():
    """The Anthropic tool_use id must flow through to tool_calls[].id so the
    subsequent tool_result can reference it."""
    msgs = [{
        "role": "assistant",
        "content": [
            {"type": "text", "text": "I'll run a command"},
            {
                "type": "tool_use",
                "id": "toolu_01ABC123",
                "name": "Bash",
                "input": {"command": "ls"},
            },
        ],
    }]
    out = anthropic_to_ollama_messages(msgs)
    assert len(out) == 1
    assert out[0]["role"] == "assistant"
    assert out[0]["content"] == "I'll run a command"
    tcs = out[0]["tool_calls"]
    assert len(tcs) == 1
    assert tcs[0]["id"] == "toolu_01ABC123"
    assert tcs[0]["function"]["name"] == "Bash"
    assert tcs[0]["function"]["arguments"] == {"command": "ls"}


def test_tool_use_without_id_still_works():
    """Defensive: if a malformed tool_use lacks an id, don't crash — emit
    without id and let downstream (mlx_proxy) generate one."""
    msgs = [{
        "role": "assistant",
        "content": [
            {"type": "tool_use", "name": "Bash", "input": {}},
        ],
    }]
    out = anthropic_to_ollama_messages(msgs)
    tc = out[0]["tool_calls"][0]
    assert "id" not in tc
    assert tc["function"]["name"] == "Bash"


def test_tool_result_tool_use_id_preserved_as_tool_call_id():
    """The Anthropic tool_result.tool_use_id must flow through to
    tool_call_id on the emitted role:"tool" message — this is what lets
    OpenAI-strict backends correlate the result with the original call."""
    msgs = [{
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_01ABC123",
                "content": "total 0\nfile.txt",
            },
        ],
    }]
    out = anthropic_to_ollama_messages(msgs)
    assert len(out) == 1
    tool_msg = out[0]
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "toolu_01ABC123"
    assert tool_msg["content"] == "total 0\nfile.txt"


def test_multi_tool_call_turn_preserves_correlation():
    """The canonical agent pattern — assistant makes N calls, user returns
    N results.  Each result must map to its originating call by id.
    Regression guard: before the fix, both ids were dropped and a
    multi-call turn became ambiguous."""
    msgs = [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "running two commands"},
                {"type": "tool_use", "id": "toolu_a", "name": "Bash", "input": {"command": "pwd"}},
                {"type": "tool_use", "id": "toolu_b", "name": "Bash", "input": {"command": "ls"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_a", "content": "/home/x"},
                {"type": "tool_result", "tool_use_id": "toolu_b", "content": "file.txt"},
            ],
        },
    ]
    out = anthropic_to_ollama_messages(msgs)
    # assistant message with both calls
    assert out[0]["role"] == "assistant"
    call_ids = [tc["id"] for tc in out[0]["tool_calls"]]
    assert call_ids == ["toolu_a", "toolu_b"]
    # two tool messages, each correlated by id to the right call
    tool_msgs = [m for m in out if m["role"] == "tool"]
    assert len(tool_msgs) == 2
    assert tool_msgs[0]["tool_call_id"] == "toolu_a"
    assert tool_msgs[0]["content"] == "/home/x"
    assert tool_msgs[1]["tool_call_id"] == "toolu_b"
    assert tool_msgs[1]["content"] == "file.txt"


def test_tool_result_structured_content_flattens_text_blocks():
    """Anthropic tool_result.content can itself be a list of text blocks
    (when the tool returned structured output).  Flatten to newline-joined
    text for Ollama/OpenAI."""
    msgs = [{
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_x",
                "content": [
                    {"type": "text", "text": "line 1"},
                    {"type": "text", "text": "line 2"},
                ],
            },
        ],
    }]
    out = anthropic_to_ollama_messages(msgs)
    assert out[0]["content"] == "line 1\nline 2"
    assert out[0]["tool_call_id"] == "toolu_x"


def test_thinking_blocks_dropped():
    """Thinking-model scratchpads shouldn't be replayed to non-thinking backends."""
    msgs = [{
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "let me think..."},
            {"type": "text", "text": "the answer is 42"},
        ],
    }]
    out = anthropic_to_ollama_messages(msgs)
    assert out[0]["content"] == "the answer is 42"
    assert "thinking" not in out[0]["content"]


# ---------------------------------------------------------------------------
# Unknown content block types — microcompact / future beta awareness
# ---------------------------------------------------------------------------


def test_unknown_block_types_are_skipped_not_crashed():
    """Silent skip is the correct behavior for directive blocks like
    ``cache_edits`` (microcompact) — they're instructions to Anthropic's
    server cache, not content to replay to local models.  But we should
    never crash on them or leak them through to the outbound Ollama body."""
    from fleet_manager.server.anthropic_translator import anthropic_to_ollama_messages
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "hello"},
                {"type": "cache_edits", "edits": [
                    {"type": "delete", "cache_reference": "ref-abc"},
                ]},
                {"type": "text", "text": "world"},
            ],
        },
    ]
    out = anthropic_to_ollama_messages(msgs)
    # Text parts survived; cache_edits silently skipped.
    user_msg = next(m for m in out if m["role"] == "user")
    assert "hello" in user_msg["content"]
    assert "world" in user_msg["content"]
    # No reference to cache_edits leaked through
    import json
    assert "cache_edits" not in json.dumps(out)


def test_unknown_block_type_logged_once_per_process(caplog):
    """Dedupe — a burst of requests with the same unknown block type
    logs ONCE, not per-block-per-request (otherwise we'd spam the log
    once microcompact fires)."""
    import logging
    from fleet_manager.server.anthropic_translator import (
        _LOGGED_UNKNOWN_BLOCK_TYPES,
        anthropic_to_ollama_messages,
    )
    # Clear dedupe state for deterministic test
    _LOGGED_UNKNOWN_BLOCK_TYPES.discard("cache_edits_test_unique")
    msgs = [
        {"role": "user", "content": [
            {"type": "cache_edits_test_unique", "payload": "anything"},
        ]},
    ]
    with caplog.at_level(logging.INFO, logger="fleet_manager.server.anthropic_translator"):
        anthropic_to_ollama_messages(msgs)
        anthropic_to_ollama_messages(msgs)
        anthropic_to_ollama_messages(msgs)
    unknown_logs = [r for r in caplog.records if "cache_edits_test_unique" in r.getMessage()]
    assert len(unknown_logs) == 1  # logged once across 3 calls
