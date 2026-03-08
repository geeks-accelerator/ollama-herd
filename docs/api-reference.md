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
  "fallback_models": ["qwen2.5:32b", "qwen2.5:7b"]
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

**Error responses:**

| Status | Condition |
|--------|-----------|
| 400 | Missing `model` field |
| 404 | Model not found on any node |
| 503 | Model exists but no node can serve it right now |

---

### `GET /v1/models`

List all models across the fleet (loaded + available on disk).

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
  "fallback_models": ["qwen2.5:32b"]
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

Response format is the same as `/api/chat`.

---

### `GET /api/tags`

List all models across the fleet with node information.

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
    }
  ]
}
```

The `fleet_nodes` array shows which nodes have each model (loaded or available on disk). The `size` field is in bytes.

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

## Dashboard

### HTML Pages

| Endpoint | Description |
|----------|-------------|
| `GET /dashboard` | Fleet Overview — live node status, CPU/memory, loaded models, queue depths |
| `GET /dashboard/trends` | Historical charts — requests/hour, average latency, token throughput |
| `GET /dashboard/models` | Model Insights — per-model latency, tokens/sec, usage comparison |

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
      "timestamp": 1710000000.0
    }
  ]
}
```

---

## Static Assets

| Endpoint | Description |
|----------|-------------|
| `GET /favicon.svg` | SVG favicon (horse icon in brand purple) |
| `GET /favicon.ico` | Redirects to SVG favicon |
| `GET /` | Redirects to `/dashboard` |
