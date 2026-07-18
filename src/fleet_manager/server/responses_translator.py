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


def _tools_to_openai(tools: list[dict] | None) -> list[dict] | None:
    """Responses tools (flat) → OpenAI chat tools (nested under ``function``).

    Responses: ``{"type":"function","name":…,"parameters":…,"description":…}``
    OpenAI chat: ``{"type":"function","function":{"name":…,"parameters":…}}``
    The only real difference is the nesting.
    """
    if not tools:
        return None
    converted: list[dict] = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        if t.get("type") not in (None, "function"):
            # Hosted tools (web_search, file_search, mcp, …) have no local
            # equivalent — drop them rather than confusing the model.
            logger.debug(f"Responses: dropping non-function tool type={t.get('type')!r}")
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
    return converted or None


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
            args = item.get("arguments", "")
            if not isinstance(args, str):
                args = json.dumps(args)
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

        if itype == "reasoning":
            # Prior-turn model reasoning — not input for a local model.
            continue

        role = item.get("role")
        if role:
            messages.append({
                "role": role,
                "content": _text_from_content(item.get("content", "")),
            })

    return messages


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
    tools = _tools_to_openai(body.get("tools"))
    if tools:
        out["tools"] = tools
    if body.get("tool_choice") is not None:
        out["tool_choice"] = body["tool_choice"]
    if body.get("temperature") is not None:
        out["temperature"] = body["temperature"]
    # Responses names the output cap differently than chat-completions.
    if body.get("max_output_tokens") is not None:
        out["max_tokens"] = body["max_output_tokens"]
    return out


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
    for tc in tool_calls:
        fn = tc.get("function") or {}
        name = fn.get("name", "")
        output.append({
            "type": "function_call",
            "id": _new_id("fc"),
            "call_id": tc.get("id") or _new_id("call"),
            "name": name,
            "arguments": _normalize_tool_args(name, fn.get("arguments")),
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


def accumulate_responses_object(chunks: list[str], model: str) -> dict:
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
