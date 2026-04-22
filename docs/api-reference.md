# API Reference

Complete endpoint documentation for Ollama Herd.

---

## OpenAI-Compatible Endpoints

### `POST /v1/chat/completions`

OpenAI-compatible chat completions with streaming and non-streaming support.

**Request body:**

```json
{
  "model": "llama3.3:70b",
  "messages": [
    {"role": "system", "content": "You are helpful."},
    {"role": "user", "content": "Hello!"}
  ],
  "stream": true,
  "temperature": 0.7,
  "max_tokens": 1024,
  "fallback_models": ["qwen2.5:32b", "qwen2.5:7b"],
  "metadata": {"tags": ["my-app", "production"]},
  "user": "alice"
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `model` | string | *required* | Model name (must exist on at least one node) |
| `messages` | array | `[]` | Chat messages in OpenAI format |
| `stream` | boolean | `false` | Enable streaming (SSE) |
| `temperature` | float | `0.7` | Sampling temperature |
| `max_tokens` | integer | `null` | Maximum tokens to generate |
| `fallback_models` | array | `[]` | Backup models to try if primary unavailable |
| `metadata.tags` | array | `[]` | Tags for per-app analytics (e.g., `["my-app", "prod"]`) |
| `user` | string | `null` | User identifier — stored as `user:<value>` tag |

**Request headers:**

| Header | Description |
|--------|-------------|
| `X-Herd-Tags` | Comma-separated tags (alternative to `metadata.tags` in body) |

Tags from all sources (body, header, user field) are merged and deduplicated. See [Request Tagging](request-tagging.md) for details.

**Streaming response** (`stream: true`):

```
data: {"id":"chatcmpl-abc123","choices":[{"delta":{"role":"assistant"},"index":0}]}\n\n
data: {"id":"chatcmpl-abc123","choices":[{"delta":{"content":"Hello"},"index":0}]}\n\n
data: {"id":"chatcmpl-abc123","choices":[{"delta":{},"finish_reason":"stop","index":0}]}\n\n
data: [DONE]\n\n
```

**Non-streaming response** (`stream: false`):

```json
{
  "id": "chatcmpl-abc123def456",
  "object": "chat.completion",
  "created": 1710000000,
  "model": "llama3.3:70b",
  "choices": [
    {
      "index": 0,
      "message": {"role": "assistant", "content": "Hello! How can I help?"},
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 12,
    "completion_tokens": 8,
    "total_tokens": 20
  }
}
```

**Response headers:**

| Header | Description |
|--------|-------------|
| `X-Fleet-Node` | Node ID that handled the request |
| `X-Fleet-Score` | Winning routing score (integer) |
| `X-Fleet-Fallback` | Fallback model used (only if primary was unavailable) |
| `X-Fleet-Retries` | Number of retries (only if retry occurred) |
| `X-Fleet-Context-Overflow` | Context overflow warning: `estimated_tokens=N; context_length=M` (only if estimated tokens exceed the node's context window) |
| `X-Thinking-Tokens` | Estimated tokens spent on chain-of-thought reasoning (thinking models only, non-streaming) |
| `X-Output-Tokens` | Estimated tokens of visible output content (thinking models only, non-streaming) |
| `X-Budget-Used` | `completion_tokens/num_predict` — at-a-glance budget check (non-streaming only) |
| `X-Done-Reason` | Ollama's done_reason: `stop` (natural end) or `length` (budget exhausted) (non-streaming only) |

**Error responses:**

| Status | Condition |
|--------|-----------|
| 400 | Missing `model` field |
| 404 | Model not found on any node (auto-pull attempted first if `FLEET_AUTO_PULL=true`) |
| 503 | Model exists but no node can serve it right now |

---

### `POST /v1/images/generations`

OpenAI-compatible image generation. Wraps `/api/generate-image` in OpenAI's image API format.

**Request body:**

```json
{
  "model": "z-image-turbo",
  "prompt": "a cat sitting on a laptop",
  "size": "1024x1024",
  "response_format": "b64_json"
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `model` | string | *required* | Image model name |
| `prompt` | string | *required* | Text description of the image |
| `size` | string | `1024x1024` | Image dimensions (`WIDTHxHEIGHT`) |
| `response_format` | string | `b64_json` | `b64_json` returns base64 PNG; `url` returns raw PNG bytes |
| `steps` | integer | model default | Inference steps |
| `guidance` | float | model default | Guidance scale |
| `seed` | integer | random | Seed for reproducibility |
| `negative_prompt` | string | `""` | What to avoid in the image |

**Response** (`b64_json`):

```json
{
  "created": 1710000000,
  "data": [
    {
      "b64_json": "iVBORw0KGgo...",
      "revised_prompt": "a cat sitting on a laptop"
    }
  ]
}
```

**Response** (`url`): Raw PNG bytes with `Content-Type: image/png`.

**Error responses:** `400` (missing fields), `404` (model not available), `502` (generation failed), `503` (disabled).

---

### `GET /v1/models`

List all models across the fleet — LLM, image, and embedding models (loaded + available on disk).

**Response:**

```json
{
  "object": "list",
  "data": [
    {
      "id": "llama3.3:70b",
      "object": "model",
      "created": 1710000000,
      "owned_by": "ollama"
    },
    {
      "id": "qwen2.5:7b",
      "object": "model",
      "created": 1710000000,
      "owned_by": "ollama"
    }
  ]
}
```

---

## Ollama-Compatible Endpoints

### `POST /api/chat`

Ollama-compatible chat endpoint. Streaming is enabled by default (matches Ollama behavior).

**Request body:**

```json
{
  "model": "llama3.3:70b",
  "messages": [
    {"role": "user", "content": "Hello!"}
  ],
  "stream": true,
  "options": {
    "temperature": 0.7,
    "num_predict": 1024
  },
  "fallback_models": ["qwen2.5:32b"],
  "metadata": {"tags": ["my-app"]}
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `model` | string | *required* | Model name |
| `messages` | array | `[]` | Chat messages |
| `stream` | boolean | `true` | Streaming (NDJSON) |
| `options.temperature` | float | `0.7` | Sampling temperature |
| `options.num_predict` | integer | `null` | Max tokens |
| `fallback_models` | array | `[]` | Backup models |
| `metadata.tags` | array | `[]` | Tags for per-app analytics |
| `user` | string | `null` | User identifier — stored as `user:<value>` tag |

The `metadata` and `fallback_models` fields are stripped before proxying to Ollama. The `X-Herd-Tags` header is also supported (same as OpenAI endpoint).

**Streaming response** (NDJSON, one JSON object per line):

```json
{"message":{"role":"assistant","content":"Hello"},"done":false}
{"message":{"role":"assistant","content":"!"},"done":false}
{"message":{"role":"assistant","content":""},"done":true,"total_duration":1234567890,"prompt_eval_count":12,"eval_count":8}
```

**Non-streaming response:**

```json
{
  "message": {"role": "assistant", "content": "Hello! How can I help?"},
  "done": true,
  "total_duration": 1234567890,
  "prompt_eval_count": 12,
  "eval_count": 8
}
```

---

### `POST /api/generate`

Ollama-compatible generate endpoint. Uses `prompt` instead of `messages`.

**Request body:**

```json
{
  "model": "llama3.3:70b",
  "prompt": "Why is the sky blue?",
  "stream": true,
  "options": {
    "temperature": 0.7
  }
}
```

Supports the same `metadata.tags`, `user`, and `X-Herd-Tags` header as `/api/chat`.

Response format is the same as `/api/chat`.

---

### `GET /api/tags`

List all models across the fleet with node information. Includes LLM models and image models (mflux + DiffusionKit).

**Response:**

```json
{
  "models": [
    {
      "name": "llama3.3:70b",
      "model": "llama3.3:70b",
      "size": 42949672960,
      "details": {
        "fleet_nodes": ["mac-studio-ultra", "macbook-pro-m4"]
      }
    },
    {
      "name": "z-image-turbo",
      "model": "z-image-turbo",
      "size": 0,
      "details": {
        "fleet_nodes": ["mac-studio-ultra"],
        "type": "image"
      }
    }
  ]
}
```

The `fleet_nodes` array shows which nodes have each model (loaded or available on disk). The `size` field is in bytes. Image models include a `"type": "image"` field in details.

---

### `GET /api/ps`

List all currently loaded (hot) models across the fleet.

**Response:**

```json
{
  "models": [
    {
      "name": "llama3.3:70b",
      "model": "llama3.3:70b",
      "size": 42949672960,
      "fleet_node": "mac-studio-ultra"
    },
    {
      "name": "qwen2.5:7b",
      "model": "qwen2.5:7b",
      "size": 4294967296,
      "fleet_node": "macbook-air-m2"
    }
  ]
}
```

---

### `POST /api/embed`
### `POST /api/embeddings`

Ollama-compatible embeddings endpoints. Routes to the best node with the requested embedding model. Both endpoints accept the same body — `/api/embed` uses `input` field, `/api/embeddings` uses `prompt` field (both are accepted on either endpoint).

**Request body:**

```json
{
  "model": "nomic-embed-text",
  "input": "The quick brown fox jumps over the lazy dog"
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `model` | string | *required* | Embedding model name |
| `input` | string/array | — | Text(s) to embed (preferred for `/api/embed`) |
| `prompt` | string | — | Text to embed (preferred for `/api/embeddings`) |
| `metadata.tags` | array | `[]` | Tags for per-app analytics |

The raw body is proxied to Ollama's embedding endpoint on the selected node. Response format matches Ollama's native response.

**Error responses:**

| Status | Condition |
|--------|-----------|
| 400 | Missing `model` field |
| 404 | Model not found on any node |
| 503 | Model exists but no node can serve it |

---

### `POST /api/pull`

Pull a model onto the fleet. Ollama-compatible — streams NDJSON progress matching Ollama's wire format. Auto-selects the node with the most available memory, or accepts an explicit `node_id`.

**Request body:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | — | Model to pull (Ollama standard field) |
| `model` | string | — | Model to pull (alias for `name` — many agents send this) |
| `stream` | bool | `true` | Stream NDJSON progress or return on completion |
| `node_id` | string | auto | Target node (auto-selects best node if omitted) |

**Streaming response** (`stream: true`, default):

```
{"status":"pulling manifest"}
{"status":"pulling abc123...","digest":"sha256:abc123","total":5000000,"completed":2500000}
{"status":"verifying sha256:abc123"}
{"status":"writing manifest"}
{"status":"success"}
```

**Non-streaming response** (`stream: false`):

```json
{"status": "success"}
```

**Response headers:**

| Header | Description |
|--------|-------------|
| `X-Fleet-Node` | Node that received the pull |

**Error responses:**

| Status | Condition |
|--------|-----------|
| 400 | Missing model name, or model is a non-Ollama model (mflux, DiffusionKit, MLX) — returns install instructions |
| 404 | Specified `node_id` not found or offline |
| 409 | Model is already being pulled |
| 503 | No node has enough available memory |

**Non-Ollama models:** Image generation models (z-image-turbo, flux-dev, sd3-medium, sd3.5-large) and speech-to-text models (qwen3-asr) are not Ollama models and cannot be pulled via this endpoint. The error response includes the correct install command for each.

**Examples:**

```bash
# Pull a model (streams NDJSON progress)
curl -N http://localhost:11435/api/pull -d '{"name": "codestral"}'

# Pull to a specific node
curl -N http://localhost:11435/api/pull -d '{"name": "llama3.3:70b", "node_id": "mac-studio"}'

# Non-streaming (blocks until complete)
curl http://localhost:11435/api/pull -d '{"name": "phi4", "stream": false}'
```

---

### `GET /api/image-models`

List all image models across the fleet (mflux, DiffusionKit, and Ollama native).

**Response:**

```json
{
  "models": [
    {
      "name": "z-image-turbo",
      "type": "image",
      "backend": "mflux",
      "fleet_nodes": ["mac-studio-ultra"]
    },
    {
      "name": "sd3-medium",
      "type": "image",
      "backend": "mflux",
      "fleet_nodes": ["macbook-pro-m4"]
    },
    {
      "name": "x/z-image-turbo:latest",
      "type": "image",
      "backend": "ollama",
      "fleet_nodes": ["mac-studio-ultra"]
    }
  ]
}
```

The `backend` field indicates which system handles the model: `mflux` (mflux/DiffusionKit via port 11436) or `ollama` (Ollama native image models with `x/` prefix).

---

## Anthropic-Compatible Endpoints

Native Anthropic Messages API surface for Claude Code and other anthropic-SDK clients. See [docs/guides/claude-code-integration.md](guides/claude-code-integration.md) for the full setup walkthrough.

### `POST /v1/messages`

Anthropic Messages endpoint — streaming + non-streaming, full tool use, system prompts, stop sequences.

**Request body:**

```json
{
  "model": "claude-sonnet-4-5",
  "max_tokens": 4096,
  "messages": [
    {"role": "user", "content": "What's the weather in Paris?"}
  ],
  "system": "You are a helpful assistant.",
  "tools": [{
    "name": "get_weather",
    "description": "Get current weather for a city",
    "input_schema": {
      "type": "object",
      "properties": {"city": {"type": "string"}},
      "required": ["city"]
    }
  }],
  "tool_choice": {"type": "auto"},
  "stream": false,
  "temperature": 0.7,
  "stop_sequences": ["\n\nHuman:"],
  "metadata": {"user_id": "alice"}
}
```

**Headers (recommended):**

- `x-api-key: <key>` — required when `FLEET_ANTHROPIC_REQUIRE_KEY=true`
- `anthropic-version: 2023-06-01` — reflected back in response headers

**Model mapping:** `claude-*` model ids are mapped to local Ollama models via `FLEET_ANTHROPIC_MODEL_MAP`. Real Ollama model names (e.g. `qwen3-coder:30b`) pass through unchanged. See [configuration-reference.md](configuration-reference.md).

**Non-streaming response:**

```json
{
  "id": "msg_abc123...",
  "type": "message",
  "role": "assistant",
  "model": "claude-sonnet-4-5",
  "content": [
    {"type": "text", "text": "I'll check that for you."},
    {"type": "tool_use", "id": "toolu_xyz...", "name": "get_weather", "input": {"city": "Paris"}}
  ],
  "stop_reason": "tool_use",
  "stop_sequence": null,
  "usage": {"input_tokens": 25, "output_tokens": 18}
}
```

**Stop reasons:** `end_turn`, `max_tokens`, `stop_sequence`, `tool_use`.

**Streaming SSE events:** `message_start` → `content_block_start/delta/stop` (one set per text or tool_use block) → `message_delta` → `message_stop`. Tool calls open a new `content_block_start` of type `tool_use` mid-stream and emit args via `input_json_delta`.

**Tool result round-trip:** Send a follow-up request with the assistant's `tool_use` block in `messages` and a `user` message containing a `tool_result` block referencing the same `tool_use_id`:

```json
{
  "role": "user",
  "content": [{"type": "tool_result", "tool_use_id": "toolu_xyz...", "content": "18C, sunny"}]
}
```

### `POST /v1/messages/count_tokens`

Token estimate for a Messages payload — used by Claude Code for budget gating before each turn. Best-effort: tiktoken `cl100k` if installed, otherwise `len(text)/4`. Not for billing.

**Request body:** same shape as `/v1/messages`.

**Response:**

```json
{"input_tokens": 142}
```

### `GET /v1/messages`

Friendly probe endpoint — Claude Code may GET this during connection setup.

**Response:**

```json
{"ok": true, "service": "ollama-herd", "endpoint": "/v1/messages", "ts": 1776857000}
```

### Response headers (all `/v1/messages` calls)

- `X-Fleet-Node` — node id that served the request
- `X-Fleet-Score` — routing score (higher = better fit)
- `X-Fleet-Fallback` — present when a fallback model was used
- `X-Fleet-Retries` — present when the request was retried
- `anthropic-version` — echoed from the request

---

## Fleet Management

### `GET /fleet/status`

Full fleet state — nodes, queues, hardware metrics, and health summary.

**Response:**

```json
{
  "fleet": {
    "nodes_total": 3,
    "nodes_online": 2,
    "models_loaded": 4,
    "requests_active": 1
  },
  "nodes": [
    {
      "node_id": "mac-studio-ultra",
      "status": "online",
      "hardware": {
        "memory_total_gb": 192,
        "cores_physical": 24
      },
      "ollama_url": "http://10.0.0.100:11434",
      "cpu": {
        "cores_physical": 24,
        "utilization_pct": 15.2
      },
      "memory": {
        "total_gb": 192.0,
        "used_gb": 45.3,
        "available_gb": 146.7,
        "pressure": "nominal"
      },
      "ollama": {
        "models_loaded": [
          {"name": "llama3.3:70b", "size_gb": 40.0}
        ],
        "models_available": ["llama3.3:70b", "qwen2.5:32b"],
        "requests_active": 1
      }
    }
  ],
  "queues": {
    "mac-studio-ultra:llama3.3:70b": {
      "node_id": "mac-studio-ultra",
      "model": "llama3.3:70b",
      "pending": 0,
      "in_flight": 1,
      "completed": 42,
      "failed": 0,
      "concurrency": 8
    }
  },
  "timestamp": 1710000000.0
}
```

---

### `POST /heartbeat`

Receives heartbeats from node agents. Internal endpoint — not intended for external clients.

**Regular heartbeat:**

```json
{
  "node_id": "mac-studio-ultra",
  "cpu": {"cores_physical": 24, "utilization_pct": 15.2},
  "memory": {"total_gb": 192.0, "used_gb": 45.3, "available_gb": 146.7, "pressure": "nominal"},
  "ollama": {
    "models_loaded": [{"name": "llama3.3:70b", "size_gb": 40.0}],
    "models_available": ["llama3.3:70b", "qwen2.5:32b"],
    "requests_active": 1
  },
  "hardware": {"memory_total_gb": 192, "cores_physical": 24}
}
```

**Drain signal:**

```json
{
  "node_id": "mac-studio-ultra",
  "draining": true
}
```

**Response:**

```json
{"status": "ok", "node_status": "online"}
```

---

## Image Generation

### `POST /api/generate-image`

Generate an image on the best available node with mflux. Requires `FLEET_IMAGE_GENERATION=true`.

**Request body (JSON):**

```json
{
  "model": "z-image-turbo",
  "prompt": "a cat sitting on a laptop",
  "width": 1024,
  "height": 1024,
  "steps": 4,
  "quantize": 8,
  "seed": 42,
  "negative_prompt": ""
}
```

**Response:** Raw PNG bytes with `Content-Type: image/png`.

**Response headers:**

| Header | Description |
|--------|-------------|
| `X-Fleet-Node` | Node that generated the image |
| `X-Fleet-Model` | Image model used |
| `X-Generation-Time` | Generation time in ms |

**Error responses:** `400` (missing fields), `404` (model not available), `502` (generation failed), `503` (disabled).

---

## Transcription (Speech-to-Text)

### `POST /api/transcribe`

Transcribe an audio file on the best available node with Qwen3-ASR. Requires `FLEET_TRANSCRIPTION=true`.

**Request:** `multipart/form-data` with `audio` file field.

```bash
curl http://localhost:11435/api/transcribe -F "audio=@recording.wav"
```

**Supported formats:** WAV, MP3, M4A, FLAC, MP4, OGG (any format FFmpeg supports).

**Response:**

```json
{
  "text": "Full transcription text...",
  "language": "English",
  "chunks": [
    {
      "text": "Hello, this is a test.",
      "start": 0.0,
      "end": 2.5,
      "chunk_index": 0,
      "language": "English"
    }
  ]
}
```

**Response headers:**

| Header | Description |
|--------|-------------|
| `X-Fleet-Node` | Node that transcribed the audio |
| `X-Fleet-Model` | Transcription model used |
| `X-Transcription-Time` | Processing time in ms |

**Error responses:** `404` (no STT models available), `502` (transcription failed), `503` (disabled).

---

### `GET /fleet/queue`

Lightweight queue status for client-side backoff decisions. Designed for high-frequency polling.

**Response:**

```json
{
  "queue_depth": 5,
  "pending": 2,
  "in_flight": 3,
  "completed": 1250,
  "failed": 3,
  "estimated_wait_ms": 15000,
  "nodes_online": 2,
  "queues": {
    "Studio:gpt-oss:120b": {
      "pending": 2,
      "in_flight": 3,
      "concurrency": 2,
      "model": "gpt-oss:120b",
      "node_id": "Studio"
    }
  },
  "timestamp": 1712345678.123
}
```

Use `estimated_wait_ms` to decide whether to send a request now or back off. `queue_depth` = `pending` + `in_flight`.

---

## Dashboard

### HTML Pages

| Endpoint | Description |
|----------|-------------|
| `GET /dashboard` | Fleet Overview — live node status, CPU/memory, loaded models, queue depths |
| `GET /dashboard/trends` | Historical charts — requests/hour, average latency, token throughput |
| `GET /dashboard/models` | Model Insights — per-model latency, tokens/sec, usage comparison |
| `GET /dashboard/apps` | Apps — per-tag analytics with latency, tokens, errors, daily trends |
| `GET /dashboard/benchmarks` | Benchmarks — capacity growth charts, per-run results |
| `GET /dashboard/health` | Health — 15 automated fleet health checks with severity and recommendations |
| `GET /dashboard/recommendations` | Recommendations — AI-powered model mix per node with one-click pull |
| `GET /dashboard/settings` | Settings — runtime toggles, config tables, node versions |

### SSE Stream

#### `GET /dashboard/events`

Server-Sent Events stream for real-time fleet updates. Pushes a JSON snapshot every 2 seconds.

**Event data:**

```json
{
  "nodes": [...],
  "queues": {...},
  "timestamp": 1710000000.0
}
```

Each node includes `cpu`, `memory`, `ollama`, and optionally `capacity` fields (when adaptive capacity learning is enabled).

### JSON APIs

#### `GET /dashboard/api/trends`

Hourly aggregated stats for the trends charts.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `hours` | integer | `72` | Number of hours of history to return |

**Response:**

```json
{
  "hours": 72,
  "data": [
    {
      "hour_bucket": "2026-03-08 01:00:00",
      "request_count": 15,
      "avg_latency_ms": 4200.5,
      "avg_prompt_tokens": 125.0,
      "avg_completion_tokens": 350.0
    }
  ]
}
```

---

#### `GET /dashboard/api/models`

Per-model daily aggregated stats for the Model Insights page.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `days` | integer | `7` | Number of days of history |

**Response:**

```json
{
  "days": 7,
  "daily": [
    {
      "model": "llama3.3:70b",
      "day": "2026-03-08",
      "request_count": 42,
      "avg_latency_ms": 4200.5,
      "total_prompt_tokens": 5250,
      "total_completion_tokens": 14700
    }
  ],
  "summary": [
    {
      "model": "llama3.3:70b",
      "total_requests": 312,
      "avg_latency_ms": 4100.2,
      "total_prompt_tokens": 39000,
      "total_completion_tokens": 109200
    }
  ]
}
```

---

#### `GET /dashboard/api/overview`

Summary totals for the dashboard header cards.

**Response:**

```json
{
  "total_requests": 847,
  "total_prompt_tokens": 105875,
  "total_completion_tokens": 296450,
  "total_tokens": 402325,
  "models_count": 5
}
```

---

#### `GET /dashboard/api/usage`

Per-node, per-model, per-day usage stats from request traces.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `days` | integer | `7` | Number of days of history |

**Response:**

```json
{
  "days": 7,
  "data": [
    {
      "node_id": "mac-studio-ultra",
      "model": "llama3.3:70b",
      "day": "2026-03-08",
      "request_count": 25,
      "completed": 24,
      "failed": 1,
      "avg_latency_ms": 4200.5,
      "total_prompt_tokens": 3125,
      "total_completion_tokens": 8750
    }
  ]
}
```

---

#### `GET /dashboard/api/traces`

Recent request traces for debugging and observability.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | integer | `50` | Maximum number of traces to return |

**Response:**

```json
{
  "traces": [
    {
      "request_id": "abc12345-def6-7890",
      "model": "llama3.3:70b",
      "original_model": "llama3.3:70b",
      "node_id": "mac-studio-ultra",
      "score": 73.0,
      "scores_breakdown": {
        "model_thermal": 50,
        "memory_fit": 20,
        "queue_depth": 0,
        "wait_time": 0,
        "role_affinity": 15
      },
      "status": "completed",
      "latency_ms": 4200.5,
      "time_to_first_token_ms": 850.2,
      "prompt_tokens": 125,
      "completion_tokens": 350,
      "retry_count": 0,
      "fallback_used": 0,
      "excluded_nodes": [],
      "client_ip": "10.0.0.50",
      "original_format": "openai",
      "error_message": null,
      "timestamp": 1710000000.0,
      "tags": ["my-app", "production"]
    }
  ]
}
```

---

#### `GET /dashboard/api/apps`

Per-tag aggregated stats for the Apps page.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `days` | integer | `7` | Number of days of history |

**Response:**

```json
{
  "days": 7,
  "by_tag": [
    {
      "tag": "my-app",
      "request_count": 150,
      "avg_latency_ms": 3200.5,
      "avg_ttft_ms": 420.1,
      "total_prompt_tokens": 18750,
      "total_completion_tokens": 52500,
      "error_rate": 0.02
    }
  ]
}
```

---

#### `GET /dashboard/api/apps/daily`

Per-tag, per-day breakdown for trend charts.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `days` | integer | `7` | Number of days of history |

**Response:**

```json
{
  "days": 7,
  "daily": [
    {
      "tag": "my-app",
      "day": "2026-03-08",
      "request_count": 25,
      "avg_latency_ms": 3100.0,
      "total_tokens": 8500
    }
  ]
}
```

---

#### `GET /dashboard/api/benchmarks`

List stored benchmark runs.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | integer | `50` | Maximum number of runs to return |

**Response:**

```json
{
  "data": [
    {
      "run_id": "bench-1772966095",
      "timestamp": 1710000000.0,
      "duration_s": 300.0,
      "total_requests": 150,
      "total_failures": 0,
      "total_prompt_tokens": 18750,
      "total_completion_tokens": 52500,
      "requests_per_sec": 0.5,
      "tokens_per_sec": 175.0,
      "latency_p50_ms": 2100.0,
      "latency_p95_ms": 4500.0,
      "latency_p99_ms": 6200.0,
      "ttft_p50_ms": 450.0,
      "ttft_p95_ms": 1200.0,
      "ttft_p99_ms": 1800.0,
      "fleet_snapshot": "{...}",
      "per_model_results": "[...]",
      "per_node_results": "[...]",
      "peak_utilization": "[...]"
    }
  ]
}
```

---

#### `POST /dashboard/api/benchmarks`

Save benchmark results from the benchmark script.

**Request body:**

```json
{
  "run_id": "bench-1772966095",
  "timestamp": 1710000000.0,
  "duration_s": 300.0,
  "total_requests": 150,
  "total_failures": 0,
  "total_prompt_tokens": 18750,
  "total_completion_tokens": 52500,
  "requests_per_sec": 0.5,
  "tokens_per_sec": 175.0,
  "latency_p50_ms": 2100.0,
  "latency_p95_ms": 4500.0,
  "latency_p99_ms": 6200.0,
  "ttft_p50_ms": 450.0,
  "ttft_p95_ms": 1200.0,
  "ttft_p99_ms": 1800.0,
  "fleet_snapshot": "{}",
  "per_model_results": "[]",
  "per_node_results": "[]",
  "peak_utilization": "[]"
}
```

**Response:**

```json
{"status": "saved", "run_id": "bench-1772966095"}
```

---

#### `POST /dashboard/api/benchmarks/start`

Start a benchmark run from the dashboard. Supports default (loaded models only) and smart mode (fill memory with recommended models first).

**Request body:**

```json
{
  "mode": "smart",
  "duration": 300,
  "model_types": ["llm", "vision", "embed", "image"]
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `mode` | string | `"default"` | `"default"` (loaded models) or `"smart"` (fill memory first) |
| `duration` | float | `300` | Benchmark duration in seconds |
| `model_types` | array | `["llm"]` | Types to benchmark: `"llm"`, `"vision"`, `"embed"`, `"image"` |

**Response:** `{"status": "started", "run_id": "bench-...", "mode": "smart", "duration": 300, "model_types": ["llm", "vision", "embed", "image"]}`

**Error:** `409` if a benchmark is already running.

---

#### `GET /dashboard/api/benchmarks/progress`

Get current benchmark status and progress. Poll every 2 seconds during a run.

**Response:**

```json
{
  "status": "running",
  "phase": "Running 300s benchmark (embed, image, llm)...",
  "elapsed": 45.2,
  "duration": 300,
  "requests_completed": 1250,
  "requests_failed": 0,
  "tok_per_sec": 85.3,
  "models": ["gpt-oss:120b", "nomic-embed-text:latest", "z-image-turbo"],
  "models_pulled": ["codestral:22b", "llama3.2:1b"],
  "pull_progress": {},
  "error": null,
  "run_id": "bench-..."
}
```

Status values: `idle`, `pulling`, `warming_up`, `running`, `complete`, `error`, `cancelled`.

During pull phase, `pull_progress` includes: `model`, `node_id`, `current`, `total`, `ram_gb`, `on_disk`, `pct`, `completed_gb`, `total_gb`.

---

#### `POST /dashboard/api/benchmarks/cancel`

Cancel a running benchmark.

**Response:** `{"status": "cancelled"}` or `{"status": "not_running"}`.

---

#### `GET /dashboard/api/context-usage`

Per-model context usage analysis — actual vs allocated context sizes.

**Query params:** `days` (default: 7) — lookback window for trace analysis.

**Response:**

```json
{
  "days": 7,
  "models": [
    {
      "model": "gpt-oss:120b",
      "allocated_ctx": 131072,
      "override_ctx": 16384,
      "request_count": 67234,
      "prompt_tokens": {"avg": 1288, "p50": 833, "p75": 1322, "p95": 3706, "p99": 4797, "max": 10938},
      "total_tokens": {"p95": 4120, "p99": 5409, "max": 34721, "max_24h": 8675},
      "utilization_pct": 4.1,
      "recommended_ctx": 16384,
      "savings_pct": 87.5
    }
  ]
}
```

---

### `GET /dashboard/api/recommendations`

Model mix recommendations for the fleet based on hardware, usage patterns, and curated benchmark data. Results are cached for 5 minutes.

**Query parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `refresh` | int | `0` | Pass `1` to force re-analysis (bypasses cache) |

**Response:**

```json
{
  "nodes": [
    {
      "node_id": "mac-studio",
      "total_ram_gb": 512.0,
      "usable_ram_gb": 506.0,
      "current_models": ["gpt-oss:120b", "qwen3:235b-a22b"],
      "recommendations": [
        {
          "model": "gpt-oss:120b",
          "display_name": "GPT-OSS 120B",
          "category": "reasoning",
          "ram_gb": 72.0,
          "quality_score": 89.0,
          "reason": "Currently your most-used reasoning model (4303 requests in 24h)",
          "priority": "high",
          "already_available": true
        }
      ],
      "total_recommended_ram_gb": 313.0,
      "ram_headroom_gb": 193.0
    }
  ],
  "usage": {
    "total_requests_24h": 4484,
    "category_breakdown": {"reasoning": 4390, "general": 52, "coding": 42},
    "top_models": [{"model": "gpt-oss:120b", "requests": 4390, "category": "reasoning"}],
    "category_coverage": {"general": true, "coding": true, "reasoning": true, "creative": true, "fast-chat": true}
  },
  "fleet_summary": "1 node(s), 512GB total RAM, 6 model(s) recommended using 313GB",
  "uncovered_categories": [],
  "generated_at": 1741700000.0
}
```

The recommender uses a curated catalog of 30+ models with benchmark data (MMLU, HumanEval, MT-Bench) and considers: available RAM per node (with 6GB OS overhead), last 24h request patterns, models already downloaded, cross-fleet distribution (avoids redundant large models), and a 50% RAM cap per model to ensure variety.

---

### `POST /dashboard/api/pull`

Pull a model onto a specific node via Ollama's `/api/pull` API. Blocks until the pull completes (may take minutes for large models).

**Request body:**

```json
{
  "node_id": "mac-studio",
  "model": "codestral:22b"
}
```

**Response:**

```json
{"ok": true, "node_id": "mac-studio", "model": "codestral:22b"}
```

Returns `{"ok": false, ...}` if the pull fails. The router proxies the pull request to the target node's Ollama instance using the same HTTP client used for inference (600s read timeout).

### `POST /dashboard/api/delete`

Delete a model from a specific node via Ollama's `DELETE /api/delete` API.

**Request body:**

```json
{
  "node_id": "mac-studio",
  "model": "qwen3-coder-480b:latest"
}
```

**Response:**

```json
{"ok": true, "node_id": "mac-studio", "model": "qwen3-coder-480b:latest"}
```

Returns `{"ok": false, ...}` if the delete fails. This permanently removes the model from the node's disk — it must be re-downloaded to use again.

### `GET /dashboard/api/model-management`

Per-node model details with disk sizes, usage statistics, and last-used timestamps for the model management UI.

**Response:**

```json
{
  "nodes": [
    {
      "node_id": "mac-studio",
      "models": [
        {
          "name": "gpt-oss:120b",
          "display_name": "GPT-OSS 120B",
          "category": "reasoning",
          "size_gb": 60.9,
          "parameter_size": "120B",
          "quantization": "Q4_K_M",
          "last_used": 1741700000.0,
          "days_unused": 0.1,
          "total_requests": 18065,
          "loaded_in_vram": true,
          "unused": false
        }
      ],
      "total_size_gb": 959.0,
      "disk_available_gb": 2994.0,
      "disk_total_gb": 3722.0
    }
  ]
}
```

Models are sorted by: loaded in VRAM first, then by last-used descending, then alphabetically. The `unused` flag is `true` if the model has never been used through the router or hasn't been used in 7+ days.

### `GET /dashboard/api/settings`

Current router configuration, toggleable settings, and registered node list with versions.

**Response:**

```json
{
  "router_version": "0.3.0",
  "router_hostname": "Neons-Mac-Studio",
  "config": {
    "toggles": { "auto_pull": true, "vram_fallback": true },
    "server": { "host": "Neons-Mac-Studio", "port": 11435, "data_dir": "~/.fleet-manager", "max_retries": 2 },
    "heartbeat": { "heartbeat_interval": 5.0, "heartbeat_timeout": 15.0, "heartbeat_offline": 30.0 },
    "scoring": { "score_model_hot": 50.0, "score_model_warm": 30.0, "..." : "..." },
    "rebalancer": { "rebalance_interval": 5.0, "rebalance_threshold": 4, "rebalance_max_per_cycle": 3 },
    "pre_warm": { "pre_warm_threshold": 3, "pre_warm_min_availability": 0.6 },
    "auto_pull_config": { "auto_pull_timeout": 300.0 },
    "context_protection": { "context_protection": "strip" }
  },
  "nodes": [
    {
      "node_id": "Neons-Mac-Studio",
      "status": "online",
      "agent_version": "0.3.0",
      "ip": "http://localhost:11434",
      "models_loaded_count": 2,
      "is_router": true
    }
  ]
}
```

### `POST /dashboard/api/settings`

Toggle runtime-mutable boolean settings. Only `auto_pull` and `vram_fallback` are allowed — all other fields are silently ignored. Changes take effect immediately but are ephemeral (env vars remain source of truth on restart).

**Request:**

```json
{"auto_pull": false}
```

**Response:**

```json
{"status": "updated", "updated": {"auto_pull": false}}
```

### `GET /dashboard/settings`

HTML page for the Settings dashboard tab. Shows router info, toggle switches, read-only configuration tables grouped by category, and a node list with version tracking.

---

## Platform Connection

Opt-in connection to the `gotomy.ai` coordination platform. Required for features like platform-wide usage telemetry (planned) and P2P capability advertisement (future). Local fleet routing works without a platform connection.

**Default:** not connected. No data leaves the node until the user explicitly connects and enables a feature.

### `GET /api/platform/status`

Returns the current platform connection state.

**Response (not connected):**
```json
{
  "state": "not_connected",
  "platform_url": "https://gotomy.ai",
  "connected": null,
  "features": {
    "telemetry_local_summary": false,
    "telemetry_include_tags": false,
    "p2p_serve": false
  },
  "error": null
}
```

**Response (connected):**
```json
{
  "state": "connected",
  "platform_url": "https://gotomy.ai",
  "connected": {
    "user_email": "user@example.com",
    "user_display_name": "User",
    "node_id": "3723887e-...",
    "connected_at": "2026-04-20T17:55:00Z"
  },
  "features": { ... },
  "error": null
}
```

### `POST /api/platform/connect`

Validate an operator token, register the node with the platform, and persist the connection state to `~/.fleet-manager/platform.json` (mode 0600).

**Request:**
```json
{
  "operator_token": "herd_...",
  "platform_url": "https://gotomy.ai",
  "node_name": "mac-studio-1",
  "region": "us-west"
}
```

Only `operator_token` is required. `platform_url` defaults to production. `node_name` defaults to hostname. `region` is optional.

**Success (200):**
```json
{
  "state": "connected",
  "node_id": "uuid-...",
  "user_email": "user@example.com",
  "user_display_name": "User",
  "platform_url": "https://gotomy.ai",
  "connected_at": "2026-04-20T17:55:00Z"
}
```

**Errors:**

| Status | Code | Cause |
|--------|------|-------|
| 400 | `invalid_token` | Token doesn't start with `herd_` or was rejected by platform |
| 400 | `registration_failed` | Platform rejected the registration for a non-auth reason |
| 502 | `platform_unreachable` | Platform returned 5xx or network failure |
| 500 | `internal_error` | Unexpected exception — see node logs |

### `POST /api/platform/disconnect`

Stop communicating with the platform and clear local connection state. Does NOT deregister the node from the platform — the node's earnings history and registration record survive so the user can reconnect from the same machine. To fully delete, use the platform dashboard.

**Request:** empty body.

**Response (always 200):**
```json
{"state": "not_connected"}
```

Idempotent — disconnecting when already disconnected is a no-op success.

### Privacy

Connecting alone does not transmit usage data. Features that send data to the platform (like `--telemetry-local-summary`) are separately opt-in. The connect flow itself sends only:
- The operator token (validates identity)
- The node's Ed25519 public key (signs future requests)
- A benchmark result (hardware class for routing decisions)
- The node name (default: hostname, editable)

Never transmitted by Connect: request history, prompt content, completion content, IP addresses of other nodes.

### Daily Usage Telemetry (opt-in)

When `FLEET_NODE_TELEMETRY_LOCAL_SUMMARY=true` (or `--telemetry-local-summary`) is set AND the node is connected to a platform, the agent POSTs a daily usage rollup to `{platform_url}/api/telemetry/local-summary` at ~00:05 UTC + jitter.

**Payload shape (structurally whitelisted — tests enforce no drift):**

```json
{
  "day": "2026-04-19",
  "node_id": "platform-issued-uuid",
  "agent_version": "0.5.2",
  "entries": [
    {
      "model": "gpt-oss:120b",
      "local_requests": 2500,
      "local_prompt_tokens": 150000,
      "local_completion_tokens": 450000,
      "p2p_served_requests": 0,
      "p2p_served_tokens": 0,
      "avg_latency_ms": 412.3,
      "p95_latency_ms": 1204.0
    }
  ]
}
```

**Never transmitted:** prompt text, completion text, request IDs, client IPs, error messages, score breakdowns, timestamps below day granularity. The rollup reads a whitelisted subset of columns only.

**Tags (second opt-in):** `FLEET_NODE_TELEMETRY_INCLUDE_TAGS=true` adds `request_count_by_tag: {"project:prod": 12, ...}` to each entry. Default off — tag values can be mildly identifying.

**Idempotency:** Platform returns 409 if the same (user, node, day) was already ingested. The node treats 409 as success. Local state file `~/.fleet-manager/telemetry_state.json` tracks the last successfully sent day.

**Retention:** Platform keeps rollups 90 days rolling.

---

## Static Assets

| Endpoint | Description |
|----------|-------------|
| `GET /favicon.svg` | SVG favicon (horse icon in brand purple) |
| `GET /favicon.ico` | Redirects to SVG favicon |
| `GET /` | Redirects to `/dashboard` |
