# Request Tagging & Analytics Guide

Tag your LLM requests so you can answer questions like "which bot is burning the most tokens?" and "why did latency spike at 3am?" without digging through logs.

## The problem

You have multiple scripts, bots, and agents all hitting the same Ollama Herd router. The dashboard shows total requests and latency, but you can't tell which project is responsible for what. When something breaks or gets slow, you're guessing.

## The fix: add one line

Add `metadata.tags` to your request body. That's it. No config, no setup, no schema changes.

### Python (OpenAI SDK)

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:11435/v1", api_key="not-needed")

response = client.chat.completions.create(
    model="gpt-oss:120b",
    messages=[{"role": "user", "content": "Hello"}],
    extra_body={"metadata": {"tags": ["instamolt", "caption-gen"]}},
)
```

### Python (httpx/requests)

```python
resp = httpx.post("http://localhost:11435/api/chat", json={
    "model": "gpt-oss:120b",
    "messages": [{"role": "user", "content": "Hello"}],
    "metadata": {"tags": ["silk-road", "product-review"]},
})
```

### JavaScript

```javascript
const resp = await fetch("http://localhost:11435/v1/chat/completions", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    model: "gpt-oss:120b",
    messages: [{ role: "user", content: "Hello" }],
    metadata: { tags: ["my-agent", "reasoning"] },
  }),
});
```

### HTTP header (when you can't modify the body)

```bash
curl -H "X-Herd-Tags: my-script, batch-job" \
  http://localhost:11435/api/chat \
  -d '{"model":"gpt-oss:120b","messages":[{"role":"user","content":"Hello"}]}'
```

### User field (automatic)

If your request includes the standard OpenAI `user` field, it's captured automatically as `user:<value>`:

```json
{"model": "gpt-oss:120b", "user": "alice", "messages": [...]}
```

This produces the tag `user:alice` without any extra setup.

## What to tag

Tags are just strings. Use whatever makes sense for your project. Here are patterns that work well:

### By project/bot

The most useful pattern — one tag per project:

```python
# Each bot identifies itself
{"metadata": {"tags": ["instamolt"]}}
{"metadata": {"tags": ["silk-road"]}}
{"metadata": {"tags": ["drift-experiences"]}}
{"metadata": {"tags": ["openclaw-agent"]}}
```

### By task type

Understand what kinds of work are hitting the fleet:

```python
{"metadata": {"tags": ["instamolt", "caption-gen"]}}
{"metadata": {"tags": ["instamolt", "engagement-decision"]}}
{"metadata": {"tags": ["instamolt", "comment-reply"]}}
{"metadata": {"tags": ["silk-road", "product-review"]}}
{"metadata": {"tags": ["silk-road", "seo-content"]}}
```

### By environment

Track dev vs production usage:

```python
{"metadata": {"tags": ["instamolt", "production"]}}
{"metadata": {"tags": ["instamolt", "dev"]}}
```

### Combine freely

Tags are an array — use multiple dimensions:

```python
{"metadata": {"tags": ["instamolt", "caption-gen", "production", "batch-3"]}}
```

All three sources (body, header, user field) merge and deduplicate automatically.

## Seeing the results

### Dashboard: Apps tab

Open `http://localhost:11435/dashboard` and click **Apps**. You'll see:

- **Request volume per tag** — which projects are making the most calls
- **Average latency per tag** — is one project's workload slower than others?
- **Token usage per tag** — who's burning through tokens
- **Error rate per tag** — is one project failing more than others?
- **Daily trends** — how usage patterns change over time

### API: per-tag stats

```bash
# Last 7 days of per-tag analytics
curl -s http://localhost:11435/dashboard/api/apps | python3 -m json.tool
```

Returns per-tag: request count, avg latency, avg TTFT, total tokens, error rate.

```bash
# Daily breakdown per tag
curl -s http://localhost:11435/dashboard/api/apps/daily | python3 -m json.tool
```

Returns per-tag per-day: request count, avg latency, total tokens.

### SQLite: custom queries

Tags are stored in the `request_traces` table. Run custom queries:

```bash
# Which tags generate the most tokens?
sqlite3 ~/.fleet-manager/latency.db "
SELECT j.value AS tag,
       COUNT(*) AS requests,
       SUM(COALESCE(completion_tokens,0)) AS tokens_out,
       ROUND(AVG(latency_ms)/1000.0, 1) AS avg_secs
FROM request_traces, json_each(request_traces.tags) AS j
WHERE tags IS NOT NULL
GROUP BY j.value
ORDER BY tokens_out DESC
"

# Error rate by tag
sqlite3 ~/.fleet-manager/latency.db "
SELECT j.value AS tag,
       COUNT(*) AS total,
       SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
       ROUND(100.0 * SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) / COUNT(*), 1) AS fail_pct
FROM request_traces, json_each(request_traces.tags) AS j
WHERE tags IS NOT NULL
GROUP BY j.value
ORDER BY fail_pct DESC
"

# Latency comparison: which tag is slowest?
sqlite3 ~/.fleet-manager/latency.db "
SELECT j.value AS tag,
       ROUND(AVG(latency_ms)/1000.0, 1) AS avg_secs,
       ROUND(AVG(time_to_first_token_ms), 0) AS avg_ttft_ms,
       COUNT(*) AS n
FROM request_traces, json_each(request_traces.tags) AS j
WHERE tags IS NOT NULL AND status='completed'
GROUP BY j.value
HAVING n > 10
ORDER BY avg_secs DESC
"

# Hourly usage pattern per tag (find peak times)
sqlite3 ~/.fleet-manager/latency.db "
SELECT j.value AS tag,
       CAST((timestamp % 86400) / 3600 AS INTEGER) AS hour,
       COUNT(*) AS requests
FROM request_traces, json_each(request_traces.tags) AS j
WHERE tags IS NOT NULL
GROUP BY j.value, hour
ORDER BY tag, hour
"
```

## Framework integration

### LangChain

```python
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(
    base_url="http://localhost:11435/v1",
    model="gpt-oss:120b",
    api_key="none",
    model_kwargs={"metadata": {"tags": ["langchain-app", "rag-pipeline"]}},
)
```

### CrewAI

```python
from crewai import LLM

llm = LLM(
    model="ollama/gpt-oss:120b",
    base_url="http://localhost:11435",
    extra_headers={"X-Herd-Tags": "crewai-agents,production"},
)
```

### OpenClaw agents

If your agent uses the OpenAI SDK pattern:

```python
# In your agent's LLM call
response = client.chat.completions.create(
    model="gpt-oss:120b",
    messages=messages,
    extra_body={"metadata": {"tags": ["my-agent-name", "task-type"]}},
)
```

If your agent uses curl/httpx directly, add the `X-Herd-Tags` header:

```python
headers = {
    "Content-Type": "application/json",
    "X-Herd-Tags": "my-agent-name,task-type",
}
```

## How it works under the hood

1. **Tag extraction** — The router extracts tags from `metadata.tags` (body), `X-Herd-Tags` (header), and `user` (body). All three sources merge and deduplicate.

2. **Tag stripping** — The `metadata` field is stripped from the body before forwarding to Ollama, so Ollama never sees it. Your requests work exactly the same with or without tags.

3. **Trace storage** — Tags are stored as a JSON array in the `tags` column of the `request_traces` table in SQLite (`~/.fleet-manager/latency.db`). An index on the column enables efficient queries.

4. **Dashboard** — The Apps tab queries the trace store, aggregates by tag, and renders charts and tables. Auto-refreshes.

## Tips

- **Tag early** — Add tags when you first integrate with Herd, not after you need to debug something. Historical data is only available for tagged requests.

- **Be consistent** — Pick a naming convention and stick with it. `instamolt` everywhere, not `instamolt` in one place and `insta-molt` in another.

- **Tag the task, not just the project** — `["instamolt", "caption-gen"]` is more useful than just `["instamolt"]`. When caption generation is slow, you'll know immediately.

- **Don't over-tag** — 2-3 tags per request is the sweet spot. Tags like `["instamolt", "caption-gen", "production", "gpt-oss", "batch-7", "tuesday"]` create noise in the analytics.

- **Use the header for middleware** — If you have a proxy or API gateway in front of Herd, add `X-Herd-Tags` there instead of modifying every client.

## Troubleshooting

| Issue | Fix |
|-------|-----|
| Tags not showing in Apps tab | Verify `metadata.tags` is in the request body (check with `curl -v`) |
| Old requests missing tags | Tags only appear on requests made after the feature was added. Can't retroactively tag. |
| `json_each` query fails | Your SQLite version may be too old. Requires SQLite 3.33+ (2020). |
| Tags showing as `null` | The request didn't include `metadata.tags`, `X-Herd-Tags`, or `user` field |
| Duplicate tags | Tags are deduplicated per-request. If you see duplicates in analytics, they came from different requests. |
