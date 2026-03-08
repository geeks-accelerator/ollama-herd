# Operations Guide

How to run, monitor, debug, and tune Ollama Herd in production.

---

## Structured Logging (JSONL)

Every log event is written as a single-line JSON object to `~/.fleet-manager/logs/herd.jsonl`. The file rotates daily at midnight UTC with 30-day retention.

### Log format

```json
{
  "ts": "2026-03-08T01:24:28.685Z",
  "level": "INFO",
  "logger": "fleet_manager.server.app",
  "msg": "Ollama Herd ready on port 11435"
}
```

Contextual fields are added when available:

```json
{
  "ts": "2026-03-08T01:25:03.421Z",
  "level": "WARNING",
  "logger": "fleet_manager.server.scorer",
  "msg": "All 3 nodes eliminated for model 'deepseek-r1:671b'",
  "model": "deepseek-r1:671b",
  "request_id": "abc123"
}
```

### Controlling log levels

| Variable | Default | Effect |
|----------|---------|--------|
| `FLEET_LOG_LEVEL` | `DEBUG` | What gets written to the JSONL file |
| `FLEET_CONSOLE_LOG_LEVEL` | `INFO` | What gets printed to the terminal |

Set `FLEET_LOG_LEVEL=INFO` in production to reduce JSONL file size. Set `FLEET_CONSOLE_LOG_LEVEL=DEBUG` for verbose terminal output during development.

### Reading logs

```bash
# Tail the live log
tail -f ~/.fleet-manager/logs/herd.jsonl | python3 -m json.tool

# Find all warnings and errors
grep -E '"level": "(WARNING|ERROR)"' ~/.fleet-manager/logs/herd.jsonl

# Find all events for a specific model
grep '"model": "llama3.3:70b"' ~/.fleet-manager/logs/herd.jsonl

# Count errors by logger
grep '"level": "ERROR"' ~/.fleet-manager/logs/herd.jsonl | \
  python3 -c "import sys,json; from collections import Counter; \
  c=Counter(json.loads(l)['logger'] for l in sys.stdin); \
  print('\n'.join(f'{v:4d} {k}' for k,v in c.most_common()))"
```

### What gets logged

| Component | Level | What |
|-----------|-------|------|
| Route handlers | INFO | Every incoming request (model, format, stream flag) |
| Route handlers | WARNING | Routing failure (no nodes available) |
| Scorer | DEBUG | Each elimination reason per node |
| Scorer | WARNING | All nodes eliminated for a model |
| Queue manager | DEBUG | Enqueue, worker start, completed, failed |
| Streaming proxy | WARNING | Malformed JSON from Ollama, HTTP error bodies |
| Streaming proxy | ERROR | Background task failures (latency recording, trace recording) |
| Rebalancer | DEBUG | Pre-warm triggers, rebalance decisions |
| Holding queue | INFO | Request entering hold, hold timeout |
| Node agent | WARNING | Router connection failures, Ollama health failures |
| Capacity learner | INFO | Mode changes, availability score transitions |

---

## Per-Request Traces

Every routing decision is recorded in SQLite (`~/.fleet-manager/latency.db`) with full detail.

### Trace fields

| Field | Description |
|-------|-------------|
| `tags` | JSON array of tags from `metadata.tags`, `X-Herd-Tags` header, and `user` field |
| `request_id` | Unique ID for the request |
| `model` | Model that was actually used (may differ from requested if fallback triggered) |
| `original_model` | Model the client originally requested |
| `node_id` | Node that handled the request |
| `score` | Routing score that won |
| `scores_breakdown` | JSON object with individual signal scores |
| `status` | `completed`, `failed`, or `retried` |
| `latency_ms` | Total time from request to response complete |
| `time_to_first_token_ms` | Time until first response chunk |
| `prompt_tokens` | Tokens in the prompt (from Ollama) |
| `completion_tokens` | Tokens generated (from Ollama) |
| `retry_count` | Number of retries before success |
| `fallback_used` | `1` if a fallback model was used, `0` otherwise |
| `excluded_nodes` | JSON list of nodes excluded from scoring (failed retries) |
| `client_ip` | Client's IP address |
| `original_format` | `openai` or `ollama` |
| `error_message` | Error details if status is `failed` |

### Accessing traces

Via the dashboard API:

```bash
# Last 20 traces
curl -s http://localhost:11435/dashboard/api/traces?limit=20

# Usage stats (per-node, per-model, per-day)
curl -s http://localhost:11435/dashboard/api/usage

# Model insights (per-model aggregates)
curl -s http://localhost:11435/dashboard/api/models

# Overview totals
curl -s http://localhost:11435/dashboard/api/overview
```

Via the dashboard UI: open `http://localhost:11435` and navigate to the **Model Insights** tab.

---

## Request Tagging & Per-App Analytics

Tag requests to track performance and usage per application, team, or environment.

### Adding tags

Tags can come from three sources (merged and deduplicated):

| Source | Example | Notes |
|--------|---------|-------|
| `metadata.tags` in body | `{"metadata": {"tags": ["my-app"]}}` | Primary method — array of strings |
| `X-Herd-Tags` header | `X-Herd-Tags: my-app, prod` | Comma-separated, for proxies/middleware |
| `user` field in body | `{"user": "alice"}` | Stored as `user:alice` tag |

### Accessing per-app analytics

Via the dashboard API:

```bash
# Per-tag stats (last 7 days)
curl -s http://localhost:11435/dashboard/api/apps?days=7

# Per-tag daily breakdown
curl -s http://localhost:11435/dashboard/api/apps/daily?days=7
```

Via the dashboard UI: open `http://localhost:11435` and navigate to the **Apps** tab.

### Querying tags in SQLite

Tags are stored as a JSON array in the `tags` column of `request_traces`:

```bash
# Requests per tag
sqlite3 ~/.fleet-manager/latency.db \
  "SELECT j.value AS tag, COUNT(*) AS requests
   FROM request_traces, json_each(request_traces.tags) AS j
   WHERE tags IS NOT NULL GROUP BY j.value ORDER BY requests DESC"

# Average latency per tag
sqlite3 ~/.fleet-manager/latency.db \
  "SELECT j.value AS tag, ROUND(AVG(latency_ms), 1) AS avg_ms
   FROM request_traces, json_each(request_traces.tags) AS j
   WHERE tags IS NOT NULL GROUP BY j.value ORDER BY avg_ms DESC"

# Error rate per tag
sqlite3 ~/.fleet-manager/latency.db \
  "SELECT j.value AS tag,
     ROUND(100.0 * SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) / COUNT(*), 1) AS error_pct
   FROM request_traces, json_each(request_traces.tags) AS j
   WHERE tags IS NOT NULL GROUP BY j.value"
```

For the full tagging guide, strategies, and framework integration examples, see [Request Tagging](request-tagging.md).

---

## Model Fallbacks

Clients can specify backup models in case the primary model has no available node.

### Request format

```bash
# OpenAI format
curl http://localhost:11435/v1/chat/completions -d '{
  "model": "llama3.3:70b",
  "fallback_models": ["qwen2.5:32b", "qwen2.5:7b"],
  "messages": [{"role": "user", "content": "Hello"}]
}'

# Ollama format
curl http://localhost:11435/api/chat -d '{
  "model": "llama3.3:70b",
  "fallback_models": ["qwen2.5:32b", "qwen2.5:7b"],
  "messages": [{"role": "user", "content": "Hello"}]
}'
```

### How fallback works

1. Router tries to score all nodes for the primary model (`llama3.3:70b`)
2. If no node passes elimination, tries the first fallback (`qwen2.5:32b`)
3. If that also fails, tries the next fallback (`qwen2.5:7b`)
4. If a model exists on a node but all nodes are busy, the request enters a **holding queue** — it waits up to 30 seconds, retrying every 2 seconds, before moving to the next fallback
5. If all models exhausted, returns a 503 error with details

### Trace visibility

When a fallback is used, the trace records both `original_model` (what was requested) and `model` (what was actually used), with `fallback_used: 1`.

---

## Auto-Retry

When a node fails before the first response chunk, the router automatically retries on the next-best node.

### Retryable errors

| Error | Retried? |
|-------|----------|
| Connection refused / timeout | Yes |
| Read timeout | Yes |
| Remote protocol error | Yes |
| HTTP 5xx from Ollama | Yes |
| HTTP 4xx from Ollama | No (client error) |
| Failure after first chunk sent | No (stream already started) |

### How retry works

1. Request streams from Node A
2. Node A fails before the first chunk
3. Router adds Node A to the `excluded_nodes` list
4. Router re-scores all remaining nodes (excluding Node A)
5. Routes to the next-best node (Node B)
6. Up to `FLEET_MAX_RETRIES` (default: 2) attempts

Each failed attempt is recorded as a `retried` trace. The successful attempt is recorded as `completed` with `retry_count` reflecting total retries.

---

## Ollama Auto-Start and Health Recovery

The node agent automatically manages the local Ollama process.

### On startup

If Ollama is not reachable at the configured host:
1. Agent looks for the `ollama` binary in PATH
2. Starts `ollama serve` as a detached background process
3. Waits up to 30 seconds for Ollama to become healthy
4. If it doesn't start, the agent exits with an error

### During runtime

If Ollama fails 3 consecutive health checks:
1. Agent logs a warning
2. Attempts to restart Ollama using the same startup procedure
3. Resets the failure counter

This handles cases where Ollama crashes or is killed externally.

---

## Dynamic Queue Concurrency

Worker count per `node:model` queue is automatically calculated from available memory.

### Formula

```
headroom = available_memory_gb - model_size_gb
concurrency = headroom / 2.0   (2 GB estimated KV cache per request)
clamped to [1, 8]
```

### Example

A Mac Studio with 512GB total, 60GB used by system, running `llama3.3:70b` (40GB):
```
headroom = (512 - 60) - 40 = 412 GB
concurrency = 412 / 2 = 206 → clamped to 8
```

A MacBook Air with 16GB total, 6GB used by system, running `qwen2.5:7b` (5GB):
```
headroom = (16 - 6) - 5 = 5 GB
concurrency = 5 / 2 = 2
```

Concurrency is recalculated each time work is enqueued, so it adapts as memory conditions change.

---

## Graceful Drain

When a node agent receives SIGTERM or SIGINT:

1. Saves capacity learner state to disk (if enabled)
2. Sends a drain heartbeat (`draining: true`) to the router
3. The router marks the node as draining — no new requests are routed to it
4. In-flight requests complete normally
5. Pending requests in the node's queues are redistributed by the rebalancer
6. Agent shuts down cleanly

### Triggering a drain

```bash
# Graceful (sends drain signal)
kill <herd-node-pid>

# Or Ctrl+C in the terminal
```

---

## Pre-Warm

When the winning node's queue exceeds `FLEET_PRE_WARM_THRESHOLD` (default: 3), the router proactively loads the model on the runner-up node.

### How it works

1. After every routing decision, check if winner's queue depth >= threshold
2. If the runner-up node doesn't have the model hot, and its availability >= `FLEET_PRE_WARM_MIN_AVAILABILITY`
3. Send an empty `/api/generate` request to the runner-up's Ollama with `keep_alive: 10m`
4. A lock prevents duplicate pre-warm requests for the same model on the same node
5. The lock releases when the model reports as loaded

Pre-warm means that by the time the router starts sending overflow requests to the runner-up, the model is already loaded — no cold-start latency.

---

## Streaming Format Conversion

The router transparently converts between Ollama and OpenAI streaming formats.

### OpenAI format (SSE)

Clients hitting `/v1/chat/completions` receive Server-Sent Events:
```
data: {"id":"chatcmpl-xxx","choices":[{"delta":{"content":"Hello"}}]}\n\n
data: {"id":"chatcmpl-xxx","choices":[{"delta":{},"finish_reason":"stop"}]}\n\n
data: [DONE]\n\n
```

### Ollama format (NDJSON)

Clients hitting `/api/chat` receive newline-delimited JSON:
```json
{"message":{"role":"assistant","content":"Hello"},"done":false}
{"message":{"role":"assistant","content":""},"done":true,"total_duration":1234}
```

The proxy handles conversion internally. Token counts are extracted from Ollama's final chunk (`prompt_eval_count`, `eval_count`) and included in OpenAI-format usage responses.

---

## Data Storage

All persistent data lives in `~/.fleet-manager/` (configurable via `FLEET_DATA_DIR`):

```
~/.fleet-manager/
  latency.db          # SQLite: latency history + request traces + usage stats
  logs/
    herd.jsonl        # Structured logs (daily rotation, 30-day retention)
  capacity-learner-{node-id}.json  # Learned behavioral data (per node)
```

The SQLite database uses WAL mode for concurrent read/write access.

---

## Troubleshooting

For common issues (LAN connectivity, "model not found" errors, node status problems, meeting detector false positives, and a debug checklist), see the dedicated [Troubleshooting Guide](troubleshooting.md).
