# Request Tagging and Apps Analytics

**Status**: Implemented
**Date**: March 2026
**Related docs**: [`request-tagging.md`](../request-tagging.md), [`api-reference.md`](../api-reference.md)

## Problem

Multiple projects query Ollama through the herd router, but there was no way to distinguish which application generated which requests. Ollama has zero metadata support, so the router is the natural place to intercept and store per-app tags for analytics.

## Design Decisions

### Tag capture from 3 sources (merged, deduplicated)

1. **Body field** (`metadata.tags`): Standard list of strings in the request body
2. **Header fallback** (`X-Herd-Tags`): Comma-separated header, useful when the body can't be modified
3. **User field** (`user`): OpenAI-standard field, stored as `user:<value>` tag

Tags are stripped before proxying to Ollama since it doesn't understand them. Industry conventions from OpenRouter, LiteLLM, and OpenAI all converge on the `metadata.tags` pattern.

### Storage

Tags are stored as JSON in the `request_traces` SQLite table. Queries use SQLite's `json_each()` to explode tags for aggregation:

```sql
SELECT j.value as tag, COUNT(*) as request_count
FROM request_traces, json_each(request_traces.tags) as j
WHERE timestamp >= ? AND tags IS NOT NULL
GROUP BY j.value ORDER BY request_count DESC
```

## Implementation

### Files modified

| File | Change |
|------|--------|
| `models/request.py` | Added `tags: list[str]` field to `InferenceRequest` |
| `server/routes/openai_compat.py` | Extract tags from body + headers |
| `server/routes/ollama_compat.py` | Extract tags from body + headers |
| `server/routes/routing.py` | `extract_tags()` helper merging all 3 sources |
| `server/trace_store.py` | `tags` column, `get_usage_by_tag()`, `get_tag_daily_stats()` |
| `server/streaming.py` | Pass tags to trace store, strip from Ollama body |
| `server/routes/dashboard.py` | `/dashboard/apps` page with charts and tables |

### Dashboard

The Apps page (`/dashboard/apps`) provides:
- Summary cards: total tagged requests, unique tags, top tag
- Bar chart: requests per tag
- Line chart: tag activity over time (daily)
- Table: per-tag breakdown with request count, avg latency, tokens, error rate

### Client usage

**Python (OpenAI SDK):**
```python
client = OpenAI(base_url="http://router:11435/v1", api_key="not-needed")
response = client.chat.completions.create(
    model="llama3.2:3b",
    messages=[{"role": "user", "content": "Hello"}],
    extra_body={"metadata": {"tags": ["my-project"]}}
)
```

**curl with header:**
```bash
curl http://router:11435/api/chat \
  -H "X-Herd-Tags: my-project, production" \
  -d '{"model": "llama3.2:3b", "messages": [...]}'
```

**curl with body:**
```bash
curl http://router:11435/api/chat -d '{
  "model": "llama3.2:3b",
  "messages": [...],
  "metadata": {"tags": ["my-project"]}
}'
```

## Configuration

No configuration needed. Tags are optional and backward-compatible. Requests without tags work exactly as before.

The `FLEET_` env prefix does not apply to tagging since it's a request-level feature, not a server-level setting.
