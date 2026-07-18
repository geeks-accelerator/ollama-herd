"""OpenAI **Responses API** (`/v1/responses`) ↔ internal translation.

This is what Codex CLI speaks: OpenAI removed chat/completions support from
Codex in Feb 2026 (`wire_api="chat"` is gone), so a bridge to local models must
implement the Responses wire format, not chat-completions.

Two directions, and they are deliberately asymmetric — see
``docs/plans/codex-responses-api-support.md`` (Codebase audit):

* **Request → internal is nearly free.** Responses is OpenAI's *own* newer API,
  and our pipeline already speaks OpenAI-shaped bodies: ``routes/openai_compat``
  passes ``messages``/``tools`` straight to Ollama's ``/api/chat``, which accepts
  them natively.  So we translate Responses → an **OpenAI-chat-shaped body** and
  let the existing pipeline do the rest.  We deliberately do *not* reuse
  ``anthropic_translator``'s block-coercion — that exists for Anthropic content
  blocks, which Responses doesn't use.
* **Internal → response is the genuinely new part.** Codex expects the Responses
  SSE event sequence, which no existing translator emits.  Its *structure*
  mirrors ``AnthropicSSEState`` (route-owned, stateful, opens/closes items), but
  its *field mapping* comes from the OpenAI family.

**Statelessness.** We implement stateless chaining: the client resends the full
``input`` each turn.  ``previous_response_id`` (server-side history) would
require persisting responses and is out of scope until a real capture proves
Codex needs it.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Parameter name used when bridging a Responses `custom` tool (freeform text
# input) into an Ollama function tool (JSON-schema input). The model fills
# this string; the response side unwraps it back to a `custom_tool_call`.
CUSTOM_TOOL_ARG = "input"

# Codex instructs the model to "send a brief preamble to the user explaining what
# you're about to do" before each tool call. Frontier models satisfy that by
# emitting the preamble *and* the tool call in one response; local models often
# emit the preamble and stop. A text-only response means "turn complete" in the
# Responses protocol, so Codex renders the preamble and the run ends mid-task —
# looking like the client hung when the model simply quit.
#
# Measured on qwen3-coder:30b against Codex's real captured tool schema, 15
# trials each: 15/15 turns called a tool with no preamble instruction, 7/15 with
# it, and 15/15 with this counter-instruction appended.
#
# Appended unconditionally rather than keyed on the preamble text: the Lite
# requests (`sol`/`terra`/`luna`, the Desktop default — where this was actually
# observed) carry no `instructions` at all, so any conditional keyed on Codex's
# wording would skip the exact path that fails.
TURN_COMPLETION_GUIDANCE = (
    "\n\nIMPORTANT: a preamble is never a complete turn. If you describe an "
    "action you are about to take, emit the tool call in the SAME response. "
    "Never end your turn immediately after saying what you are about to do."
)


# ---------------------------------------------------------------------------
# Request:  Responses  →  OpenAI-chat-shaped body (which Ollama accepts)
# ---------------------------------------------------------------------------


def _text_from_content(content: Any) -> str:
    """Flatten a Responses content value to plain text.

    Content is either a string or a list of typed parts (``input_text`` /
    ``output_text`` / ``refusal``).  Unknown part types are skipped rather than
    raising — a forward-compat stance, since the API keeps gaining part types.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    out: list[str] = []
    for part in content:
        if isinstance(part, str):
            out.append(part)
        elif isinstance(part, dict) and part.get("type") in (
            "input_text", "output_text", "text", "summary_text",
        ):
            out.append(str(part.get("text", "")))
    return "".join(out)


def _tools_to_openai(
    tools: list[dict] | None, custom_names: set[str] | None = None
) -> list[dict] | None:
    """Responses tools (flat) → OpenAI chat tools (nested under ``function``).

    Responses: ``{"type":"function","name":…,"parameters":…,"description":…}``
    OpenAI chat: ``{"type":"function","function":{"name":…,"parameters":…}}``
    The only real difference is the nesting.
    """
    if not tools:
        return None
    custom_names = custom_names if custom_names is not None else set()
    nested: list[str] = []
    converted: list[dict] = []
    dropped: list[str] = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        ttype = t.get("type")

        if ttype == "custom":
            # Codex's primary tool (`exec`) is a `custom` tool: freeform text
            # input, optionally constrained by a grammar, NOT a JSON schema.
            # Ollama function-calling can't express that — but the shapes are
            # close enough to bridge: expose it as a function with a single
            # string parameter carrying the freeform text.  The model then
            # emits a normal tool call, and the response side converts it back
            # into a `custom_tool_call` item (see CUSTOM_TOOL_ARG).
            #
            # Without this the tool is dropped, the model has nothing that can
            # run a command, and it rationalises the failure ("I'm facing
            # limitations in this environment") rather than reporting it.
            name = t.get("name", "")
            desc = t.get("description", "")
            fmt = t.get("format") or {}
            arg_desc = "Raw source text for this tool."
            if fmt.get("type") == "grammar":
                # Telling the model what NOT to emit isn't enough — it needs the
                # call shape. Without an exemplar it emits `{"cmd": "…"}`, which
                # is a SyntaxError as a JS program, so `tools.exec_command` is
                # never called and any `sandbox_permissions` it set never
                # reaches Codex as an approval request.
                desc += (
                    f"\n\nProvide the raw {fmt.get('syntax', 'text')} source as the "
                    f"`{CUSTOM_TOOL_ARG}` argument — plain text only, no JSON "
                    f"wrapper and no markdown code fences. For example:\n"
                    f'  await tools.exec_command({{cmd: "git pull"}})\n'
                    f"To run a command the sandbox would otherwise block, add "
                    f'`sandbox_permissions: "require_escalated"` and a '
                    f"one-sentence `justification`."
                )
                arg_desc = (
                    f"Raw {fmt.get('syntax', 'text')} source, e.g. "
                    f'await tools.exec_command({{cmd: "git pull"}}) — not JSON.'
                )
            custom_names.add(name)
            converted.append({"type": "function", "function": {
                "name": name,
                "description": desc,
                "parameters": {
                    "type": "object",
                    "properties": {CUSTOM_TOOL_ARG: {
                        "type": "string",
                        "description": arg_desc,
                    }},
                    "required": [CUSTOM_TOOL_ARG],
                },
            }})
            continue

        if ttype == "namespace":
            # A namespace groups nested tools that are only reachable from
            # inside `exec`'s sandbox (`await tools.<name>(...)`), so they are
            # not independently callable. Recording rather than warning.
            nested.extend(
                f"{t.get('name')}.{n.get('name')}"
                for n in (t.get("tools") or []) if isinstance(n, dict)
            )
            continue

        if ttype not in (None, "function"):
            # Hosted tools (web_search, file_search, mcp) have no local
            # equivalent — genuinely nothing we can do.
            dropped.append(f"{ttype}:{t.get('name')}")
            continue
        # Already nested (defensive — some clients send the chat shape).
        if isinstance(t.get("function"), dict):
            converted.append(t)
            continue
        fn = {"name": t.get("name", "")}
        if t.get("description"):
            fn["description"] = t["description"]
        if t.get("parameters") is not None:
            fn["parameters"] = t["parameters"]
        converted.append({"type": "function", "function": fn})
    if nested:
        logger.debug(
            f"Responses: {len(nested)} namespaced tool(s) reachable only from "
            f"inside a custom tool's sandbox: {nested[:6]}"
        )
    if dropped:
        # WARNING, not debug: a silently-dropped tool is why a model with no way
        # to act still reports success. Make the capability loss visible.
        logger.warning(
            f"Responses: dropped {len(dropped)} untranslatable tool(s) "
            f"{dropped} — the model cannot call these"
        )
    return converted or None


# Item types we've already warned about, so an unknown type costs one log line
# per process rather than one per turn.
_LOGGED_UNKNOWN_ITEM_TYPES: set[str] = set()
_LOGGED_JSON_CALL_REPAIRS: set[str] = set()


def _log_unknown_item_type_once(itype: str) -> None:
    if itype in _LOGGED_UNKNOWN_ITEM_TYPES:
        return
    _LOGGED_UNKNOWN_ITEM_TYPES.add(itype)
    logger.warning(
        f"Responses: dropping unrecognised input item type {itype!r} — the "
        f"model will not see it. If Codex behaviour looks broken around this "
        f"item, this is the first thing to check."
    )


def responses_input_to_messages(
    input_value: Any, instructions: str | None = None
) -> list[dict]:
    """``input`` (+ ``instructions``) → OpenAI-chat ``messages``.

    Handles the item kinds Codex actually sends in a stateless conversation:

    * plain string → a single user message
    * ``{"role","content"}`` → a message (content flattened to text)
    * ``{"type":"function_call", call_id, name, arguments}`` → an assistant
      message carrying ``tool_calls`` (the model's prior tool request)
    * ``{"type":"function_call_output", call_id, output}`` → a ``tool`` message
      (the result we fed back), threaded by ``tool_call_id``
    """
    messages: list[dict] = []
    if instructions:
        messages.append({"role": "system", "content": instructions})

    if isinstance(input_value, str):
        if input_value:
            messages.append({"role": "user", "content": input_value})
        return messages

    if not isinstance(input_value, list):
        return messages

    for item in input_value:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            continue

        itype = item.get("type")

        if itype == "function_call":
            # Ollama's chat templates expect `arguments` as an OBJECT, while the
            # Responses/OpenAI wire format carries it as a JSON *string*.  Passing
            # the string through makes Ollama 400 with "Value looks like object,
            # but can't find closing '}' symbol", which breaks the *second* turn
            # of every agentic session — the first tool call runs, then the
            # follow-up carrying its result dies (2026-07-18).
            args = item.get("arguments", "")
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args.strip() else {}
                except (json.JSONDecodeError, TypeError):
                    # Not JSON — hand it over as-is rather than dropping the call.
                    logger.warning(
                        f"Responses: tool-call arguments for "
                        f"{item.get('name')!r} are not valid JSON; passing raw"
                    )
            messages.append({
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": item.get("call_id") or item.get("id") or "",
                    "type": "function",
                    "function": {"name": item.get("name", ""), "arguments": args},
                }],
            })
            continue

        if itype == "function_call_output":
            output = item.get("output", "")
            if not isinstance(output, str):
                output = json.dumps(output)
            messages.append({
                "role": "tool",
                "tool_call_id": item.get("call_id") or item.get("id") or "",
                "content": output,
            })
            continue

        if itype == "custom_tool_call":
            # Our own prior custom call, echoed back in history.
            messages.append({
                "role": "assistant", "content": "",
                "tool_calls": [{
                    "id": item.get("call_id") or item.get("id") or "",
                    "type": "function",
                    "function": {"name": item.get("name", ""),
                                 "arguments": {CUSTOM_TOOL_ARG: item.get("input", "")}},
                }],
            })
            continue

        if itype == "custom_tool_call_output":
            messages.append({
                "role": "tool",
                "tool_call_id": item.get("call_id") or item.get("id") or "",
                "content": item.get("output", "") if isinstance(item.get("output"), str)
                           else json.dumps(item.get("output")),
            })
            continue

        if itype == "reasoning":
            # Prior-turn model reasoning — not input for a local model.
            continue

        if itype == "additional_tools":
            # Not a message — Codex smuggles its tool catalogue in here rather
            # than the top-level `tools` field.  Extracted by
            # ``collect_tools_from_input``; skip it as conversation content.
            continue

        role = item.get("role")
        if role:
            messages.append({
                "role": role,
                "content": _text_from_content(item.get("content", "")),
            })
        elif itype:
            # An input item we don't understand, with no role to fall back on —
            # so it is dropped and the model never sees it.  Silent drops are
            # how three separate tool bugs hid today, so make this one audible.
            #
            # NOT the approval flow — that guess (commit d13c18d) was wrong.
            # Approvals are model-initiated through tool *arguments*
            # (`sandbox_permissions`); the Responses API has no approval item
            # type at all, only `mcp_approval_request`/`_response` for remote
            # MCP. See docs/plans/codex-code-mode-escalation.md.
            #
            # Still worth watching: `ResponseInputItemParam` has 32 members and
            # we handle ~9, including first-class `shell_call` /
            # `apply_patch_call` surfaces. No capture shows Codex 0.145 sending
            # them, but the captures are all short and first-turn.
            _log_unknown_item_type_once(itype)

    return messages


def collect_tools_from_input(input_value: Any) -> list[dict]:
    """Pull tool definitions out of ``additional_tools`` items in ``input``.

    **Discovered by capturing a real Codex CLI request (2026-07-18).** Codex
    does not use the documented top-level ``tools`` field at all — it sends
    ``{"type":"additional_tools","role":"developer","tools":[…]}`` as an *input
    item*.  Before this, the whole catalogue was silently dropped, so the model
    received no tools, could not act, and (worse) confidently narrated work it
    had never done.

    Returns the raw tool dicts; ``_tools_to_openai`` decides which survive
    translation.
    """
    if not isinstance(input_value, list):
        return []
    found: list[dict] = []
    for item in input_value:
        if isinstance(item, dict) and item.get("type") == "additional_tools":
            for t in item.get("tools") or []:
                if isinstance(t, dict):
                    found.append(t)
    return found


def _append_turn_completion_guidance(messages: list[dict]) -> None:
    """Append the preamble counter-instruction to the system message in place.

    Extends an existing system message rather than adding a second one, so the
    message sequence a model sees is unchanged in shape.
    """
    for m in messages:
        if m.get("role") == "system":
            m["content"] = f"{m.get('content', '')}{TURN_COMPLETION_GUIDANCE}"
            return
    messages.insert(
        0, {"role": "system", "content": TURN_COMPLETION_GUIDANCE.strip()}
    )


def responses_to_openai_body(body: dict) -> dict:
    """A Responses request → an OpenAI-chat-shaped body for the pipeline.

    The output is what ``routes/openai_compat`` would have produced, so the
    scorer / queue / streaming path treats it identically to a
    ``/v1/chat/completions`` call.
    """
    messages = responses_input_to_messages(
        body.get("input"), body.get("instructions")
    )
    out: dict[str, Any] = {
        "model": body.get("model", ""),
        "messages": messages,
        "stream": bool(body.get("stream", False)),
    }
    # Tools may arrive top-level (per the docs) or inside an `additional_tools`
    # input item (what Codex actually does). Take both.
    raw_tools = list(body.get("tools") or []) + collect_tools_from_input(
        body.get("input")
    )
    # `custom_names` records which tools arrived as Responses `custom` tools so
    # the response side can emit `custom_tool_call` for them instead of
    # `function_call` — Codex rejects the wrong item type.
    custom_names: set[str] = set()
    tools = _tools_to_openai(raw_tools, custom_names)
    out_custom = custom_names
    if tools:
        out["tools"] = tools
        # Only meaningful when the model has something to call — a plain chat
        # turn has no tool call to withhold, so the guidance would be noise.
        _append_turn_completion_guidance(messages)
    if body.get("tool_choice") is not None:
        out["tool_choice"] = body["tool_choice"]
    if body.get("temperature") is not None:
        out["temperature"] = body["temperature"]
    # Responses names the output cap differently than chat-completions.
    if body.get("max_output_tokens") is not None:
        out["max_tokens"] = body["max_output_tokens"]
    if out_custom:
        # Consumed by the route; stripped before the body reaches Ollama.
        out["_custom_tool_names"] = sorted(out_custom)
    if tools:
        # Also consumed by the route. Needed to spot a call to a tool we never
        # offered — see `redirect_nested_tool_call`.
        out["_known_tool_names"] = sorted(t["function"]["name"] for t in tools)
    return out


def _patch_text_from_args(parsed: dict) -> str | None:
    """Pull the patch body out of whatever shape the model wrapped it in.

    Codex documents the call as ``{"command": ["apply_patch", "*** Begin
    Patch…"]}``, but models paraphrase — the body turns up under `input`,
    `patch`, or `content` too. Anything that doesn't carry a recognisable patch
    envelope is left alone rather than guessed at.
    """
    for key in ("input", "patch", "content", "text"):
        v = parsed.get(key)
        if isinstance(v, str) and "*** Begin Patch" in v:
            return v
    cmd = parsed.get("command")
    if isinstance(cmd, list):
        for part in cmd:
            if isinstance(part, str) and "*** Begin Patch" in part:
                return part
    if isinstance(cmd, str) and "*** Begin Patch" in cmd:
        return cmd
    return None


def redirect_apply_patch_call(
    name: str, args: str, known_names: set[str]
) -> tuple[str, str] | None:
    """An `apply_patch` tool call → the `exec_command` call that actually runs it.

    `apply_patch` is not a tool. It is a **binary** Codex injects at the front of
    the sandbox PATH (verified live: `/Users/…/.codex/tmp/arg0/codex-arg0…/apply_patch`),
    and Codex's own instructions describe invoking it as an argv array —
    ``{"command": ["apply_patch", "*** Begin Patch…"]}``. Models read
    "Use the `apply_patch` tool to edit files" and call it as a top-level tool,
    which Codex's router rejects outright:

        ERROR codex_core::tools::router: error=unsupported call: apply_patch

    The run then reads and executes fine but can never write, so an agentic
    coding session explores forever and edits nothing.

    Rewritten as a heredoc so patch bodies containing quotes, backslashes, or
    ``$`` survive the shell verbatim. Returns ``None`` — leaving the call
    untouched — when `apply_patch` really was offered as a tool, when there is no
    `exec_command` to route through, or when no patch envelope is recognisable.
    """
    if name != "apply_patch" or "apply_patch" in known_names:
        return None
    if "exec_command" not in known_names:
        return None
    try:
        parsed = json.loads(args) if args else {}
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    patch = _patch_text_from_args(parsed)
    if not patch:
        return None
    logger.warning(
        "Responses: model called 'apply_patch' as a tool — it is a binary on the "
        "sandbox PATH, not a tool, and Codex rejects the call outright. Rewriting "
        "as an `exec_command` heredoc so the edit actually lands."
    )
    cmd = f"apply_patch <<'CODEX_PATCH_EOF'\n{patch}\nCODEX_PATCH_EOF"
    return "exec_command", json.dumps({"cmd": cmd})


def redirect_nested_tool_call(
    name: str,
    args: str,
    custom_names: set[str],
    known_names: set[str],
) -> tuple[str, str] | None:
    """A call to a *nested* code-mode tool → the code-mode tool that hosts it.

    In code mode the only callable tool is `exec`; everything else
    (`exec_command`, `apply_patch`, …) lives behind `await tools.<name>(…)`
    inside the JavaScript it evaluates. Models read the 14 KB `exec` description
    — which is dense with `tools.exec_command(...)` — and call that name
    directly as a top-level function. Measured on gpt-oss:120b: 4/4 calls went
    to `exec_command`, a tool that was never offered, with and without our own
    guidance text, so this is the model reading Codex's description rather than
    anything we added.

    Passing it through is what stalls the loop: Codex gets a `function_call` for
    a tool it doesn't have, cannot execute it, and the run stops with no error.
    The intent is unambiguous, so express it the way the protocol allows.

    Returns ``(code_mode_tool_name, javascript)`` or ``None`` when the call is a
    legitimately offered tool and should be left alone.
    """
    # An empty `known_names` means "we don't know what was offered", not
    # "nothing was offered" — never redirect on absence of information.
    if not known_names or not custom_names or name in known_names:
        return None
    host = sorted(custom_names)[0]
    try:
        parsed = json.loads(args) if args else {}
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    logger.warning(
        f"Responses: model called {name!r}, which was never offered — it is a "
        f"nested tool of the code-mode {host!r} tool. Rewriting as "
        f"`await tools.{name}(…)` so Codex can run it instead of stalling."
    )
    return host, f"await tools.{name}({json.dumps(parsed)})"


# ---------------------------------------------------------------------------
# Response:  Ollama  →  Responses wire format
# ---------------------------------------------------------------------------


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def _map_status(done_reason: str) -> str:
    """Ollama ``done_reason`` → Responses ``status``."""
    return "incomplete" if done_reason == "length" else "completed"


def _normalize_tool_args(name: str, args: Any) -> str:
    """Tool arguments as the JSON *string* the Responses API expects.

    Ollama hands back a dict (usually) or a JSON string (some models).  Mirrors
    the defensive parsing in ``anthropic_translator`` — a non-JSON string is
    wrapped rather than dropped, so a misbehaving model degrades instead of
    breaking the stream.
    """
    if isinstance(args, str):
        try:
            json.loads(args)
            return args  # already valid JSON text
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                f"Responses: tool args for {name!r} were a non-JSON string "
                f"({len(args)} chars) — wrapping in _raw"
            )
            return json.dumps({"_raw": args})
    try:
        return json.dumps(args if args is not None else {})
    except (TypeError, ValueError):
        return "{}"


def _log_json_call_repair_once(name: str) -> None:
    if name in _LOGGED_JSON_CALL_REPAIRS:
        return
    _LOGGED_JSON_CALL_REPAIRS.add(name)
    logger.warning(
        f"Responses: model emitted a JSON object instead of {name!r} source — "
        f"rewriting it as an `await tools.exec_command({{…}})` call. Codex's "
        f"code-mode tool takes raw JavaScript, so the JSON form is a "
        f"SyntaxError and any escalation it requested would be lost."
    )


def _repair_if_json_exec(text: str, name: str) -> str:
    """A JSON ``exec_command`` payload → the JavaScript call Codex expects.

    Codex's code-mode `exec` tool takes *raw JavaScript* ("not JSON, quoted
    strings, or markdown code fences") and its Lark grammar — ``SOURCE:
    /[\\s\\S]+/`` — constrains nothing, so a JSON object sails through and then
    fails as a JS program. Local models emit that form often enough to matter,
    and the cost is silent: `tools.exec_command` is never invoked, so a
    ``sandbox_permissions: "require_escalated"`` the model *did* set never
    becomes an approval request and the user just sees a sandbox failure.

    Sibling keys are preserved verbatim rather than allow-listed — which keys
    `exec_command` accepts is Codex's business, not ours. Anything that isn't a
    JSON object with a ``cmd`` key is returned untouched, so valid JavaScript
    never round-trips through here.
    """
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text
    if not isinstance(parsed, dict) or "cmd" not in parsed:
        return text
    _log_json_call_repair_once(name)
    # `cmd` is a string in Codex's schema ("Shell command to execute.") — pass
    # it through as-is. `prefix_rule` is the array-typed field, not this one.
    return f"await tools.exec_command({json.dumps(parsed)})"


def _unwrap_custom_input(args: str, name: str = "exec") -> str:
    """Recover a custom tool's freeform text from our bridge parameter.

    The model was handed a one-string-property function, so it returns
    ``{"input": "<text>"}``. Codex wants just ``<text>``. Falls back to the raw
    string if the model ignored the schema and emitted bare text.

    Whichever path produced the text, it gets one repair pass — the model may
    put a JSON payload *inside* the bridge parameter (the common case) or skip
    the parameter and emit the payload as the whole argument object.
    """
    try:
        parsed = json.loads(args)
    except (json.JSONDecodeError, TypeError):
        return _repair_if_json_exec(args, name)
    if isinstance(parsed, dict):
        if CUSTOM_TOOL_ARG in parsed:
            return _repair_if_json_exec(str(parsed[CUSTOM_TOOL_ARG]), name)
        # Ahead of the single-key fallback: `{"cmd": "git pull"}` is one key, and
        # unwrapping it would yield the bare string `git pull` — not JavaScript
        # either, but plausible enough to fail unnoticed.
        if "cmd" in parsed:
            return _repair_if_json_exec(args, name)
        if len(parsed) == 1:
            return _repair_if_json_exec(str(next(iter(parsed.values()))), name)
    return args


def _parse_ollama_chunk(line: str) -> dict | None:
    line = (line or "").strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        logger.warning(f"Responses translator: malformed Ollama JSON: {line[:200]}")
        return None


def _chunk_parts(chunk: dict) -> tuple[str, list[dict], bool]:
    """Pull (text, tool_calls, done) out of an Ollama chunk."""
    msg = chunk.get("message") or {}
    text = msg.get("content", "") or chunk.get("response", "") or ""
    tool_calls = msg.get("tool_calls") or []
    return text, tool_calls, bool(chunk.get("done"))


def build_responses_object(
    *,
    model: str,
    text: str,
    tool_calls: list[dict],
    custom_names: set[str] | None = None,
    known_names: set[str] | None = None,
    input_tokens: int,
    output_tokens: int,
    done_reason: str = "",
    response_id: str | None = None,
    created_at: float | None = None,
) -> dict:
    """The non-streaming ``{object:"response", output:[…], usage}`` payload.

    ``output`` is a list of *items*: an assistant ``message`` (when there's
    text) followed by one top-level ``function_call`` item per tool call.
    Function calls are siblings of the message, not nested content parts.
    """
    output: list[dict] = []
    if text:
        output.append({
            "type": "message",
            "id": _new_id("msg"),
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        })
    custom_names = custom_names or set()
    known_names = known_names or set()
    for tc in tool_calls:
        fn = tc.get("function") or {}
        name = fn.get("name", "")
        args = _normalize_tool_args(name, fn.get("arguments"))
        patch_call = redirect_apply_patch_call(name, args, known_names)
        if patch_call:
            name, args = patch_call
            output.append({
                "type": "function_call",
                "id": _new_id("fc"),
                "call_id": tc.get("id") or _new_id("call"),
                "name": name,
                "arguments": args,
                "status": "completed",
            })
            continue
        redirect = redirect_nested_tool_call(name, args, custom_names, known_names)
        if redirect:
            host, js = redirect
            output.append({
                "type": "custom_tool_call",
                "id": _new_id("ctc"),
                "call_id": tc.get("id") or _new_id("call"),
                "name": host,
                "input": js,
                "status": "completed",
            })
            continue
        if name in custom_names:
            output.append({
                "type": "custom_tool_call",
                "id": _new_id("ctc"),
                "call_id": tc.get("id") or _new_id("call"),
                "name": name,
                "input": _unwrap_custom_input(args, name),
                "status": "completed",
            })
            continue
        output.append({
            "type": "function_call",
            "id": _new_id("fc"),
            "call_id": tc.get("id") or _new_id("call"),
            "name": name,
            "arguments": args,
            "status": "completed",
        })
    return {
        "id": response_id or _new_id("resp"),
        "object": "response",
        "created_at": int(created_at or time.time()),
        "status": _map_status(done_reason),
        "model": model,
        "output": output,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
        "error": None,
    }


def accumulate_responses_object(
    chunks: list[str], model: str, custom_names: set[str] | None = None,
    known_names: set[str] | None = None,
) -> dict:
    """Collapse a full Ollama NDJSON stream into one Responses object."""
    text_parts: list[str] = []
    tool_calls: list[dict] = []
    input_tokens = output_tokens = 0
    done_reason = ""
    for line in chunks:
        chunk = _parse_ollama_chunk(line)
        if chunk is None:
            continue
        text, tcs, done = _chunk_parts(chunk)
        if text:
            text_parts.append(text)
        if tcs:
            tool_calls.extend(tcs)
        if done:
            input_tokens = chunk.get("prompt_eval_count") or input_tokens or 0
            output_tokens = chunk.get("eval_count") or output_tokens or 0
            done_reason = chunk.get("done_reason", "") or ""
    return build_responses_object(
        model=model,
        text="".join(text_parts),
        tool_calls=tool_calls,
        custom_names=custom_names,
        known_names=known_names,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        done_reason=done_reason,
    )


@dataclass
class ResponsesSSEState:
    """Mutable state for one streamed Responses turn.

    Tracks the item lifecycle Codex expects: a message item opened on first
    text and closed before any tool call, then one ``function_call`` item per
    call, then ``response.completed``.  ``sequence_number`` is monotonic across
    every event of the response.
    """

    response_id: str = field(default_factory=lambda: _new_id("resp"))
    model: str = ""
    created_at: int = field(default_factory=lambda: int(time.time()))
    seq: int = 0
    started: bool = False
    next_output_index: int = 0
    # open message item
    msg_id: str | None = None
    msg_index: int | None = None
    text_parts: list[str] = field(default_factory=list)
    # completed items, for the final response.completed payload
    emitted_tools: list[dict] = field(default_factory=list)
    custom_names: set[str] = field(default_factory=set)
    known_names: set[str] = field(default_factory=set)
    input_tokens: int = 0
    output_tokens: int = 0
    finished: bool = False

    def _next_seq(self) -> int:
        self.seq += 1
        return self.seq


def _ev(state: ResponsesSSEState, etype: str, payload: dict) -> str:
    """Format one Responses SSE event (`event:` + `data:` with a `type`)."""
    data = {"type": etype, "sequence_number": state._next_seq(), **payload}
    return f"event: {etype}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


def _response_envelope(state: ResponsesSSEState, status: str, output: list[dict]) -> dict:
    return {
        "id": state.response_id,
        "object": "response",
        "created_at": state.created_at,
        "status": status,
        "model": state.model,
        "output": output,
        "usage": {
            "input_tokens": state.input_tokens,
            "output_tokens": state.output_tokens,
            "total_tokens": state.input_tokens + state.output_tokens,
        },
        "error": None,
    }


def _close_message_item(state: ResponsesSSEState) -> Iterator[str]:
    """Emit output_text.done + output_item.done for the open message item."""
    if state.msg_id is None:
        return
    text = "".join(state.text_parts)
    yield _ev(state, "response.output_text.done", {
        "item_id": state.msg_id,
        "output_index": state.msg_index,
        "content_index": 0,
        "text": text,
    })
    yield _ev(state, "response.output_item.done", {
        "output_index": state.msg_index,
        "item": {
            "type": "message",
            "id": state.msg_id,
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        },
    })
    state.msg_id = None
    state.msg_index = None


def ollama_chunk_to_responses_events(
    line: str, state: ResponsesSSEState
) -> Iterator[str]:
    """Translate one Ollama NDJSON line into zero-or-more Responses SSE events."""
    chunk = _parse_ollama_chunk(line)
    if chunk is None:
        return
    text, tool_calls, done = _chunk_parts(chunk)

    # response.created + response.in_progress, once
    if not state.started:
        state.started = True
        yield _ev(state, "response.created",
                  {"response": _response_envelope(state, "in_progress", [])})
        yield _ev(state, "response.in_progress",
                  {"response": _response_envelope(state, "in_progress", [])})

    # Text → open a message item (once), then stream deltas
    if text:
        if state.msg_id is None:
            state.msg_id = _new_id("msg")
            state.msg_index = state.next_output_index
            state.next_output_index += 1
            yield _ev(state, "response.output_item.added", {
                "output_index": state.msg_index,
                "item": {
                    "type": "message",
                    "id": state.msg_id,
                    "role": "assistant",
                    "status": "in_progress",
                    "content": [],
                },
            })
            yield _ev(state, "response.content_part.added", {
                "item_id": state.msg_id,
                "output_index": state.msg_index,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            })
        state.text_parts.append(text)
        yield _ev(state, "response.output_text.delta", {
            "item_id": state.msg_id,
            "output_index": state.msg_index,
            "content_index": 0,
            "delta": text,
        })

    # Tool calls → close the message item first, then one function_call item each.
    # Ollama hands us complete arguments (it doesn't stream them), so we emit a
    # single arguments delta followed by .done — same shape Codex sees from
    # OpenAI, just not incrementally.
    if tool_calls:
        yield from _close_message_item(state)
        for tc in tool_calls:
            fn = tc.get("function") or {}
            name = fn.get("name", "")
            args = _normalize_tool_args(name, fn.get("arguments"))
            item_id = _new_id("fc")
            call_id = tc.get("id") or _new_id("call")
            idx = state.next_output_index
            state.next_output_index += 1
            patch_call = redirect_apply_patch_call(name, args, state.known_names)
            if patch_call:
                name, args = patch_call
            redirect = redirect_nested_tool_call(
                name, args, state.custom_names, state.known_names
            )
            if redirect or name in state.custom_names:
                # This began life as a Responses `custom` tool (or is a nested
                # tool of one that the model called directly). Codex expects a
                # `custom_tool_call` carrying freeform text — NOT a
                # `function_call` with JSON arguments — and a different event
                # family. Unwrap our bridge parameter back to the raw string.
                if redirect:
                    name, text = redirect
                else:
                    text = _unwrap_custom_input(args, name)
                item = {
                    "type": "custom_tool_call",
                    "id": item_id.replace("fc_", "ctc_", 1),
                    "call_id": call_id,
                    "name": name,
                    "input": text,
                    "status": "completed",
                }
                state.emitted_tools.append(item)
                yield _ev(state, "response.output_item.added", {
                    "output_index": idx,
                    "item": {**item, "input": "", "status": "in_progress"},
                })
                yield _ev(state, "response.custom_tool_call_input.delta", {
                    "item_id": item["id"], "output_index": idx, "delta": text,
                })
                yield _ev(state, "response.custom_tool_call_input.done", {
                    "item_id": item["id"], "output_index": idx, "input": text,
                })
                yield _ev(state, "response.output_item.done", {
                    "output_index": idx, "item": item,
                })
                continue
            item = {
                "type": "function_call",
                "id": item_id,
                "call_id": call_id,
                "name": name,
                "arguments": args,
                "status": "completed",
            }
            state.emitted_tools.append(item)
            yield _ev(state, "response.output_item.added", {
                "output_index": idx,
                "item": {**item, "arguments": "", "status": "in_progress"},
            })
            yield _ev(state, "response.function_call_arguments.delta", {
                "item_id": item_id,
                "output_index": idx,
                "delta": args,
            })
            yield _ev(state, "response.function_call_arguments.done", {
                "item_id": item_id,
                "output_index": idx,
                "name": name,
                "arguments": args,
            })
            yield _ev(state, "response.output_item.done", {
                "output_index": idx,
                "item": item,
            })

    if done and not state.finished:
        state.finished = True
        state.input_tokens = chunk.get("prompt_eval_count") or state.input_tokens or 0
        state.output_tokens = chunk.get("eval_count") or state.output_tokens or 0
        done_reason = chunk.get("done_reason", "") or ""

        # Rebuild the final output[] before closing (the message item's text is
        # only complete now).
        final_output: list[dict] = []
        msg_text = "".join(state.text_parts)
        if state.msg_id is not None:
            msg_id, msg_index = state.msg_id, state.msg_index
            yield from _close_message_item(state)
            final_output.append({
                "type": "message",
                "id": msg_id,
                "role": "assistant",
                "status": "completed",
                "content": [
                    {"type": "output_text", "text": msg_text, "annotations": []}
                ],
            })
            _ = msg_index
        elif msg_text:
            final_output.append({
                "type": "message",
                "id": _new_id("msg"),
                "role": "assistant",
                "status": "completed",
                "content": [
                    {"type": "output_text", "text": msg_text, "annotations": []}
                ],
            })
        final_output.extend(state.emitted_tools)

        yield _ev(state, "response.completed", {
            "response": _response_envelope(
                state, _map_status(done_reason), final_output
            ),
        })
