# Request Tagging & Per-App Analytics

Tag your requests to understand performance, load, and usage patterns across different applications, teams, or environments.

---

## Why Tag Requests?

If you have multiple applications querying the same Ollama Herd cluster — a coding assistant, a RAG pipeline, a chatbot, and an agent framework — you need visibility into how each one performs:

- Which app is consuming the most tokens?
- Is one project causing queue congestion?
- What's the error rate per application?
- How does latency compare across different workloads?

Ollama itself has no tagging support. Ollama Herd fills this gap.

---

## Competitive Landscape

How other platforms handle request tagging and per-app analytics:

| Platform | Mechanism | Analytics | Notes |
|----------|-----------|-----------|-------|
| **OpenRouter** | `HTTP-Referer` header + `X-Title` header | Per-app dashboard with cost, latency, usage | Most mature — built for multi-app routing |
| **LiteLLM** | `metadata.tags` in body or `x-litellm-tags` header | Spend tracking per tag, Langfuse/Helicone integration | Array of strings, flexible grouping |
| **OpenAI** | `user` field + `metadata` (Responses API only) | Limited — mostly for abuse detection | `metadata` only on newer Responses API |
| **Ollama** | None | None | No tagging, no per-app analytics |
| **Ollama Herd** | `metadata.tags` + `X-Herd-Tags` header + `user` field | Per-tag dashboard with latency, tokens, errors, trends | Inspired by LiteLLM's approach, adds header fallback |

### Design Decisions

- **`metadata.tags` in body** — follows LiteLLM's convention, which is becoming an industry pattern. Array of strings allows multi-dimensional tagging (e.g., `["project-alpha", "production", "rag-pipeline"]`).
- **`X-Herd-Tags` header** — for clients that can't easily modify the request body (proxies, middleware, load balancers). Comma-separated for simplicity.
- **`user` field** — already standard in OpenAI format. Stored as `user:<value>` tag so it appears in the same analytics without a separate dimension.
- **Tags are stripped before proxying** — the `metadata` and `fallback_models` fields are removed from the request body before forwarding to Ollama, so they never interfere with Ollama's processing.

---

## How to Tag Requests

Tags can come from three sources, which are merged and deduplicated:

### 1. Request Body — `metadata.tags`

The primary method. Add a `metadata` object with a `tags` array to your request body:

**OpenAI format:**

```bash
curl http://router-ip:11435/v1/chat/completions -d '{
  "model": "llama3.2:3b",
  "metadata": {"tags": ["my-app", "production"]},
  "messages": [{"role": "user", "content": "Hello!"}]
}'
```

**Ollama format:**

```bash
curl http://router-ip:11435/api/chat -d '{
  "model": "llama3.2:3b",
  "metadata": {"tags": ["my-app", "production"]},
  "messages": [{"role": "user", "content": "Hello!"}]
}'
```

**Python (OpenAI SDK):**

```python
from openai import OpenAI

client = OpenAI(base_url="http://router-ip:11435/v1", api_key="not-needed")
response = client.chat.completions.create(
    model="llama3.2:3b",
    messages=[{"role": "user", "content": "Hello!"}],
    extra_body={"metadata": {"tags": ["my-app", "production"]}},
)
```

**Python (requests):**

```python
import requests

requests.post("http://router-ip:11435/v1/chat/completions", json={
    "model": "llama3.2:3b",
    "metadata": {"tags": ["my-app", "production"]},
    "messages": [{"role": "user", "content": "Hello!"}],
})
```

### 2. HTTP Header — `X-Herd-Tags`

For clients that can't modify the request body, or when adding tags at the proxy/middleware layer:

```bash
curl -H "X-Herd-Tags: my-app, production" \
  http://router-ip:11435/v1/chat/completions -d '{
  "model": "llama3.2:3b",
  "messages": [{"role": "user", "content": "Hello!"}]
}'
```

Multiple tags are comma-separated. Whitespace is trimmed.

### 3. User Field

The standard OpenAI `user` field is automatically captured as a tag with `user:` prefix:

```bash
curl http://router-ip:11435/v1/chat/completions -d '{
  "model": "llama3.2:3b",
  "user": "alice",
  "messages": [{"role": "user", "content": "Hello!"}]
}'
```

This produces the tag `user:alice`.

### Combining Sources

All three sources are merged and deduplicated. This request:

```bash
curl -H "X-Herd-Tags: production" \
  http://router-ip:11435/v1/chat/completions -d '{
  "model": "llama3.2:3b",
  "user": "alice",
  "metadata": {"tags": ["my-app", "production"]},
  "messages": [{"role": "user", "content": "Hello!"}]
}'
```

Produces tags: `["my-app", "production", "user:alice"]` (deduplicated — `production` appears only once).

---

## Tagging Strategies

### By Application

The most common pattern — identify which software is making requests:

```json
{"metadata": {"tags": ["coding-assistant"]}}
{"metadata": {"tags": ["rag-pipeline"]}}
{"metadata": {"tags": ["chatbot"]}}
{"metadata": {"tags": ["agent-framework"]}}
```

### By Environment

Track production vs development usage:

```json
{"metadata": {"tags": ["my-app", "production"]}}
{"metadata": {"tags": ["my-app", "staging"]}}
{"metadata": {"tags": ["my-app", "dev"]}}
```

### By Feature

Understand which features within an app drive the most load:

```json
{"metadata": {"tags": ["my-app", "code-review"]}}
{"metadata": {"tags": ["my-app", "summarization"]}}
{"metadata": {"tags": ["my-app", "embedding"]}}
```

### By Team

Per-team usage tracking:

```json
{"metadata": {"tags": ["team-backend"]}}
{"metadata": {"tags": ["team-ml"]}}
```

---

## Analytics Dashboard

The **Apps** tab on the dashboard (`/dashboard/apps`) provides per-tag analytics:

### Summary Cards

- **Tagged Requests** — total requests that included tags
- **Unique Tags** — number of distinct tags seen
- **Top Tag** — the most-used tag

### Charts

- **Requests by Tag** — bar chart comparing request volume per tag
- **Tag Activity Over Time** — line chart showing daily request trends per tag

### Per-Tag Table

Each row shows:

| Column | Description |
|--------|-------------|
| Tag | The tag string |
| Requests | Total request count |
| Avg Latency | Average total latency in ms |
| Avg TTFT | Average time to first token in ms |
| Total Tokens | Combined prompt + completion tokens |
| Error Rate | Percentage of failed requests |

Click any row to see a daily breakdown for that tag.

---

## API Endpoints

### `GET /dashboard/api/apps`

Per-tag aggregated stats.

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

### `GET /dashboard/api/apps/daily`

Per-tag, per-day breakdown.

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

## Trace Integration

Tags appear in the per-request trace data. Every trace record in the response from `/dashboard/api/traces` includes a `tags` field:

```json
{
  "request_id": "abc12345-def6-7890",
  "model": "llama3.3:70b",
  "node_id": "mac-studio-ultra",
  "tags": ["my-app", "production"],
  "status": "completed",
  "latency_ms": 4200.5,
  "..."
}
```

### Querying Traces by Tag

The trace data is stored in SQLite (`~/.fleet-manager/latency.db`). You can query directly:

```bash
# Find all traces for a specific tag
sqlite3 ~/.fleet-manager/latency.db \
  "SELECT request_id, model, latency_ms, tags FROM request_traces
   WHERE tags LIKE '%my-app%' ORDER BY timestamp DESC LIMIT 10"

# Count requests per tag (using json_each)
sqlite3 ~/.fleet-manager/latency.db \
  "SELECT j.value AS tag, COUNT(*) AS requests
   FROM request_traces, json_each(request_traces.tags) AS j
   WHERE tags IS NOT NULL
   GROUP BY j.value ORDER BY requests DESC"
```

---

## Storage

Tags are stored as a JSON array string in the `tags` column of the `request_traces` table in `~/.fleet-manager/latency.db`. The column is added automatically via schema migration on first startup after upgrading.

An index (`idx_traces_tags`) is created on the `tags` column for efficient filtering.

Requests without tags have `NULL` in the `tags` column and are excluded from the Apps dashboard analytics.

---

## Framework Integration Examples

### LangChain

```python
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(
    base_url="http://router-ip:11435/v1",
    model="llama3.3:70b",
    api_key="none",
    model_kwargs={"metadata": {"tags": ["langchain-app"]}},
)
```

### CrewAI

```python
from crewai import LLM

# Use X-Herd-Tags header via extra_headers
llm = LLM(
    model="ollama/llama3.3:70b",
    base_url="http://router-ip:11435",
    extra_headers={"X-Herd-Tags": "crewai-agents"},
)
```

### curl with Header

```bash
# When you can't modify the body (e.g., piping from another tool)
curl -H "X-Herd-Tags: my-pipeline, batch-job" \
  http://router-ip:11435/api/generate -d '{
  "model": "llama3.2:3b",
  "prompt": "Summarize this document..."
}'
```
