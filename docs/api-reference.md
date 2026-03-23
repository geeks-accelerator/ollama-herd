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

**Error responses:**

| Status | Condition |
|--------|-----------|
| 400 | Missing `model` field |
| 404 | Model not found on any node (auto-pull attempted first if `FLEET_AUTO_PULL=true`) |
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
| `GET /dashboard/apps` | Apps — per-tag analytics with latency, tokens, errors, daily trends |
| `GET /dashboard/benchmarks` | Benchmarks — capacity growth charts, per-run results |
| `GET /dashboard/health` | Health — 11 automated fleet health checks with severity and recommendations |
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
  "router_version": "0.2.0",
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
      "agent_version": "0.2.0",
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

## Static Assets

| Endpoint | Description |
|----------|-------------|
| `GET /favicon.svg` | SVG favicon (horse icon in brand purple) |
| `GET /favicon.ico` | Redirects to SVG favicon |
| `GET /` | Redirects to `/dashboard` |
