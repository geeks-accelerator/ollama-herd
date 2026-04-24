"""Tests for tool_schema_fixup — the Qwen3-Coder long-context bug workaround.

Anchor claim (from llama.cpp#20164): promoting optional params to required
eliminates the tool-call looping bug at 30K+ tokens.  These tests lock in
that each of the heavily-used Claude Code CLI tools produces a schema
where the common optional params are now required.
"""

from __future__ import annotations

from fleet_manager.server.tool_schema_fixup import (
    CLAUDE_CODE_TOOL_DEFAULTS,
    MODE_INJECT,
    MODE_OFF,
    MODE_PROMOTE_EXISTING,
    fixup_tool_schema,
    fixup_tool_schemas,
)


# Real schemas captured from Claude Code CLI via FLEET_DEBUG_REQUEST_BODIES.
# Trimmed for readability; the fields that matter (name + properties +
# required) are preserved verbatim.

BASH_TOOL = {
    "type": "function",
    "function": {
        "name": "Bash",
        "description": "Executes a bash command.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "number"},
                "description": {"type": "string"},
                "run_in_background": {"type": "boolean"},
                "dangerouslyDisableSandbox": {"type": "boolean"},
            },
            "required": ["command"],
        },
    },
}

GREP_TOOL = {
    "type": "function",
    "function": {
        "name": "Grep",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
                "glob": {"type": "string"},
                "output_mode": {"type": "string"},
                "-n": {"type": "boolean"},
                "-i": {"type": "boolean"},
                "head_limit": {"type": "number"},
                "offset": {"type": "number"},
                "multiline": {"type": "boolean"},
            },
            "required": ["pattern"],
        },
    },
}

READ_TOOL = {
    "type": "function",
    "function": {
        "name": "Read",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "offset": {"type": "integer"},
                "limit": {"type": "integer"},
                "pages": {"type": "string"},
            },
            "required": ["file_path"],
        },
    },
}

UNKNOWN_TOOL = {
    "type": "function",
    "function": {
        "name": "SomeProprietaryTool",
        "parameters": {
            "type": "object",
            "properties": {
                "a": {"type": "string"},
                "b": {"type": "string"},
            },
            "required": ["a"],
        },
    },
}


# ---------------------------------------------------------------------------
# Mode: off — no-op
# ---------------------------------------------------------------------------


def test_mode_off_returns_input_unchanged():
    out = fixup_tool_schema(BASH_TOOL, mode=MODE_OFF)
    # Same object identity is acceptable for mode=off (no copy needed)
    assert out is BASH_TOOL


def test_unknown_mode_logs_warning_and_passes_through():
    out = fixup_tool_schema(BASH_TOOL, mode="no-such-mode")
    assert out is BASH_TOOL


# ---------------------------------------------------------------------------
# Mode: inject — the actual fix for Claude Code CLI
# ---------------------------------------------------------------------------


def test_inject_promotes_bash_optional_params_with_known_defaults():
    fixed = fixup_tool_schema(BASH_TOOL, mode=MODE_INJECT)
    required = set(fixed["function"]["parameters"]["required"])
    props = fixed["function"]["parameters"]["properties"]
    # All three defaulted Bash fields promoted
    assert "timeout" in required
    assert "run_in_background" in required
    assert "dangerouslyDisableSandbox" in required
    # And they carry the injected defaults so the model knows what to emit
    assert props["timeout"]["default"] == 120000
    assert props["run_in_background"]["default"] is False
    assert props["dangerouslyDisableSandbox"]["default"] is False
    # Fields without a known-safe default are left alone
    assert "description" not in required


def test_inject_promotes_grep_high_optional_count():
    """Grep has the worst case — many optional params.  The fix removes as
    many optional holes as we have defaults for."""
    fixed = fixup_tool_schema(GREP_TOOL, mode=MODE_INJECT)
    required = set(fixed["function"]["parameters"]["required"])
    # Original required field survives
    assert "pattern" in required
    # All six defaulted Grep fields promoted
    for p in ("output_mode", "-n", "-i", "multiline", "head_limit", "offset"):
        assert p in required, f"{p!r} should have been promoted"


def test_inject_read_promotes_offset_but_not_limit():
    """Policy: Read.offset gets default=0 (safe); Read.limit does NOT
    (forcing 2000 would change semantics for <2000-line files)."""
    fixed = fixup_tool_schema(READ_TOOL, mode=MODE_INJECT)
    required = set(fixed["function"]["parameters"]["required"])
    props = fixed["function"]["parameters"]["properties"]
    assert "file_path" in required
    assert "offset" in required
    assert props["offset"]["default"] == 0
    assert "limit" not in required  # intentional per CLAUDE_CODE_TOOL_DEFAULTS policy
    assert "pages" not in required


def test_inject_unknown_tool_passes_through():
    """Tools we don't have a defaults table for must not be touched."""
    fixed = fixup_tool_schema(UNKNOWN_TOOL, mode=MODE_INJECT)
    assert fixed["function"]["parameters"]["required"] == ["a"]


def test_inject_does_not_mutate_input():
    """The function must not modify its input — tests assert identity preservation."""
    import copy as _copy
    original_bash = _copy.deepcopy(BASH_TOOL)
    fixup_tool_schema(BASH_TOOL, mode=MODE_INJECT)
    assert BASH_TOOL == original_bash


# ---------------------------------------------------------------------------
# Mode: promote (existing defaults only)
# ---------------------------------------------------------------------------


def test_promote_mode_noop_when_no_defaults_in_schema():
    """Claude Code's schemas don't emit `default` fields today, so this mode
    is effectively a no-op on real Claude Code traffic.  But exercising the
    code path is still valuable for forward-compat."""
    fixed = fixup_tool_schema(BASH_TOOL, mode=MODE_PROMOTE_EXISTING)
    # No promotions — Bash fields don't have `default` in the raw schema
    assert fixed["function"]["parameters"]["required"] == ["command"]


def test_promote_mode_promotes_when_default_present():
    tool = {
        "type": "function",
        "function": {
            "name": "Widget",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "string"},
                    "y": {"type": "integer", "default": 7},
                },
                "required": ["x"],
            },
        },
    }
    fixed = fixup_tool_schema(tool, mode=MODE_PROMOTE_EXISTING)
    required = set(fixed["function"]["parameters"]["required"])
    assert "x" in required
    assert "y" in required  # promoted because it had a default


# ---------------------------------------------------------------------------
# Batch helper
# ---------------------------------------------------------------------------


def test_fixup_tool_schemas_batches_a_mixed_list():
    out = fixup_tool_schemas([BASH_TOOL, UNKNOWN_TOOL], mode=MODE_INJECT)
    assert len(out) == 2
    # First tool got promotions
    bash_req = out[0]["function"]["parameters"]["required"]
    assert "timeout" in bash_req
    # Second tool untouched
    assert out[1]["function"]["parameters"]["required"] == ["a"]


def test_fixup_tool_schemas_none_input_returns_none():
    assert fixup_tool_schemas(None, mode=MODE_INJECT) is None


def test_fixup_tool_schemas_empty_input_returns_empty():
    assert fixup_tool_schemas([], mode=MODE_INJECT) == []


def test_custom_defaults_table_overrides_builtin():
    """Users / tests can pass their own defaults to change the policy without
    code changes.  Here we add a default for a tool the built-in table
    doesn't cover."""
    custom = {"SomeProprietaryTool": {"b": "hello"}}
    out = fixup_tool_schema(UNKNOWN_TOOL, mode=MODE_INJECT, defaults_table=custom)
    required = set(out["function"]["parameters"]["required"])
    assert "b" in required
    assert out["function"]["parameters"]["properties"]["b"]["default"] == "hello"


# ---------------------------------------------------------------------------
# Regression guard — CLAUDE_CODE_TOOL_DEFAULTS must stay in sync
# ---------------------------------------------------------------------------


def test_defaults_table_structure_is_sane():
    """Every value in the defaults table is a JSON-serializable primitive.
    Guards against someone accidentally putting a callable or non-JSON type
    that would break the outbound tool schema."""
    import json
    for tool_name, fields in CLAUDE_CODE_TOOL_DEFAULTS.items():
        assert isinstance(tool_name, str)
        assert isinstance(fields, dict)
        for field_name, default in fields.items():
            assert isinstance(field_name, str)
            # Round-trip through JSON to confirm serializability
            json.dumps(default)
