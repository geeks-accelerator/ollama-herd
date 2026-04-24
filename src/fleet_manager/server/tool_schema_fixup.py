"""Tool-schema fixup — work around Qwen3-Coder's long-context tool-call bug.

**The bug**:

Documented in [ggml-org/llama.cpp#20164](https://github.com/ggml-org/llama.cpp/issues/20164).
Qwen3-Coder's tool-call generation degrades once context passes ~30K tokens
(about 20% of advertised 128K–256K): it starts silently omitting **optional**
parameters, loops retrying with different omissions, and eventually abandons
the tool entirely.  The reporter confirmed the exact fix: **convert optional
parameters to required**.  With the optional flag gone, the parser no longer
has a hole to fall through.

**Why Claude Code hits this harder than most workloads**:

Claude Code ships ~27 tools, and most of the frequently-used ones have
multiple optional parameters — `Grep` alone has 13.  Empirically captured
from a live request body (2026-04-23): every optional field on `Bash`,
`Grep`, `Read`, `Agent`, etc. is marked optional and NO field has a
``default`` in the schema.  So the bug triggers every tool-heavy long
session.

**What this module does**:

Given one Anthropic tool definition (already translated to Ollama function
format), walks the JSON Schema ``properties`` and, for any property we have
a known-safe default for, injects ``default`` and promotes the field to
``required``.  The model still gets to override the default when it has a
reason, but it can no longer silently omit the field.

Properties for which we lack a confident default are left alone —
promoting without a default would force the model to hallucinate a value,
which is worse than the original bug.

See ``docs/research/why-claude-code-degrades-at-30k.md`` for the full
research + reasoning chain.

# EXTRACTION SEAM (recorded 2026-04-24):
# - Fleet-manager dependencies: NONE.  Pure Python, stdlib + logging only.
# - External dependencies: NONE at import time.
# - Public surface to preserve if extracted:
#     CLAUDE_CODE_TOOL_DEFAULTS (dict)
#     MODE_OFF / MODE_PROMOTE_EXISTING / MODE_INJECT / VALID_MODES (str consts)
#     fixup_tool_schema(ollama_tool, mode, *, defaults_table) -> dict
#     fixup_tool_schemas(ollama_tools, mode, *, defaults_table) -> list | None
# - Ollama function-tool shape assumed on input; no coupling to fleet types.
# - Drop-in copyable to any Anthropic→OpenAI/Ollama proxy.  See
#   ``docs/research/claude-code-local-ecosystem-landscape.md`` for the
#   landscape analysis that discusses when this becomes worth extracting.
"""

from __future__ import annotations

import copy
import logging
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Known-safe defaults for Claude Code CLI tools
#
# Keyed by (tool_name, param_name).  Values match Claude Code's documented
# behavior when the param is omitted, as of April 2026.  When Claude Code
# updates a default (rare), add the new version here — the old value stays
# compatible because the model still gets to override.
#
# Policy for what to include:
#   - Booleans with a clear on/off convention → always includable (e.g.
#     ``run_in_background=False``)
#   - Integers with a documented default → include the documented value
#   - Strings with a documented default → include the documented value
#   - Fields without a documented or safely-inferable default → SKIP.  We'd
#     rather leave the parser bug alone for that field than force the model
#     to invent a value.
# ---------------------------------------------------------------------------


CLAUDE_CODE_TOOL_DEFAULTS: dict[str, dict[str, Any]] = {
    "Bash": {
        "timeout": 120000,  # 2-minute default per tool description
        "run_in_background": False,
        "dangerouslyDisableSandbox": False,
    },
    "Read": {
        "offset": 0,  # read from start
        # limit intentionally omitted — "up to 2000 lines" default matches
        # absent-param behavior; forcing 2000 into every call would change
        # semantics for files < 2000 lines.
    },
    "Grep": {
        "output_mode": "files_with_matches",
        "-n": True,
        "-i": False,
        "multiline": False,
        "head_limit": 250,
        "offset": 0,
    },
    "Edit": {
        "replace_all": False,
    },
    "Write": {
        # No optional params in the current schema.
    },
    "Agent": {
        "run_in_background": False,
    },
    "WebFetch": {
        # No safe default for the ``prompt`` field; leave alone.
    },
    "TodoWrite": {
        # No optional params worth promoting.
    },
}


# Modes the caller can request.  Keeping this as strings (not an enum) so
# it maps cleanly to a single env var without pydantic gymnastics.

MODE_OFF = "off"                  # don't touch any schema (pre-fix behavior)
MODE_PROMOTE_EXISTING = "promote" # only promote properties that already have ``default``
MODE_INJECT = "inject"            # use CLAUDE_CODE_TOOL_DEFAULTS + promote
VALID_MODES = {MODE_OFF, MODE_PROMOTE_EXISTING, MODE_INJECT}


def fixup_tool_schema(
    ollama_tool: dict[str, Any],
    mode: str = MODE_INJECT,
    *,
    defaults_table: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a copy of ``ollama_tool`` with optional params promoted to required.

    ``ollama_tool`` is the function-shape produced by
    :func:`anthropic_tool_to_ollama`:

        {"type": "function",
         "function": {"name": "...", "parameters": {JSON Schema}}}

    Never mutates the input.  Safe to call on any tool — returns the input
    unchanged when ``mode`` is ``"off"``, when the shape isn't recognized,
    or when we have no defaults for the tool.

    ``defaults_table`` defaults to :data:`CLAUDE_CODE_TOOL_DEFAULTS`; tests
    and opt-in users can pass their own.
    """
    if mode not in VALID_MODES:
        logger.warning(
            f"tool_schema_fixup: unknown mode {mode!r}; treating as off",
        )
        return ollama_tool
    if mode == MODE_OFF:
        return ollama_tool

    function = ollama_tool.get("function") if isinstance(ollama_tool, dict) else None
    if not isinstance(function, dict):
        return ollama_tool

    params = function.get("parameters")
    if not isinstance(params, dict):
        return ollama_tool

    properties = params.get("properties")
    if not isinstance(properties, dict) or not properties:
        return ollama_tool

    tool_name = function.get("name") or ""
    table = defaults_table if defaults_table is not None else CLAUDE_CODE_TOOL_DEFAULTS
    tool_defaults = table.get(tool_name) or {}

    required_list = list(params.get("required") or [])
    required_set = set(required_list)

    # Build the fixed-up schema non-destructively
    new_props: dict[str, Any] = {}
    promoted: list[str] = []
    for pname, pschema in properties.items():
        if not isinstance(pschema, dict):
            new_props[pname] = pschema
            continue
        already_required = pname in required_set
        has_default = "default" in pschema

        # Decide whether to promote this property
        promote = False
        new_pschema = dict(pschema)

        if mode == MODE_PROMOTE_EXISTING:
            if has_default and not already_required:
                promote = True
        elif mode == MODE_INJECT:
            if has_default and not already_required:
                promote = True
            elif not has_default and pname in tool_defaults and not already_required:
                new_pschema["default"] = tool_defaults[pname]
                promote = True

        new_props[pname] = new_pschema
        if promote:
            promoted.append(pname)

    if not promoted:
        return ollama_tool

    new_params = dict(params)
    new_params["properties"] = new_props
    new_required = list(required_list)
    for p in promoted:
        if p not in required_set:
            new_required.append(p)
    new_params["required"] = new_required

    new_function = dict(function)
    new_function["parameters"] = new_params

    result = dict(ollama_tool)
    result["function"] = new_function

    logger.debug(
        f"tool_schema_fixup: {tool_name} promoted optional params to required: "
        f"{promoted}",
    )
    return result


def fixup_tool_schemas(
    ollama_tools: list[dict[str, Any]] | None,
    mode: str = MODE_INJECT,
    *,
    defaults_table: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]] | None:
    """Apply :func:`fixup_tool_schema` to each tool in a list.

    Summarises the total promotion count at INFO level so operators can
    tell from the log whether the fix is actually doing work on their
    workload.  Empty / None input is passed through unchanged.
    """
    if not ollama_tools:
        return ollama_tools
    # Use deepcopy on the input list to insulate callers from the nested-dict
    # updates we'd otherwise do — the list itself is rebuilt fresh, but nested
    # params dicts could be shared if we didn't copy.  Cheaper than it looks:
    # tool schemas are small.
    ollama_tools = copy.deepcopy(ollama_tools)
    out: list[dict[str, Any]] = []
    total_promotions = 0
    for tool in ollama_tools:
        fixed = fixup_tool_schema(tool, mode=mode, defaults_table=defaults_table)
        out.append(fixed)
        # Count promotions for logging: a tool was touched if its required
        # list grew.
        try:
            before = len((tool.get("function") or {}).get("parameters", {}).get("required") or [])
            after = len((fixed.get("function") or {}).get("parameters", {}).get("required") or [])
            total_promotions += max(0, after - before)
        except Exception:  # noqa: BLE001 — logging-only
            pass
    if total_promotions > 0 and mode != MODE_OFF:
        logger.info(
            f"tool_schema_fixup(mode={mode}): promoted {total_promotions} "
            f"optional param(s) to required across {len(out)} tool(s)"
        )
    return out
