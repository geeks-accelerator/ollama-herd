"""Anthropic Messages API ↔ Ollama format translation.

Two directions:

1. **Request translation** (`anthropic_to_ollama_messages`, `anthropic_tools_to_ollama`)
   — flatten Anthropic content blocks (text/image/tool_use/tool_result) into
   Ollama's `messages` shape (string `content` + `images[]` + `tool_calls[]` +
   `role: "tool"` for results), and rename Anthropic `input_schema` → Ollama
   `parameters`.

2. **Response translation** (`AnthropicSSEState`, `ollama_chunk_to_anthropic_events`,
   `accumulate_anthropic_response`) — consume Ollama's NDJSON stream and emit
   Anthropic SSE event sequences (`message_start` → `content_block_*` →
   `message_delta` → `message_stop`) or accumulate into a single
   `AnthropicMessageResponse`.

The translator is pure (no I/O) so it's trivial to unit-test against canned
NDJSON traces.
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


# --- Request: Anthropic → Ollama --------------------------------------------


def anthropic_system_to_text(system: str | list[dict[str, Any]] | None) -> str:
    """Flatten Anthropic system prompt (string or text-block array) to a string."""
    if not system:
        return ""
    if isinstance(system, str):
        return system
    parts = []
    for block in system:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(p for p in parts if p)


def _coerce_blocks(content: Any) -> list[dict[str, Any]]:
    """Normalize content into a list of dict blocks.

    Anthropic accepts `content` as either a plain string or an array of typed
    blocks. We convert strings to a single text block so downstream code only
    needs to handle the list case.
    """
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def anthropic_to_ollama_messages(
    messages: list[dict[str, Any]],
    system: str = "",
) -> list[dict[str, Any]]:
    """Translate Anthropic messages array → Ollama messages array.

    - Prepends system prompt as a `{role: "system"}` message if provided.
    - For each Anthropic message, walks its content blocks and emits Ollama-
      shaped messages: text concatenates into `content`, images go into
      `images: []`, `tool_use` blocks become `tool_calls: []` on the assistant
      message, and `tool_result` blocks each become a separate `role: "tool"`
      message in their original order.
    - `thinking` blocks are dropped — we don't replay reasoning back to the model.
    """
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})

    for msg in messages:
        role = msg.get("role", "user")
        blocks = _coerce_blocks(msg.get("content"))

        text_parts: list[str] = []
        images: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        # tool_results emit *their own* role:"tool" messages, in order
        pending_tool_results: list[dict[str, Any]] = []

        for block in blocks:
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "image":
                source = block.get("source") or {}
                if source.get("type") == "base64" and source.get("data"):
                    images.append(source["data"])
                # url-type images are not supported by Ollama — skip silently
            elif btype == "tool_use":
                # Assistant turn calling a tool
                tool_calls.append({
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": block.get("input", {}) or {},
                    }
                })
            elif btype == "tool_result":
                # User turn returning a tool's output — flatten content
                content = block.get("content", "")
                if isinstance(content, list):
                    flat = []
                    for sub in content:
                        if isinstance(sub, dict) and sub.get("type") == "text":
                            flat.append(sub.get("text", ""))
                    content = "\n".join(flat)
                pending_tool_results.append({
                    "role": "tool",
                    "content": str(content) if content is not None else "",
                })
            elif btype == "thinking":
                # Drop — don't replay reasoning to non-thinking models
                continue

        # Build the primary message for this turn (if any non-tool-result content)
        if text_parts or images or tool_calls:
            primary: dict[str, Any] = {
                "role": role,
                "content": "\n".join(t for t in text_parts if t),
            }
            if images:
                primary["images"] = images
            if tool_calls:
                primary["tool_calls"] = tool_calls
            out.append(primary)

        # Tool results follow the assistant's tool_use turn; emit each as its
        # own role:"tool" message preserving order.
        out.extend(pending_tool_results)

    return out


def anthropic_tool_to_ollama(tool: dict[str, Any]) -> dict[str, Any]:
    """Translate one Anthropic tool definition to Ollama's function-tool shape."""
    return {
        "type": "function",
        "function": {
            "name": tool.get("name", ""),
            "description": tool.get("description", "") or "",
            "parameters": tool.get("input_schema", {}) or {"type": "object", "properties": {}},
        },
    }


def anthropic_tools_to_ollama(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    return [anthropic_tool_to_ollama(t) for t in tools]


def apply_tool_choice(
    tools: list[dict[str, Any]] | None,
    choice: dict[str, Any] | None,
    system_prompt: str,
) -> tuple[list[dict[str, Any]] | None, str]:
    """Apply Anthropic tool_choice semantics to the Ollama request.

    Ollama doesn't natively support `tool_choice` forcing, so we approximate:
    - `auto` (default): pass tools through as-is
    - `none`: strip tools entirely
    - `any`: pass tools, append a system instruction that the model MUST call one
    - `tool` (with name): pass tools, append instruction to call that specific tool

    Returns (possibly modified tools list, possibly extended system prompt).
    """
    if not choice or not tools:
        return tools, system_prompt

    ctype = choice.get("type", "auto")
    if ctype == "none":
        return None, system_prompt
    if ctype == "auto":
        return tools, system_prompt
    if ctype == "any":
        addon = "\n\nYou MUST respond by calling one of the provided tools."
        return tools, (system_prompt + addon).strip()
    if ctype == "tool":
        name = choice.get("name", "")
        if name:
            addon = f"\n\nYou MUST respond by calling the tool named `{name}`."
            return tools, (system_prompt + addon).strip()
    return tools, system_prompt


# --- Response: Ollama NDJSON → Anthropic SSE --------------------------------


@dataclass
class AnthropicSSEState:
    """Mutable state for the streaming translator.

    A single instance is shared across all chunks of one response. The
    translator only emits one block at a time (text or tool_use), opening and
    closing them as needed.
    """

    message_id: str = field(default_factory=lambda: f"msg_{uuid.uuid4().hex[:24]}")
    model: str = ""
    started: bool = False
    text_open: bool = False
    text_block_index: int | None = None
    next_block_index: int = 0
    emitted_tools: list[dict[str, Any]] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str | None = None
    stop_sequence: str | None = None
    finished: bool = False


def _sse(event: str, data: dict[str, Any]) -> str:
    """Format one SSE event with `event:` and `data:` lines."""
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


def _map_done_reason(done_reason: str, has_tool_calls: bool, stop_seq: str | None) -> str:
    """Map Ollama's done_reason → Anthropic's stop_reason."""
    if has_tool_calls:
        return "tool_use"
    if done_reason == "length":
        return "max_tokens"
    if stop_seq:
        return "stop_sequence"
    return "end_turn"


def ollama_chunk_to_anthropic_events(
    line: str, state: AnthropicSSEState, stop_sequences: list[str] | None = None,
) -> Iterator[str]:
    """Translate one Ollama NDJSON line into zero-or-more Anthropic SSE events."""
    line = line.strip()
    if not line:
        return
    try:
        chunk = json.loads(line)
    except json.JSONDecodeError:
        logger.warning(f"Anthropic translator: malformed Ollama JSON: {line[:200]}")
        return

    msg = chunk.get("message") or {}
    content = msg.get("content", "") or chunk.get("response", "") or ""
    tool_calls = msg.get("tool_calls") or []
    done = bool(chunk.get("done"))

    # Emit message_start once, on the first chunk we see
    if not state.started:
        state.started = True
        yield _sse("message_start", {
            "type": "message_start",
            "message": {
                "id": state.message_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": state.model,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": state.input_tokens or 0, "output_tokens": 0},
            },
        })

    # Stream text deltas
    if content:
        if not state.text_open:
            state.text_block_index = state.next_block_index
            state.next_block_index += 1
            state.text_open = True
            yield _sse("content_block_start", {
                "type": "content_block_start",
                "index": state.text_block_index,
                "content_block": {"type": "text", "text": ""},
            })
        yield _sse("content_block_delta", {
            "type": "content_block_delta",
            "index": state.text_block_index,
            "delta": {"type": "text_delta", "text": content},
        })

    # Emit any new tool calls as content_block_start + input_json_delta + stop
    if tool_calls:
        # Close text block first if open
        if state.text_open:
            yield _sse("content_block_stop", {
                "type": "content_block_stop",
                "index": state.text_block_index,
            })
            state.text_open = False
        for tc in tool_calls:
            fn = tc.get("function") or {}
            name = fn.get("name", "")
            args = fn.get("arguments", {})
            if isinstance(args, str):
                # Some models return args as a JSON string — parse defensively
                raw_args = args
                try:
                    args = json.loads(args)
                    logger.debug(
                        f"Anthropic translator: string-encoded tool args parsed for "
                        f"tool={name!r} (model returned JSON-as-string)"
                    )
                except (json.JSONDecodeError, TypeError):
                    logger.warning(
                        f"Anthropic translator: tool args for {name!r} were a "
                        f"non-JSON string ({len(raw_args)} chars) — wrapping in _raw"
                    )
                    args = {"_raw": raw_args}
            tool_id = f"toolu_{uuid.uuid4().hex[:24]}"
            idx = state.next_block_index
            state.next_block_index += 1
            state.emitted_tools.append({"id": tool_id, "name": name, "input": args})
            yield _sse("content_block_start", {
                "type": "content_block_start",
                "index": idx,
                "content_block": {"type": "tool_use", "id": tool_id, "name": name, "input": {}},
            })
            yield _sse("content_block_delta", {
                "type": "content_block_delta",
                "index": idx,
                "delta": {"type": "input_json_delta", "partial_json": json.dumps(args)},
            })
            yield _sse("content_block_stop", {
                "type": "content_block_stop",
                "index": idx,
            })

    if done and not state.finished:
        state.finished = True
        # Close any open text block
        if state.text_open:
            yield _sse("content_block_stop", {
                "type": "content_block_stop",
                "index": state.text_block_index,
            })
            state.text_open = False

        # Token counts come from the final chunk
        prompt_tok = chunk.get("prompt_eval_count") or state.input_tokens or 0
        completion_tok = chunk.get("eval_count") or state.output_tokens or 0
        state.input_tokens = prompt_tok
        state.output_tokens = completion_tok

        # Stop reason
        done_reason = chunk.get("done_reason", "") or ""
        has_tools = bool(state.emitted_tools)
        # Check if the accumulated text ended with a stop sequence
        matched_stop = None
        if stop_sequences:
            # We don't have the full text accumulated in state for streaming — best
            # effort: only set if Ollama itself reported length-stop with no tools
            pass
        state.stop_reason = _map_done_reason(done_reason, has_tools, matched_stop)

        yield _sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": state.stop_reason, "stop_sequence": matched_stop},
            "usage": {"output_tokens": completion_tok},
        })
        yield _sse("message_stop", {"type": "message_stop"})


def accumulate_anthropic_response(
    chunks: list[str], model: str, stop_sequences: list[str] | None = None,
) -> dict[str, Any]:
    """Walk a finished list of Ollama NDJSON lines, return Anthropic JSON response.

    Used for the non-streaming code path: collect every chunk first, then
    build the final response in one shot. Mirrors the SSE state machine but
    accumulates content blocks instead of emitting events.
    """
    text_parts: list[str] = []
    tool_uses: list[dict[str, Any]] = []
    prompt_tok = 0
    completion_tok = 0
    done_reason = ""

    for line in chunks:
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = data.get("message") or {}
        if msg.get("content"):
            text_parts.append(msg["content"])
        elif data.get("response"):
            text_parts.append(data["response"])
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except (json.JSONDecodeError, TypeError):
                    args = {"_raw": args}
            tool_uses.append({
                "type": "tool_use",
                "id": f"toolu_{uuid.uuid4().hex[:24]}",
                "name": fn.get("name", ""),
                "input": args,
            })
        if data.get("done"):
            prompt_tok = data.get("prompt_eval_count") or prompt_tok
            completion_tok = data.get("eval_count") or completion_tok
            done_reason = data.get("done_reason", "") or done_reason

    content_blocks: list[dict[str, Any]] = []
    full_text = "".join(text_parts)
    if full_text:
        content_blocks.append({"type": "text", "text": full_text})
    content_blocks.extend(tool_uses)

    matched_stop: str | None = None
    if stop_sequences:
        for seq in stop_sequences:
            if seq and full_text.endswith(seq):
                matched_stop = seq
                break

    stop_reason = _map_done_reason(done_reason, bool(tool_uses), matched_stop)

    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": matched_stop,
        "usage": {
            "input_tokens": prompt_tok,
            "output_tokens": completion_tok,
        },
    }


# --- Token estimation -------------------------------------------------------


def estimate_tokens(text: str) -> int:
    """Rough token count. Tries tiktoken (cl100k), falls back to chars/4."""
    if not text:
        return 0
    try:
        import tiktoken  # type: ignore
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except (ImportError, Exception):
        return max(1, len(text) // 4)


def flatten_text_for_count(
    messages: list[dict[str, Any]], system: str | list[dict[str, Any]] | None = None,
) -> str:
    """Concatenate all text payloads from system + messages for token estimation."""
    parts: list[str] = []
    if system:
        parts.append(anthropic_system_to_text(system))
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                t = block.get("type")
                if t == "text":
                    parts.append(block.get("text", ""))
                elif t == "tool_use":
                    parts.append(json.dumps(block.get("input", {})))
                elif t == "tool_result":
                    inner = block.get("content", "")
                    if isinstance(inner, str):
                        parts.append(inner)
                    elif isinstance(inner, list):
                        for sub in inner:
                            if isinstance(sub, dict) and sub.get("type") == "text":
                                parts.append(sub.get("text", ""))
    return "\n".join(p for p in parts if p)


# --- Model mapping ----------------------------------------------------------


def map_anthropic_model(model: str, model_map: dict[str, str]) -> str:
    """Map a Claude-style model id (e.g. claude-sonnet-4-5) to a local Ollama model.

    Resolution order:
    1. Exact match in `model_map`
    2. If the requested model already looks like a local Ollama tag (contains ':'
       or matches no `claude-` prefix), pass it through unchanged.
    3. Fallback to `model_map["default"]`
    """
    if not model:
        return model_map.get("default", "")
    if model in model_map:
        return model_map[model]
    # Heuristic passthrough: caller might have sent a real local model name
    if ":" in model or not model.startswith("claude"):
        return model
    return model_map.get("default", model)


# Suppress _request timestamp linter (used for response shape compat)
_ = time.time
