"""Repair malformed JSON in tool-call arguments emitted by local models.

Local coding models occasionally produce tool-call JSON that's *almost*
valid — trailing commas, unescaped quotes, missing brackets — especially
under long-context pressure.  Claude Code's Anthropic SDK parser rejects
these and the session errors or loops on retries.

This module offers a best-effort server-side repair that:

1. Tries to parse the model's ``tool_use.input`` as JSON.
2. If that fails, runs it through ``json-repair`` (pure-Python lib that
   handles trailing commas, unquoted keys, unescaped quotes, missing
   brackets, and similar single-character syntax errors).
3. Validates the repaired dict against the tool's ``input_schema``
   (structural check only — required fields + type names, no deep
   constraint validation).
4. If ALL of the above succeeds, returns the repaired dict + True.
   Otherwise returns the original input unchanged + False.

Design principles:

- **Never hide failures silently.**  Every repair attempt logs a WARNING
  with the original + repaired input (truncated).  A sustained repair
  rate above ~5% on any model is a signal that model is unreliable, not
  a license to keep masking it.
- **Prefer original on any doubt.**  If repair produces something that
  doesn't pass schema validation, we return the original.  Claude Code's
  own parser is stricter than ours and will surface the real error —
  which is information the user needs.
- **Pure, no I/O, no network.**  The caller owns metrics and logging
  integration so this module stays testable.

# EXTRACTION SEAM (recorded 2026-04-24):
# - Fleet-manager dependencies: NONE.
# - External dependencies: ``json-repair`` (lazy-imported inside the
#   function so a missing install degrades gracefully — no import-time
#   crash if the lib isn't present, just a WARNING log and pass-through).
# - Public surface to preserve if extracted:
#     repair_tool_use_input(raw_input, input_schema=None) -> (value, was_repaired)
#     _structurally_valid_against_schema(...) — internal but small
# - The caller owns metrics.  ``build_anthropic_non_streaming_response``
#   in ``mlx_proxy.py`` passes a mutable ``repair_stats`` dict that this
#   module increments in place.  The same pattern would work for any
#   extracted consumer — no coupling to fleet MlxProxy.
# - Pairs with ``tool_schema_fixup`` and ``context_management`` as the
#   self-contained "reliability layer."  See
#   ``docs/research/claude-code-local-ecosystem-landscape.md``.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# Known-single-arg Claude Code tools and their primary argument name.  Used
# by Pattern D (see ``_regex_recover_tool_args``) to infer which key to
# store a value under when the model emits a single value without any key
# labeling.  Conservative table — only tools with one obvious "main" arg.
# Extracted from Claude Code's published tool schemas as of 2026-04.
_SINGLE_ARG_TOOL_DEFAULTS: dict[str, str] = {
    "Bash": "command",
    "Read": "file_path",
    "Write": "file_path",
    "Glob": "pattern",
    "Grep": "pattern",
    "WebFetch": "url",
    "WebSearch": "query",
    "TodoWrite": "todos",
}


def _structurally_valid_against_schema(
    repaired: dict[str, Any], input_schema: dict[str, Any] | None,
) -> bool:
    """Lightweight structural check.  Not a full JSON Schema validator.

    Verifies the repaired dict has:
      - all ``required`` fields declared by the schema
      - primitive-type matches on declared properties (string vs int vs bool)

    Deliberately doesn't enforce enum values, array item schemas, nested
    objects, etc. — full validation would need the ``jsonschema`` dep and
    a lot of edge cases, and we're trying to be permissive rather than
    pedantic.  The client-side parser will catch anything subtle.
    """
    if not input_schema:
        # No schema → nothing to check against.  Accept whatever parsed.
        return True
    if not isinstance(repaired, dict):
        return False

    required = input_schema.get("required") or []
    if not isinstance(required, list):
        required = []
    for req in required:
        if isinstance(req, str) and req not in repaired:
            return False

    # Type check declared properties
    properties = input_schema.get("properties") or {}
    if not isinstance(properties, dict):
        return True
    type_map = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "array": list,
        "object": dict,
        "null": type(None),
    }
    for name, spec in properties.items():
        if name not in repaired or not isinstance(spec, dict):
            continue
        declared = spec.get("type")
        if declared is None or declared not in type_map:
            continue
        expected = type_map[declared]
        actual = repaired[name]
        # "null" declared as type means None is acceptable
        if actual is None and declared != "null":
            # Many schemas don't accept null even when optional; skip check
            continue
        if not isinstance(actual, expected):
            return False
    return True


def _regex_recover_tool_args(
    raw_text: str,
    tool_name: str | None = None,
) -> dict[str, Any] | None:
    """Recover tool arguments from XML-in-JSON hybrid model output.

    Backstop for cases json-repair can't handle: the model drops out of
    JSON mode partway and emits XML tags (``<parameter=key>value``,
    ``<parameter_key>value</parameter>``) or free-text bodies interleaved
    with JSON fragments.  Adapted from nicedreamzapp/claude-code-local's
    four-pattern catalog (``recover_garbled_tool_json``).

    Returns a dict of `{key: value}` if ANY pattern extracts at least one
    argument; ``None`` otherwise.  The caller is responsible for schema
    validation — this function is permissive.

    Four patterns tried in order:
      A. ``parameter=key>value`` — equals-sign delimited, no quotes
      B. ``<parameter_key>value`` or ``<parameter_key>[...]`` — XML-ish
      C. Malformed JSON inside ``"arguments": {"key": "val", ...}`` with
         escaped or partially-escaped quotes
      D. Single-arg tools with leftover free-text — infer key from
         ``_SINGLE_ARG_TOOL_DEFAULTS`` table
    """
    arguments: dict[str, Any] = {}

    # Pattern A — "parameter=key>value" blocks
    for m in re.finditer(
        r'["\s,]?parameter=(\w+)>\s*(.*?)(?:</parameter>|$)',
        raw_text, re.DOTALL,
    ):
        key = m.group(1)
        val = m.group(2).strip().rstrip('"}\n')
        if key and val:
            arguments[key] = val

    # Pattern B — "<parameter_key>value" or "<parameter=key>" variants
    if not arguments:
        for m in re.finditer(
            r'<parameter[_=](\w+)>\s*(.*?)(?:</parameter|<|$)',
            raw_text, re.DOTALL,
        ):
            key = m.group(1)
            val = m.group(2).strip().strip('[]"')
            if key and val:
                arguments[key] = val

    # Pattern C — salvage kv pairs from inside an "arguments" object
    if not arguments:
        args_match = re.search(
            r'"arguments"\s*:\s*\{(.*)', raw_text, re.DOTALL,
        )
        if args_match:
            for m in re.finditer(
                r'"(\w+)"\s*:\s*"((?:[^"\\]|\\.)*)"',
                args_match.group(1),
            ):
                arguments[m.group(1)] = m.group(2)

    # Pattern D — single-arg tool fallback
    if not arguments and tool_name in _SINGLE_ARG_TOOL_DEFAULTS:
        # Strip any noise, take what remains as the value
        val = raw_text.strip()
        # Strip leading JSON noise
        val = re.sub(r'^[\s,":{}]+', '', val)
        # Strip trailing noise
        val = re.sub(r'[\s"}]+$', '', val)
        # Strip XML-ish opening tags
        val = re.sub(r'^parameter=\w+>\s*', '', val)
        val = re.sub(r'^<parameter[_=]\w+>\s*', '', val)
        if val and len(val) > 2:
            arguments[_SINGLE_ARG_TOOL_DEFAULTS[tool_name]] = val

    return arguments if arguments else None


def repair_tool_use_input(
    raw_input: Any,
    input_schema: dict[str, Any] | None = None,
    tool_name: str | None = None,
) -> tuple[Any, bool]:
    """Best-effort repair of a tool_use.input payload.

    Returns ``(repaired_or_original, was_repaired)``.

    ``raw_input`` may be:
      - a dict (already parsed, happy path — no repair needed)
      - a string that's supposed to be JSON (what some models emit when
        grammar-constrained decoding partially fails)
      - anything else — passed through unchanged

    Repair only activates on strings that fail to parse as JSON.  On
    success, ``was_repaired=True`` so the caller can log + count.
    """
    # Already a dict — no repair needed
    if isinstance(raw_input, dict):
        return raw_input, False

    # Not a string and not a dict — can't repair (might be None, list, etc.)
    if not isinstance(raw_input, str):
        return raw_input, False

    # Empty string → treat as empty object, no repair counter bump
    if not raw_input.strip():
        return {}, False

    # Try strict parse first
    try:
        parsed = json.loads(raw_input)
        if isinstance(parsed, dict):
            return parsed, False
        # Parsed but isn't an object (e.g. bare string) — pass through
        return raw_input, False
    except (json.JSONDecodeError, ValueError):
        pass  # Fall through to repair

    # Strict parse failed — try json-repair
    try:
        from json_repair import repair_json
    except ImportError:
        logger.warning(
            "tool_call_repair: json-repair not installed — install with "
            "`uv pip install json-repair` to enable tool-call recovery. "
            "Passing original input through.",
        )
        return raw_input, False

    try:
        repaired_str = repair_json(raw_input, return_objects=False)
    except Exception as exc:  # noqa: BLE001 — repair must be fail-safe
        logger.debug(f"tool_call_repair: json-repair raised: {exc}")
        return raw_input, False

    # If json-repair produced something useful, try to parse it
    repaired_dict: dict[str, Any] | None = None
    if repaired_str and repaired_str not in ("{}", "[]"):
        try:
            candidate = json.loads(repaired_str)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.debug(f"tool_call_repair: repaired output still invalid: {exc}")
            candidate = None
        if isinstance(candidate, dict):
            # Passes structural validation?  Keep it.
            if _structurally_valid_against_schema(candidate, input_schema):
                repaired_dict = candidate
            else:
                logger.info(
                    "tool_call_repair: json-repair produced invalid-schema "
                    "output; trying XML-hybrid patterns.  original=%r",
                    raw_input[:120],
                )

    # Second stage: regex-based XML-in-JSON pattern recovery.  Handles
    # cases json-repair can't (the model dropped out of JSON mode and
    # emitted XML tags or free-text).  See ``_regex_recover_tool_args``.
    if repaired_dict is None:
        regex_args = _regex_recover_tool_args(raw_input, tool_name=tool_name)
        if regex_args is not None and _structurally_valid_against_schema(
            regex_args, input_schema,
        ):
            logger.warning(
                "tool_call_repair: recovered tool args via regex patterns.  "
                "tool=%r original=%r recovered=%r",
                tool_name, raw_input[:120], str(regex_args)[:120],
            )
            return regex_args, True
        if regex_args is not None:
            logger.debug(
                "tool_call_repair: regex recovery produced args but failed "
                "schema validation.  tool=%r args=%r",
                tool_name, str(regex_args)[:120],
            )

    if repaired_dict is None:
        # All recovery attempts exhausted
        return raw_input, False

    logger.warning(
        "tool_call_repair: repaired malformed tool-call JSON.  "
        "original=%r repaired=%r",
        raw_input[:120], repaired_str[:120],
    )
    return repaired_dict, True
