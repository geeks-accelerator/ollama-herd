# Anthropic Messages API Compat Layer

## Context

Claude Code CLI is hardcoded to call Anthropic's Messages API. The only way to point it at a non-Anthropic backend is via `ANTHROPIC_BASE_URL`, which expects a server that speaks the Anthropic Messages protocol natively (`POST /v1/messages`, `POST /v1/messages/count_tokens`, Anthropic-flavored SSE). It does **not** speak OpenAI or Ollama.

We already expose `openai_compat.py` (`/v1/chat/completions`) and `ollama_compat.py` (`/api/chat`). Adding `anthropic_compat.py` (`/v1/messages`) lets Claude Code — and any other Anthropic-SDK-based tool — route through ollama-herd to local models like `qwen3-coder:30b`, with full benefit of scoring, queues, traces, and health.

**Driver:** hackathon in a few hours; team wants Claude Code as the agent UI but local models as the inference backend.

**Discovered:** 2026-04-22 — investigating whether `ANTHROPIC_BASE_URL` could short-circuit the LiteLLM sidecar.

## Goal

A FastAPI route file `server/routes/anthropic_compat.py` that:

1. Accepts `POST /v1/messages` in Anthropic Messages JSON
2. Translates → `InferenceRequest` → existing `score_with_fallbacks()` → `queue_mgr.enqueue()` → `StreamingProxy.stream_from_node()`
3. Translates Ollama's NDJSON response → Anthropic SSE event stream (or non-streaming JSON)
4. Handles **tool use** end-to-end (the hard part — Claude Code is useless without it)
5. Stubs `POST /v1/messages/count_tokens` with a tiktoken-equivalent estimate
6. Mounts on the same port as the rest of the router (`:11435`), no separate process

**Non-goals (deferred, behind 400 with clear error):**
- Vision/image content blocks (already partially supported by Ollama; punt for hackathon)
- Extended thinking blocks (`type: thinking`) — return as text in `content` for now
- Prompt caching (`cache_control` blocks) — accept and ignore
- Files API, Computer Use, Code Execution beta — return 501

## API Surface

### Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/v1/messages` | Inference (streaming + non-streaming) |
| `POST` | `/v1/messages/count_tokens` | Token estimation |
| `GET` | `/v1/models` | Already exists (openai_compat); Claude Code may probe — make sure it returns Ollama-available models with Anthropic-style IDs |

### Request shape (Anthropic → ours)

```json
{
  "model": "claude-sonnet-4-5",          // mapped to local model (see Model Mapping)
  "max_tokens": 4096,                     // → num_predict
  "messages": [
    {"role": "user", "content": "hi"},
    {"role": "user", "content": [        // content can be string OR array of blocks
      {"type": "text", "text": "..."},
      {"type": "tool_result", "tool_use_id": "toolu_01...", "content": "..."},
      {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "..."}}
    ]}
  ],
  "system": "You are a helpful assistant",   // string OR array of {type:text, text:...} blocks
  "tools": [
    {
      "name": "get_weather",
      "description": "...",
      "input_schema": {"type": "object", "properties": {...}, "required": [...]}
    }
  ],
  "tool_choice": {"type": "auto"},        // auto | any | tool | none
  "temperature": 0.7,
  "stream": true,
  "metadata": {"user_id": "..."},         // pass through to traces
  "stop_sequences": ["\n\nHuman:"]
}
```

### Response shape (non-streaming)

```json
{
  "id": "msg_01XYZ...",
  "type": "message",
  "role": "assistant",
  "model": "claude-sonnet-4-5",
  "content": [
    {"type": "text", "text": "I'll check the weather."},
    {"type": "tool_use", "id": "toolu_01ABC...", "name": "get_weather", "input": {"city": "SF"}}
  ],
  "stop_reason": "tool_use",          // end_turn | max_tokens | stop_sequence | tool_use
  "stop_sequence": null,
  "usage": {"input_tokens": 25, "output_tokens": 18}
}
```

### Streaming SSE events (the protocol Claude Code expects)

```
event: message_start
data: {"type":"message_start","message":{"id":"msg_01...","type":"message","role":"assistant","content":[],"model":"claude-sonnet-4-5","stop_reason":null,"usage":{"input_tokens":25,"output_tokens":1}}}

event: content_block_start
data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":" world"}}

event: content_block_stop
data: {"type":"content_block_stop","index":0}

event: content_block_start
data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"toolu_01...","name":"get_weather","input":{}}}

event: content_block_delta
data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\"city\":"}}

event: content_block_delta
data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"\"SF\"}"}}

event: content_block_stop
data: {"type":"content_block_stop","index":1}

event: message_delta
data: {"type":"message_delta","delta":{"stop_reason":"tool_use","stop_sequence":null},"usage":{"output_tokens":18}}

event: message_stop
data: {"type":"message_stop"}
```

## Translation Tables

### 1. Messages: Anthropic → Ollama

Anthropic `messages[].content` may be a string OR an array of typed blocks. Ollama wants `messages[].content` as a string (with optional `images: []` for multimodal and `tool_calls` for tool turns).

| Anthropic block | Ollama mapping |
|---|---|
| `{type: "text", text: "..."}` | concat into `content` string |
| `{type: "image", source: {type: "base64", ...}}` | append to `images: []` (existing converter at [streaming.py:1074](src/fleet_manager/server/streaming.py#L1074)) |
| `{type: "tool_use", id, name, input}` (assistant turn) | `message.tool_calls: [{function: {name, arguments: input}}]` (Ollama's tool_calls format) |
| `{type: "tool_result", tool_use_id, content, is_error}` | New `role: "tool"` message with `content` = result text. Drop `tool_use_id` if Ollama can't correlate — preserve order. |
| `{type: "thinking", thinking}` | drop on input (we don't replay reasoning to non-thinking models) |
| `cache_control: {...}` on any block | ignore |

### 2. System prompt

- Anthropic `system` is top-level (string or `[{type:text, text:...}]` array)
- Concatenate text blocks → single string
- Prepend as `messages[0]` with `role: "system"` if not already present

### 3. Tools: Anthropic → Ollama

Anthropic `tools[]` and Ollama `tools[]` are both JSON-schema-driven. Translation is mostly key renaming:

```python
def anthropic_tool_to_ollama(tool: dict) -> dict:
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool["input_schema"],   # already JSON schema
        },
    }
```

`tool_choice`:
- `{"type": "auto"}` → omit (Ollama default)
- `{"type": "any"}` → not natively supported; rely on prompt + hope (or inject an instruction)
- `{"type": "tool", "name": "X"}` → not natively supported; inject system prompt forcing X
- `{"type": "none"}` → strip `tools[]` from request

### 4. Streaming: Ollama NDJSON → Anthropic SSE

Ollama yields lines like:
```json
{"message":{"content":"Hel","tool_calls":[]},"done":false}
{"message":{"content":"lo","tool_calls":[]},"done":false}
{"message":{"content":"","tool_calls":[{"function":{"name":"get_weather","arguments":{"city":"SF"}}}]},"done":false}
{"done":true,"prompt_eval_count":25,"eval_count":18,"done_reason":"stop"}
```

The translator (a small state machine) must:

1. On first chunk: emit `message_start` with `usage.input_tokens` (use `prompt_eval_count` if available from a pre-flight, else estimate)
2. When text appears for the first time: emit `content_block_start` (text, index 0), then per-token `content_block_delta` (`text_delta`)
3. When a `tool_calls` entry appears: close the current text block with `content_block_stop`, emit `content_block_start` (tool_use, index N), then stream the JSON args as `input_json_delta` chunks (Ollama gives args fully-formed; we can either emit one big delta or pre-serialize and chunk)
4. On `done: true`: close the open block, emit `message_delta` with `stop_reason` mapped from `done_reason`, then `message_stop`

`done_reason` mapping:
| Ollama | Anthropic |
|---|---|
| `stop` (natural) | `end_turn` |
| `length` | `max_tokens` |
| `stop` + tool_calls present | `tool_use` |
| `stop` + matched stop_sequences | `stop_sequence` |

### 5. Model mapping

Claude Code defaults to model IDs like `claude-sonnet-4-5`, `claude-opus-4-7`. Two strategies:

**A. Env-var routing** (ship this):
```bash
FLEET_ANTHROPIC_MODEL_MAP='{"claude-sonnet-4-5":"qwen3-coder:30b","claude-opus-4-7":"qwen3:32b","claude-haiku-4-5":"qwen3:14b","default":"qwen3-coder:30b"}'
```
Plus a fallback: any unknown `claude-*` → `default`. Any `model` that's already a local model name (e.g. `qwen3-coder:30b`) → pass through.

**B. Auto-pick by hardware** (later): use the recommendations engine to pick the best local coder model.

For hackathon: ship A.

## Implementation Steps

### Step 1 — Skeleton route file

**New file:** `src/fleet_manager/server/routes/anthropic_compat.py`

Mirror the structure of `openai_compat.py`:

```python
router = APIRouter(tags=["anthropic"])

@router.post("/v1/messages")
async def messages(request: Request, body: AnthropicMessagesRequest):
    # 1. Map model
    local_model = _map_model(body.model, settings)
    # 2. Translate messages, system, tools → Ollama-compatible InferenceRequest
    inference_req = _translate_to_inference(body, local_model)
    # 3. Score + enqueue (same pattern as openai_compat lines 87-147)
    results, actual_model = await score_with_fallbacks(...)
    entry = QueueEntry(...)
    response_future = await queue_mgr.enqueue(entry, process_fn)
    stream = await response_future
    # 4. Branch streaming vs JSON
    if body.stream:
        return StreamingResponse(_anthropic_sse(stream, ...), media_type="text/event-stream")
    return await _accumulate_to_anthropic_json(stream, ...)

@router.post("/v1/messages/count_tokens")
async def count_tokens(body: AnthropicMessagesRequest):
    # tiktoken cl100k or simple len(text)//4 estimate
    return {"input_tokens": _estimate_tokens(body)}
```

**Wire it up:** add to `src/fleet_manager/server/app.py`:
```python
from fleet_manager.server.routes import anthropic_compat
app.include_router(anthropic_compat.router)
```

### Step 2 — Pydantic models

Create `src/fleet_manager/server/anthropic_models.py` with strict-but-forgiving models:
- `AnthropicMessagesRequest` (top-level)
- `ContentBlock` discriminated union: `TextBlock`, `ImageBlock`, `ToolUseBlock`, `ToolResultBlock`, `ThinkingBlock`
- `AnthropicTool`, `ToolChoice`
- `AnthropicMessageResponse`, `Usage`

Use `model_config = ConfigDict(extra="ignore")` so unknown fields (cache_control, etc.) don't 422.

### Step 3 — Request translator

`_translate_to_inference(body, local_model) -> InferenceRequest`:

1. Flatten `system` into one string, prepend as `{role: "system", content: ...}`
2. Walk `messages[]`:
   - String content → straight passthrough
   - Block list → bucket text/images/tool_use/tool_result; emit:
     - Assistant turn with text + tool_use → `{role: "assistant", content: text, tool_calls: [...]}`
     - User turn with tool_result → `{role: "tool", content: result_text}` (one tool message per result, in order)
3. Translate `tools[]` via `anthropic_tool_to_ollama()`
4. Build `InferenceRequest(original_format=RequestFormat.ANTHROPIC, ...)`

**Add to `RequestFormat` enum** in `src/fleet_manager/server/request.py`: `ANTHROPIC = "anthropic"`.

### Step 4 — Streaming proxy hook

In `src/fleet_manager/server/streaming.py` `stream_from_node()` ([streaming.py:530-533](src/fleet_manager/server/streaming.py#L530)):

```python
if request.original_format == RequestFormat.OPENAI:
    yield _ollama_to_openai_sse(...)
elif request.original_format == RequestFormat.ANTHROPIC:
    yield from _ollama_to_anthropic_sse(line, state)
else:
    yield raw_line + "\n"
```

`_ollama_to_anthropic_sse(line, state)` is the state machine described in **Translation Table 4**. Pass a mutable `state` dict (`{"text_open": False, "tool_index": 0, "current_tool_id": None, "message_id": "msg_01...", "input_tokens": 0, "output_tokens": 0}`) so cross-line state survives.

Alternative: keep the translator inside `anthropic_compat.py` and have it consume the raw NDJSON stream. **Recommended** — keeps `streaming.py` ignorant of yet-another-format, and the translator can be developed/tested in isolation.

### Step 5 — Non-streaming response builder

`_accumulate_to_anthropic_json(stream)`:
- Run the same state machine
- Instead of yielding SSE events, accumulate text + tool_uses into `content[]`
- Return final `AnthropicMessageResponse`

### Step 6 — `count_tokens` endpoint

Use `tiktoken.get_encoding("cl100k_base")` for a rough Claude-equivalent count (Anthropic uses a different tokenizer but Claude Code mainly uses this for budgeting, not exact accounting). Fall back to `len(text) // 4` if tiktoken not installed.

```python
@router.post("/v1/messages/count_tokens")
async def count_tokens(body: AnthropicMessagesRequest):
    text = _flatten_for_count(body)  # system + all message content text
    try:
        import tiktoken
        n = len(tiktoken.get_encoding("cl100k_base").encode(text))
    except ImportError:
        n = max(1, len(text) // 4)
    return {"input_tokens": n}
```

Add `tiktoken` to `pyproject.toml` dependencies.

### Step 7 — Auth pass-through

Claude Code sends `x-api-key: <key>` and `anthropic-version: 2023-06-01`. We don't validate (this is a local trust boundary), but log them at DEBUG level and reflect `anthropic-version` in response headers if the SDK requires it. Set a `FLEET_ANTHROPIC_REQUIRE_KEY` env var for users who want to gate it (default: off).

### Step 8 — Model list

Make sure `GET /v1/models` (already in `openai_compat.py`) also responds when Claude Code probes. If Claude Code sends `claude-*` IDs that aren't in our `/v1/models` list, that should be fine — it doesn't pre-validate.

### Step 9 — Trace integration

Pass `body.metadata.user_id` into the trace store as the request tag (already supported via `tags` column). Tag the request format as `anthropic` so we can filter dashboard analytics.

### Step 10 — End-to-end test

```bash
# Test against herd, no Claude Code yet
curl -s http://localhost:11435/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: dummy" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "claude-sonnet-4-5",
    "max_tokens": 100,
    "messages": [{"role": "user", "content": "say hi in 3 words"}]
  }' | jq .

# Streaming smoke test
curl -N http://localhost:11435/v1/messages \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-4-5","max_tokens":50,"stream":true,"messages":[{"role":"user","content":"hi"}]}'
```

Then point Claude Code at it:
```bash
export ANTHROPIC_BASE_URL=http://localhost:11435
export ANTHROPIC_AUTH_TOKEN=dummy
claude
```

## Tool Use Flow (the critical path)

This is what Claude Code lives or dies on. Walk through one full agentic turn:

**Turn 1 — Claude Code asks the model to pick a tool:**
```
POST /v1/messages
{
  "model": "claude-sonnet-4-5",
  "tools": [{"name": "Read", "input_schema": {...}}, {"name": "Bash", ...}],
  "messages": [{"role": "user", "content": "what's in /tmp?"}]
}
```

We translate, call qwen3-coder:30b with Ollama tool format, stream back:
```
content_block_start (tool_use, name=Bash, id=toolu_01abc)
content_block_delta (input_json_delta, partial_json="{\"command\":")
content_block_delta (input_json_delta, partial_json=" \"ls /tmp\"}")
content_block_stop
message_delta (stop_reason=tool_use)
message_stop
```

**Turn 2 — Claude Code executes the tool, sends back the result:**
```
POST /v1/messages
{
  "messages": [
    {"role": "user", "content": "what's in /tmp?"},
    {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_01abc", "name": "Bash", "input": {"command": "ls /tmp"}}]},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_01abc", "content": "file1\nfile2\n"}]}
  ]
}
```

We translate the `tool_result` block → Ollama `{role: "tool", content: "file1\nfile2\n"}` message. The model sees its own tool_call followed by the tool's output and continues the conversation.

**Pitfalls:**
- Ollama may not preserve `tool_use_id` correlation across turns. We rely on **positional order** — each `tool_result` follows in the same order as the `tool_use` blocks emitted by the assistant.
- Some local models emit JSON args as a string, not as parsed dict — handle both.
- Some local models hallucinate tool names — return them as-is in `tool_use.name`; Claude Code will reject and re-prompt.

## Testing Strategy

| Test | Location | Notes |
|---|---|---|
| Translate string content | `tests/test_anthropic_compat.py::test_translate_string_content` | Anthropic msg with string content → Ollama format |
| Translate block list (text + image) | same | Multiple text blocks concat; image → `images[]` |
| Translate tool_use round-trip | same | Assistant turn with `tool_use` block → Ollama `tool_calls` |
| Translate tool_result | same | User turn with `tool_result` block → Ollama `role: "tool"` message |
| System prompt flatten | same | Both string and array system prompts work |
| Tool schema translation | same | Anthropic `input_schema` → Ollama `parameters` |
| Streaming state machine | `tests/test_anthropic_streaming.py` | Feed canned Ollama NDJSON → assert SSE event sequence |
| Streaming with tool calls | same | Mid-stream tool_call closes text block, opens tool_use block |
| `count_tokens` estimate | `tests/test_anthropic_count_tokens.py` | Within 20% of tiktoken |
| Live integration | `tests/integration/test_claude_code_e2e.py` (manual / opt-in) | Actually run `claude` against the local server |

Aim: unit tests pass in <2s. No pytest network calls.

## Hackathon Cuts (defer if behind schedule)

In priority order — drop from the bottom:

1. ✅ **Must ship:** `/v1/messages` non-streaming, text-only, no tools
2. ✅ **Must ship:** `/v1/messages` streaming SSE for text-only
3. ✅ **Must ship:** Tool use (this is what makes Claude Code useful)
4. ✅ **Must ship:** `count_tokens` (Claude Code calls it before every turn)
5. 🟡 **Nice to have:** Multi-image content blocks
6. 🟡 **Nice to have:** Stop sequences
7. 🔴 **Defer:** Extended thinking blocks (return as text for now)
8. 🔴 **Defer:** Prompt caching (`cache_control`)
9. 🔴 **Defer:** Files API, computer use, code execution

## Known Limitations (write these into the README before the demo)

- **Tool quality depends on the local model.** `qwen3-coder:30b` and `qwen3:32b` handle tool use well. Smaller models drop tool calls or hallucinate args. Codestral does not natively output the `tool_calls` array — fall back to `qwen3-coder` for agentic loops.
- **No extended thinking parity.** Claude's `thinking` blocks are returned as plain text. Reasoning models (qwen3 thinking variants) emit reasoning into `content` instead of a separate block.
- **No prompt caching.** Every request is full re-encode. For long Claude Code sessions this is slower than real Claude.
- **Token counts are estimates.** Don't trust `usage` for billing — only for budget gating.
- **Vision works only if the local model supports it.** qwen3-coder doesn't see images. Use a vision model (e.g. `llava:13b`, `gemma3:27b`) explicitly.

## Configuration Reference Additions

Add to `docs/configuration-reference.md`:

| Var | Default | Purpose |
|---|---|---|
| `FLEET_ANTHROPIC_MODEL_MAP` | `{"default":"qwen3-coder:30b"}` | JSON map of `claude-*` → local model |
| `FLEET_ANTHROPIC_REQUIRE_KEY` | `false` | If true, validate `x-api-key` header against `FLEET_ANTHROPIC_API_KEY` |
| `FLEET_ANTHROPIC_API_KEY` | unset | Shared secret for `/v1/messages` when require_key is true |
| `FLEET_ANTHROPIC_DEFAULT_MAX_TOKENS` | `4096` | Used when `max_tokens` not specified |

## Open Questions

- Does Claude Code retry on certain HTTP status codes that Anthropic returns (e.g., 529 overloaded)? Match those for graceful degradation under fleet pressure.
- Should we synthesize an Anthropic-style request ID (`req_01...`) for traces, or use ollama-herd's existing UUIDs? Pick one and stay consistent.
- `metadata.user_id` — pass through to traces tag column or to a new column? Existing `tags` is a JSON blob, fine for now.

## Success Criteria

By demo time:

1. `claude` CLI launched with `ANTHROPIC_BASE_URL=http://localhost:11435` runs an agentic coding task end-to-end (read files, edit, run commands)
2. Dashboard shows the requests routed through `anthropic` format with traces
3. Tool calls execute and results feed back into the next turn
4. Latency for first token: <2s on `qwen3-coder:30b` warm
5. No Python tracebacks in `/tmp/herd-router.log` during the run
