# API Reference

Every endpoint with request/response schemas, headers, error codes, and examples.

The router runs on port 11435 by default. All endpoints accept JSON bodies and return JSON responses.

## OpenAI-Compatible Endpoints

### `POST /v1/chat/completions`

Chat completions with streaming and non-streaming support.

**Request:**

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
  "metadata": {"tags": ["my-app"]},
  "user": "alice"
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `model` | string | required | Model name |
| `messages` | array | `[]` | Chat messages in OpenAI format |
| `stream` | boolean | `false` | Enable SSE streaming |
| `temperature` | float | `0.7` | Sampling temperature |
| `max_tokens` | integer | null | Maximum tokens to generate |
| `fallback_models` | array | `[]` | Backup models if primary unavailable |
| `metadata.tags` | array | `[]` | Tags for per-app analytics |
| `user` | string | null | Stored as `user:<value>` tag |

**Response headers:**

| Header | Description |
|--------|-------------|
| `X-Fleet-Node` | Node that handled the request |
| `X-Fleet-Score` | Winning routing score |
| `X-Fleet-Fallback` | Fallback model used (if applicable) |
| `X-Fleet-Retries` | Retry count (if retries occurred) |
| `X-Fleet-Context-Overflow` | Context overflow warning |
| `X-Thinking-Tokens` | Thinking tokens (chain-of-thought models, non-streaming) |
| `X-Output-Tokens` | Output tokens (chain-of-thought models, non-streaming) |
| `X-Budget-Used` | `completion_tokens/num_predict` budget check (non-streaming) |
| `X-Done-Reason` | `stop` (natural) or `length` (budget exhausted) |

**Errors:** `400` missing model, `404` model not found, `503` no node available.

**Example:**

```bash
curl http://localhost:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "llama3.3:70b", "messages": [{"role": "user", "content": "Hello!"}], "stream": false}'
```

---

### `POST /v1/images/generations`

OpenAI-compatible image generation.

**Request:**

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
| `model` | string | required | Image model name |
| `prompt` | string | required | Image description |
| `size` | string | `1024x1024` | Dimensions (`WIDTHxHEIGHT`) |
| `response_format` | string | `b64_json` | `b64_json` or `url` (raw PNG) |
| `steps` | integer | model default | Inference steps |
| `guidance` | float | model default | Guidance scale |
| `seed` | integer | random | Reproducibility seed |

**Errors:** `400` missing fields, `404` model unavailable, `502` generation failed, `503` disabled.

---

### `GET /v1/models`

List all models across the fleet (LLM, image, embedding).

**Response:**

```json
{
  "object": "list",
  "data": [
    {"id": "llama3.3:70b", "object": "model", "created": 1710000000, "owned_by": "ollama"},
    {"id": "z-image-turbo", "object": "model", "created": 1710000000, "owned_by": "mflux"}
  ]
}
```

---

### `POST /v1/embeddings`

OpenAI-compatible embeddings.

**Request:**

```json
{
  "model": "nomic-embed-text",
  "input": "The quick brown fox"
}
```

---

## Ollama-Compatible Endpoints

### `POST /api/chat`

Ollama-compatible chat. Streaming enabled by default (matches Ollama behavior).

**Request:**

```json
{
  "model": "llama3.3:70b",
  "messages": [{"role": "user", "content": "Hello!"}],
  "stream": true,
  "options": {"temperature": 0.7, "num_predict": 1024},
  "fallback_models": ["qwen2.5:32b"],
  "metadata": {"tags": ["my-app"]}
}
```

**Streaming response** (NDJSON):

```
{"message":{"role":"assistant","content":"Hello"},"done":false}
{"message":{"role":"assistant","content":"!"},"done":false}
{"message":{"role":"assistant","content":""},"done":true,"total_duration":1234567890}
```

The `metadata`, `fallback_models`, and `user` fields are stripped before proxying to Ollama. The `X-Herd-Tags` header is also supported.

---

### `POST /api/generate`

Ollama-compatible text generation. Uses `prompt` instead of `messages`.

```json
{
  "model": "llama3.3:70b",
  "prompt": "Why is the sky blue?",
  "stream": true
}
```

---

### `POST /api/pull`

Pull a model onto the fleet. Streams NDJSON progress matching Ollama's wire format.

**Request:**

```json
{
  "name": "codestral",
  "stream": true,
  "node_id": "mac-studio"
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | -- | Model to pull (Ollama standard) |
| `model` | string | -- | Model to pull (alias for `name`) |
| `stream` | bool | `true` | Stream progress or block until done |
| `node_id` | string | auto | Target node (auto-selects best if omitted) |

**Streaming response:**

```
{"status":"pulling manifest"}
{"status":"pulling abc123...","digest":"sha256:abc123","total":5000000,"completed":2500000}
{"status":"success"}
```

**Non-Ollama models** (mflux, DiffusionKit, MLX) return a `400` with install instructions instead of pulling.

**Errors:** `400` missing name or non-Ollama model, `404` node not found, `409` already pulling, `503` no node with enough memory.

**Examples:**

```bash
# Stream pull progress
curl -N http://localhost:11435/api/pull -d '{"name": "codestral"}'

# Pull to specific node
curl -N http://localhost:11435/api/pull -d '{"name": "llama3.3:70b", "node_id": "mac-studio"}'

# Block until done
curl http://localhost:11435/api/pull -d '{"name": "phi4", "stream": false}'
```

---

### `GET /api/tags`

List all models across the fleet with node information.

**Response:**

```json
{
  "models": [
    {
      "name": "llama3.3:70b",
      "size": 42949672960,
      "details": {"fleet_nodes": ["mac-studio-ultra", "macbook-pro-m4"]}
    },
    {
      "name": "z-image-turbo",
      "size": 0,
      "details": {"fleet_nodes": ["mac-studio-ultra"], "type": "image"}
    }
  ]
}
```

---

### `GET /api/ps`

List all currently loaded (hot) models.

**Response:**

```json
{
  "models": [
    {"name": "llama3.3:70b", "size": 42949672960, "fleet_node": "mac-studio-ultra"},
    {"name": "qwen2.5:7b", "size": 4294967296, "fleet_node": "macbook-air-m2"}
  ]
}
```

---

### `POST /api/embed` / `POST /api/embeddings`

Ollama-compatible embeddings. Both endpoints accept `input` or `prompt`.

```json
{
  "model": "nomic-embed-text",
  "input": "The quick brown fox"
}
```

**Errors:** `400` missing model, `404` model not found, `503` no node available.

---

### `GET /api/image-models`

List all image models (mflux, DiffusionKit, Ollama native).

```json
{
  "models": [
    {"name": "z-image-turbo", "type": "image", "backend": "mflux", "fleet_nodes": ["mac-studio"]},
    {"name": "x/z-image-turbo:latest", "type": "image", "backend": "ollama", "fleet_nodes": ["mac-studio"]}
  ]
}
```

---

## Fleet Management Endpoints

### `GET /fleet/status`

Full fleet state — nodes, models, queues, hardware.

```bash
curl -s http://localhost:11435/fleet/status | python3 -m json.tool
```

Returns `fleet` summary (totals), `nodes` array (per-node details), and `queues` object (per node:model queue state).

---

### `GET /fleet/queue`

Lightweight queue depths only. Designed for client-side backoff.

```bash
curl -s http://localhost:11435/fleet/queue | python3 -m json.tool
```

---

## Dashboard API Endpoints

All return JSON. Used by the web dashboard and available for external monitoring.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/dashboard/api/health` | GET | 15 automated health checks |
| `/dashboard/api/overview` | GET | Fleet totals (requests, nodes, models) |
| `/dashboard/api/traces` | GET | Recent request traces (`?limit=20`) |
| `/dashboard/api/usage` | GET | Per-node, per-model daily usage stats |
| `/dashboard/api/models` | GET | Per-model aggregates (`?days=7`) |
| `/dashboard/api/apps` | GET | Per-tag analytics (`?days=7`) |
| `/dashboard/api/apps/daily` | GET | Per-tag daily breakdown |
| `/dashboard/api/trends` | GET | Hourly request/latency trends (`?hours=24`) |
| `/dashboard/api/recommendations` | GET | Model mix recommendations per node |
| `/dashboard/api/settings` | GET | Current configuration |
| `/dashboard/api/settings` | POST | Update runtime settings |
| `/dashboard/api/model-management` | GET | Per-node model details |
| `/dashboard/api/pull` | POST | Pull model to node (dashboard use) |
| `/dashboard/api/delete` | POST | Delete model from node |
| `/dashboard/events` | GET | Server-Sent Events stream for real-time updates |

---

## Request Tagging

Tags can come from three sources (merged and deduplicated):

| Source | Example |
|--------|---------|
| `metadata.tags` in body | `{"metadata": {"tags": ["my-app"]}}` |
| `X-Herd-Tags` header | `X-Herd-Tags: my-app, production` |
| `user` field in body | `{"user": "alice"}` (stored as `user:alice`) |

---

## Common Response Headers

Every routed request includes:

| Header | Description |
|--------|-------------|
| `X-Fleet-Node` | Which node handled the request |
| `X-Fleet-Score` | The winning routing score |

These help with debugging — you can see exactly which node was chosen and why.

## Next Steps

- **[Integrations](integrations.md)** — How to connect your tools
- **[Deployment](deployment.md)** — Monitoring and log analysis
- **[Routing Engine](routing-engine.md)** — Understanding scoring decisions
