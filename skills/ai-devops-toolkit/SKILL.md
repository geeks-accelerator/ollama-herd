---
name: ai-devops-toolkit
description: Operational tooling for teams running local LLM infrastructure. Request tracing with full scoring breakdowns, per-application usage analytics via request tagging, automated health checks with severity levels, latency percentile tracking, error rate monitoring, and capacity planning via model recommendations. SQLite-backed observability — no Prometheus, no Grafana, no external dependencies.
version: 1.0.0
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"wrench","requires":{"anyBins":["curl","sqlite3"],"optionalBins":["python3","pip"],"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"]},"os":["darwin","linux"]}}
---

# AI DevOps Toolkit

Operational tooling for running local LLM inference at production quality. This skill provides the observability, tracing, and health monitoring layer for an Ollama Herd fleet.

## Prerequisites

```bash
pip install ollama-herd
herd              # start the router (exposes all observability endpoints)
herd-node         # start on each monitored node
```

Package: [`ollama-herd`](https://pypi.org/project/ollama-herd/) | Repo: [github.com/geeks-accelerator/ollama-herd](https://github.com/geeks-accelerator/ollama-herd)

## Scope

This toolkit assumes you have an Ollama Herd router running at `http://localhost:11435` with one or more node agents reporting in. It focuses on the operational side: are requests succeeding? what's slow? which apps consume the most tokens? are nodes healthy? is capacity adequate?

## Observability stack

Everything is backed by SQLite at `~/.fleet-manager/latency.db`. No external databases, no time-series infrastructure. Query with standard `sqlite3`.

```
~/.fleet-manager/
├── latency.db          # Request traces, latency history, usage stats
└── logs/
    └── herd.jsonl      # Structured logs, daily rotation, 30-day retention
```

## Health checks

### Automated fleet health analysis
```bash
curl -s http://localhost:11435/dashboard/api/health | python3 -m json.tool
```

Eleven checks, each returning a severity (info/warning/critical) and recommendation:

| Check | What it detects |
|-------|----------------|
| Offline nodes | Nodes that stopped sending heartbeats |
| Degraded nodes | Nodes reporting errors or high memory pressure |
| Memory pressure | Nodes approaching memory limits |
| Underutilized nodes | Healthy nodes not receiving traffic |
| VRAM fallbacks | Requests rerouted to loaded alternatives to avoid cold loads |
| Version mismatch | Nodes running different versions than the router |
| Context protection | num_ctx values stripped or models upgraded to prevent reloads |
| Zombie reaper | Stuck in-flight requests cleaned up |
| Model thrashing | Models loading/unloading frequently (memory contention) |
| Request timeouts | Requests exceeding expected latency thresholds |
| Error rates | Elevated failure rates per model or per node |

### Node-level status
```bash
curl -s http://localhost:11435/fleet/status | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f\"Fleet: {d['fleet']['nodes_online']}/{d['fleet']['nodes_total']} online, {d['fleet']['requests_active']} active requests\")
for n in d['nodes']:
    mem = n.get('memory', {})
    cpu = n.get('cpu', {})
    print(f\"  {n['node_id']:20s} {n['status']:10s} CPU={cpu.get('utilization_pct',0):.0f}% MEM={mem.get('used_gb',0):.0f}/{mem.get('total_gb',0):.0f}GB pressure={mem.get('pressure','?')}\")
"
```

## Request tracing

Every routing decision is recorded with full context.

### Recent traces
```bash
curl -s "http://localhost:11435/dashboard/api/traces?limit=20" | python3 -m json.tool
```

Each trace includes: request_id, model, original_model (before fallback), node_id, score, scores_breakdown (all 7 signals), status, latency_ms, time_to_first_token_ms, prompt_tokens, completion_tokens, retry_count, fallback_used, tags.

### Failure investigation
```bash
# Recent failures with error details
sqlite3 ~/.fleet-manager/latency.db "SELECT request_id, model, node_id, error_message, latency_ms/1000.0 as secs, datetime(timestamp, 'unixepoch', 'localtime') as time FROM request_traces WHERE status='failed' ORDER BY timestamp DESC LIMIT 20"

# Retry frequency — which nodes need attention?
sqlite3 ~/.fleet-manager/latency.db "SELECT node_id, SUM(retry_count) as retries, COUNT(*) as total, ROUND(100.0 * SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) / COUNT(*), 1) as fail_pct FROM request_traces GROUP BY node_id ORDER BY fail_pct DESC"

# Fallback frequency — which models are unreliable?
sqlite3 ~/.fleet-manager/latency.db "SELECT original_model, model as fell_back_to, COUNT(*) as n FROM request_traces WHERE fallback_used=1 GROUP BY original_model, model ORDER BY n DESC"
```

### Latency analysis
```bash
# P50/P75/P99 latency by model
sqlite3 ~/.fleet-manager/latency.db "
WITH ranked AS (
  SELECT model, latency_ms,
    PERCENT_RANK() OVER (PARTITION BY model ORDER BY latency_ms) as pct
  FROM request_traces WHERE status='completed'
)
SELECT model,
  ROUND(MIN(CASE WHEN pct >= 0.5 THEN latency_ms END)/1000.0, 1) as p50_s,
  ROUND(MIN(CASE WHEN pct >= 0.75 THEN latency_ms END)/1000.0, 1) as p75_s,
  ROUND(MIN(CASE WHEN pct >= 0.99 THEN latency_ms END)/1000.0, 1) as p99_s,
  COUNT(*) as n
FROM ranked GROUP BY model HAVING n > 10 ORDER BY p75_s DESC
"

# Time-to-first-token by node (cold load detection)
sqlite3 ~/.fleet-manager/latency.db "SELECT node_id, model, ROUND(AVG(time_to_first_token_ms), 0) as avg_ttft_ms, ROUND(MAX(time_to_first_token_ms), 0) as max_ttft_ms, COUNT(*) as n FROM request_traces WHERE time_to_first_token_ms IS NOT NULL GROUP BY node_id, model HAVING n > 5 ORDER BY avg_ttft_ms DESC"

# Slowest requests (outlier detection)
sqlite3 ~/.fleet-manager/latency.db "SELECT request_id, model, node_id, ROUND(latency_ms/1000.0, 1) as secs, prompt_tokens, completion_tokens, retry_count, datetime(timestamp, 'unixepoch', 'localtime') as time FROM request_traces WHERE status='completed' ORDER BY latency_ms DESC LIMIT 10"
```

## Per-application analytics

Tag requests to track usage per application, team, or environment.

### Tagging requests
```bash
# Via request body
curl -s http://localhost:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"llama3.3:70b","messages":[{"role":"user","content":"Hello"}],"metadata":{"tags":["prod","code-review"]}}'

# Via header (useful for proxies)
curl -s -H "X-Herd-Tags: prod, code-review" \
  http://localhost:11435/v1/chat/completions \
  -d '{"model":"llama3.3:70b","messages":[{"role":"user","content":"Hello"}]}'
```

### Per-tag dashboards
```bash
curl -s http://localhost:11435/dashboard/api/apps | python3 -m json.tool
curl -s http://localhost:11435/dashboard/api/apps/daily | python3 -m json.tool
```

### Token consumption by tag
```bash
sqlite3 ~/.fleet-manager/latency.db "SELECT j.value as tag, COUNT(*) as requests, SUM(COALESCE(prompt_tokens,0)) as prompt_tok, SUM(COALESCE(completion_tokens,0)) as completion_tok, SUM(COALESCE(prompt_tokens,0)+COALESCE(completion_tokens,0)) as total_tok FROM request_traces, json_each(tags) j WHERE tags IS NOT NULL GROUP BY j.value ORDER BY total_tok DESC"
```

### Error rates by tag
```bash
sqlite3 ~/.fleet-manager/latency.db "SELECT j.value as tag, COUNT(*) as total, SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed, ROUND(100.0 * SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) / COUNT(*), 1) as fail_pct FROM request_traces, json_each(tags) j WHERE tags IS NOT NULL GROUP BY j.value HAVING total > 10 ORDER BY fail_pct DESC"
```

## Traffic patterns
```bash
# Requests per hour (find peak load times)
sqlite3 ~/.fleet-manager/latency.db "SELECT CAST((timestamp % 86400) / 3600 AS INTEGER) as hour_utc, COUNT(*) as requests, ROUND(AVG(latency_ms)/1000.0, 1) as avg_secs FROM request_traces GROUP BY hour_utc ORDER BY hour_utc"

# Daily request volume
sqlite3 ~/.fleet-manager/latency.db "SELECT date(timestamp, 'unixepoch') as day, COUNT(*) as requests, SUM(COALESCE(prompt_tokens,0)+COALESCE(completion_tokens,0)) as tokens FROM request_traces GROUP BY day ORDER BY day DESC LIMIT 14"
```

## Capacity planning

### Model recommendations per node
```bash
curl -s http://localhost:11435/dashboard/api/recommendations | python3 -m json.tool
```

Returns recommendations based on hardware capabilities, current usage, and curated benchmark data. Use for capacity planning: which models fit on which machines, and what's the optimal mix.

### Usage statistics
```bash
curl -s http://localhost:11435/dashboard/api/usage | python3 -m json.tool
```

### Model performance comparison
```bash
curl -s http://localhost:11435/dashboard/api/models | python3 -m json.tool
```

## Configuration
```bash
# View all settings
curl -s http://localhost:11435/dashboard/api/settings | python3 -m json.tool

# Toggle runtime settings
curl -s -X POST http://localhost:11435/dashboard/api/settings \
  -H "Content-Type: application/json" \
  -d '{"auto_pull": false}'
```

## Log analysis

Structured JSONL logs at `~/.fleet-manager/logs/herd.jsonl`:

```bash
# Recent errors
grep '"level": "ERROR"' ~/.fleet-manager/logs/herd.jsonl | tail -10 | python3 -m json.tool

# Context protection events
grep "Context protection" ~/.fleet-manager/logs/herd.jsonl | tail -10

# Stale in-flight reaper activity
grep "Reaped stale" ~/.fleet-manager/logs/herd.jsonl | tail -10

# Stream errors
grep "Stream error" ~/.fleet-manager/logs/herd.jsonl | tail -10
```

## Dashboard

Web dashboard at `http://localhost:11435/dashboard`. Key tabs for ops:
- **Trends** — requests/hour, latency, token throughput over 24h–7d
- **Apps** — per-tag analytics with daily breakdowns
- **Health** — automated health checks with severity and recommendations
- **Model Insights** — per-model latency and throughput comparison

## Guardrails

- Never restart services without explicit user confirmation.
- Never delete or modify `~/.fleet-manager/` contents.
- Do not pull or delete models without user confirmation.
- Report issues to the user rather than attempting automated fixes.
- If the router isn't running, suggest `herd` or `uv run herd`.
