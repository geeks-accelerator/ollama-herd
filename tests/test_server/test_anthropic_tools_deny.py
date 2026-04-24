"""Tests for FLEET_ANTHROPIC_TOOLS_DENY — server-side tool filtering.

The filtering happens inline in ``routes/anthropic_compat.py`` — we
exercise it by mirroring the same logic here.  A full-integration test
would require spinning up the FastAPI app; keep this focused.
"""

from __future__ import annotations


def _filter_tools(source_tools, deny_csv):
    """Mirror of the inline filter in anthropic_compat.py."""
    if not source_tools or not deny_csv:
        return source_tools
    deny_set = {name.strip() for name in deny_csv.split(",") if name.strip()}
    if not deny_set:
        return source_tools
    return [t for t in source_tools if t.get("name") not in deny_set]


def test_empty_deny_passes_through():
    tools = [{"name": "Bash"}, {"name": "Read"}]
    assert _filter_tools(tools, "") == tools
    assert _filter_tools(tools, None) == tools


def test_single_tool_deny_strips_one():
    tools = [{"name": "Bash"}, {"name": "NotebookEdit"}, {"name": "Read"}]
    out = _filter_tools(tools, "NotebookEdit")
    assert [t["name"] for t in out] == ["Bash", "Read"]


def test_multi_tool_deny_strips_all_matches():
    tools = [
        {"name": "Bash"}, {"name": "TodoWrite"},
        {"name": "NotebookEdit"}, {"name": "Read"},
    ]
    out = _filter_tools(tools, "TodoWrite,NotebookEdit")
    assert [t["name"] for t in out] == ["Bash", "Read"]


def test_deny_ignores_whitespace_and_empty_entries():
    tools = [{"name": "Bash"}, {"name": "TodoWrite"}]
    out = _filter_tools(tools, " TodoWrite , ,  ")
    assert [t["name"] for t in out] == ["Bash"]


def test_deny_matches_exact_not_substring():
    """Denying "Edit" should NOT strip "NotebookEdit" or "MultiEdit"."""
    tools = [
        {"name": "Edit"}, {"name": "NotebookEdit"}, {"name": "MultiEdit"},
    ]
    out = _filter_tools(tools, "Edit")
    assert [t["name"] for t in out] == ["NotebookEdit", "MultiEdit"]


def test_deny_on_empty_tool_list():
    assert _filter_tools([], "Bash") == []
    assert _filter_tools(None, "Bash") is None


def test_deny_everything():
    tools = [{"name": "Bash"}, {"name": "Read"}]
    assert _filter_tools(tools, "Bash,Read") == []
