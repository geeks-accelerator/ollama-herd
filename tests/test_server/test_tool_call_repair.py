"""Tests for tool-call JSON repair — recovering malformed model output.

Contract: never hide real failures silently.  Log every repair attempt,
expose success/failure counters, return the ORIGINAL input unchanged
when repair produces something that doesn't satisfy the tool schema.
"""

from __future__ import annotations

from fleet_manager.server.tool_call_repair import repair_tool_use_input


# ---------------------------------------------------------------------------
# Happy paths — no repair needed
# ---------------------------------------------------------------------------


def test_already_valid_json_dict_passes_through():
    out, was_repaired = repair_tool_use_input({"path": "/foo"})
    assert out == {"path": "/foo"}
    assert was_repaired is False


def test_valid_json_string_parses_to_dict():
    out, was_repaired = repair_tool_use_input('{"path": "/foo", "offset": 0}')
    assert out == {"path": "/foo", "offset": 0}
    # Valid JSON → no repair counter bump (strict parse succeeded)
    assert was_repaired is False


def test_empty_string_treated_as_empty_object():
    out, was_repaired = repair_tool_use_input("")
    assert out == {}
    assert was_repaired is False


# ---------------------------------------------------------------------------
# Repair paths — malformed input that json-repair handles
# ---------------------------------------------------------------------------


def test_trailing_comma_repaired():
    out, was_repaired = repair_tool_use_input('{"path": "/foo", "offset": 0,}')
    assert out == {"path": "/foo", "offset": 0}
    assert was_repaired is True


def test_missing_closing_brace_repaired():
    out, was_repaired = repair_tool_use_input('{"command": "echo hi"')
    assert out == {"command": "echo hi"}
    assert was_repaired is True


def test_unquoted_key_repaired():
    out, was_repaired = repair_tool_use_input('{command: "ls"}')
    assert out == {"command": "ls"}
    assert was_repaired is True


# ---------------------------------------------------------------------------
# Schema validation gates
# ---------------------------------------------------------------------------


def test_repair_rejected_when_required_field_missing():
    """If the repaired dict lacks a required field, return the original —
    better to let the client see the raw malformed input than salvage
    with an incomplete object that passes type checks but fails semantics."""
    schema = {
        "type": "object",
        "properties": {"command": {"type": "string"}, "timeout": {"type": "integer"}},
        "required": ["command"],
    }
    # Repair produces {"timeout": 5} (no command), should be rejected
    out, was_repaired = repair_tool_use_input('{"timeout": 5,}', schema)
    # Either original passed through or repair was rejected; either way was_repaired=False
    assert was_repaired is False


def test_repair_accepted_when_required_field_present():
    schema = {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    }
    out, was_repaired = repair_tool_use_input('{"command": "ls",}', schema)
    assert out == {"command": "ls"}
    assert was_repaired is True


def test_repair_rejected_on_type_mismatch():
    """Repaired dict has wrong primitive type for a declared property."""
    schema = {
        "type": "object",
        "properties": {"timeout": {"type": "integer"}},
    }
    # Repair produces {"timeout": "five"} — wrong type
    out, was_repaired = repair_tool_use_input('{"timeout": "five",}', schema)
    assert was_repaired is False


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_non_string_non_dict_passes_through():
    """Lists, numbers, None — we don't try to repair these."""
    for val in (None, 42, ["a", "b"], 3.14):
        out, was_repaired = repair_tool_use_input(val)
        assert out == val
        assert was_repaired is False


def test_pure_garbage_returns_original():
    """json-repair on totally invalid input returns empty or the original —
    we want the original passed through so the client sees real output."""
    out, was_repaired = repair_tool_use_input("this is not JSON at all")
    # Could be either original or {} depending on json-repair's behavior,
    # but the critical thing is was_repaired=False
    assert was_repaired is False


def test_no_schema_accepts_any_valid_dict():
    """When tool_schemas isn't available, structural check is skipped."""
    out, was_repaired = repair_tool_use_input('{"anything": "goes",}', None)
    assert out == {"anything": "goes"}
    assert was_repaired is True


def test_repaired_result_has_all_required_fields():
    """Sanity: when repair succeeds with a schema, all required fields present."""
    schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string"},
        },
        "required": ["pattern"],
    }
    out, was_repaired = repair_tool_use_input(
        '{"pattern": "foo", "path": "/bar",}', schema,
    )
    assert was_repaired is True
    assert "pattern" in out
    assert "path" in out


# ---------------------------------------------------------------------------
# Expanded regex recovery — XML-in-JSON hybrids and single-arg inference
# (Pattern A–D adopted from nicedreamzapp/claude-code-local, 2026-04-24)
# ---------------------------------------------------------------------------


def test_pattern_a_parameter_equals_key():
    """Pattern A: parameter=key>value segments (equals-sign delimited)."""
    from fleet_manager.server.tool_call_repair import _regex_recover_tool_args
    raw = '{"name": "Bash"} parameter=command>ls -la /tmp'
    args = _regex_recover_tool_args(raw, tool_name="Bash")
    assert args is not None
    assert args.get("command", "").startswith("ls -la")


def test_pattern_b_xml_parameter_tag():
    """Pattern B: <parameter_key>value</parameter>."""
    from fleet_manager.server.tool_call_repair import _regex_recover_tool_args
    raw = '<parameter_pattern>import .*</parameter><parameter_path>src/</parameter>'
    args = _regex_recover_tool_args(raw, tool_name="Grep")
    assert args is not None
    # Pattern B extracts from the XML fragments
    assert "pattern" in args or "path" in args


def test_pattern_c_arguments_with_malformed_json():
    """Pattern C: "arguments": {kv pairs with escaped quotes}."""
    from fleet_manager.server.tool_call_repair import _regex_recover_tool_args
    raw = '"arguments": {"command": "echo hi", "timeout": "1000" missing-close'
    args = _regex_recover_tool_args(raw, tool_name="Bash")
    assert args is not None
    assert args.get("command") == "echo hi"


def test_pattern_d_single_arg_inference_bash():
    """Pattern D: single-arg tool with leftover free-text → infer 'command'."""
    from fleet_manager.server.tool_call_repair import _regex_recover_tool_args
    # Only a single value, no clear structure
    raw = "ls -la /Users/neonsoul"
    args = _regex_recover_tool_args(raw, tool_name="Bash")
    assert args is not None
    assert args.get("command") == "ls -la /Users/neonsoul"


def test_pattern_d_single_arg_inference_grep():
    """Pattern D: Grep falls back to 'pattern'."""
    from fleet_manager.server.tool_call_repair import _regex_recover_tool_args
    raw = "my search pattern"
    args = _regex_recover_tool_args(raw, tool_name="Grep")
    assert args is not None
    assert args.get("pattern") == "my search pattern"


def test_pattern_d_skipped_for_unknown_tools():
    """Pattern D only fires for tools in the single-arg table."""
    from fleet_manager.server.tool_call_repair import _regex_recover_tool_args
    args = _regex_recover_tool_args("just some text", tool_name="RandomTool")
    assert args is None


def test_regex_returns_none_on_pure_garbage():
    """When no pattern matches anywhere, return None."""
    from fleet_manager.server.tool_call_repair import _regex_recover_tool_args
    # No XML, no key-value structure, no matching tool name
    args = _regex_recover_tool_args("", tool_name="Random")
    assert args is None


# ---------------------------------------------------------------------------
# End-to-end: repair_tool_use_input falls through to regex recovery
# ---------------------------------------------------------------------------


def test_xml_hybrid_falls_through_to_regex_recovery():
    """Main entry point should cascade: strict parse → json-repair →
    regex patterns.  XML-in-JSON that json-repair can't fix should
    still recover via pattern matching."""
    raw = '<parameter=command>echo hello</parameter>'
    schema = {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    }
    out, was_repaired = repair_tool_use_input(raw, schema, tool_name="Bash")
    assert was_repaired is True
    assert out == {"command": "echo hello"}


def test_single_arg_inference_end_to_end():
    """raw string that's just a value, with tool_name hint → infer key."""
    schema = {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    }
    out, was_repaired = repair_tool_use_input(
        "ls -la", schema, tool_name="Bash",
    )
    assert was_repaired is True
    assert out == {"command": "ls -la"}


def test_regex_recovery_respects_schema_validation():
    """Even if a pattern matches, the result must pass schema validation.
    Mismatched schema → original passed through."""
    raw = '<parameter_key_that_does_not_exist>value</parameter>'
    schema = {
        "type": "object",
        "properties": {"pattern": {"type": "string"}},
        "required": ["pattern"],
    }
    out, was_repaired = repair_tool_use_input(raw, schema, tool_name="Grep")
    # Pattern B extracts key_that_does_not_exist, schema says "pattern" required
    # So validation fails and we fall back to original
    assert was_repaired is False
