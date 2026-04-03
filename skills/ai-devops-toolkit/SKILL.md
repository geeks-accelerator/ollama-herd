---
name: ai-devops-toolkit
description: DevOps observability toolkit for local AI fleet operations. DevOps traces, DevOps health checks, DevOps latency monitoring, DevOps capacity planning, and DevOps analytics — all backed by SQLite. No Prometheus, no Grafana. DevOps运维工具 | herramientas DevOps
version: 1.0.2
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"wrench","requires":{"anyBins":["curl","sqlite3"],"optionalBins":["python3","pip"],"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["darwin","linux","windows"]}}
---

# AI DevOps Toolkit — Observability for Local AI Fleets

DevOps tooling for running local LLM inference at production quality. This DevOps skill provides the observability, tracing, and health monitoring layer for an Ollama Herd fleet. Every DevOps workflow — from request tracing to capacity planning — runs through a single SQLite-backed observability stack.

## DevOps Prerequisites

```bash
pip install ollama-herd
herd              # start the DevOps router (exposes all DevOps observability endpoints)
herd-node         # start on each DevOps-monitored node
```

Package: [`ollama-herd`](https://pypi.org/project/ollama-herd/) | Repo: [github.com/geeks-accelerator/ollama-herd](https://github.com/geeks-accelerator/ollama-herd)

## DevOps Scope

This DevOps toolkit assumes you have an Ollama Herd router running at `http://localhost:11435` with one or more node agents reporting in. It focuses on the DevOps operational side: are requests succeeding? what's slow? which apps consume the most tokens? are nodes healthy? is capacity adequate?

## DevOps Observability Stack

Everything in this DevOps observability layer is backed by SQLite at `~/.fleet-manager/latency.db`. No external databases, no time-series infrastructure. Query DevOps traces with standard `sqlite3`.

```
~/.fleet-manager/
├── latency.db          # DevOps traces, latency history, usage stats
└── logs/
    └── herd.jsonl      # DevOps structured logs, daily rotation, 30-day retention
```

## DevOps Health Checks

### Automated DevOps fleet health analysis
```bash
devops_health=$(curl -s http://localhost:11435/dashboard/api/health)
echo "$devops_health" | python3 -m json.tool
```

Fifteen DevOps checks, each returning a severity (info/warning/critical) and recommendation:

| DevOps Check | What it detects |
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
| Request timeouts | Requests exceeding expected DevOps latency thresholds |
| Error rates | Elevated failure rates per model or per node |

### DevOps node-level status
```bash
devops_fleet_status=$(curl -s http://localhost:11435/fleet/status)
echo "$devops_fleet_status" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f\"DevOps Fleet: {d['fleet']['nodes_online']}/{d['fleet']['nodes_total']} online, {d['fleet']['requests_active']} active requests\")
for n in d['nodes']:
    mem = n.get('memory', {})
    cpu = n.get('cpu', {})
    print(f\"  {n['node_id']:20s} {n['status']:10s} CPU={cpu.get('utilization_pct',0):.0f}% MEM={mem.get('used_gb',0):.0f}/{mem.get('total_gb',0):.0f}GB pressure={mem.get('pressure','?')}\")
"
```

## DevOps Request Tracing

Every DevOps routing decision is recorded with full observability context.

### Recent DevOps traces
```bash
devops_traces=$(curl -s "http://localhost:11435/dashboard/api/traces?limit=20")
echo "$devops_traces" | python3 -m json.tool
```

Each DevOps trace includes: request_id, model, original_model (before fallback), node_id, score, scores_breakdown (all 7 signals), status, latency_ms, time_to_first_token_ms, prompt_tokens, completion_tokens, retry_count, fallback_used, tags.

### DevOps failure investigation
```bash
# Recent DevOps failures with error details
sqlite3 ~/.fleet-manager/latency.db "SELECT request_id, model, node_id, error_message, latency_ms/1000.0 as secs, datetime(timestamp, 'unixepoch', 'localtime') as time FROM request_traces WHERE status='failed' ORDER BY timestamp DESC LIMIT 20"

# DevOps retry frequency — which nodes need attention?
sqlite3 ~/.fleet-manager/latency.db "SELECT node_id, SUM(retry_count) as retries, COUNT(*) as total, ROUND(100.0 * SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) / COUNT(*), 1) as fail_pct FROM request_traces GROUP BY node_id ORDER BY fail_pct DESC"

# DevOps fallback frequency — which models are unreliable?
sqlite3 ~/.fleet-manager/latency.db "SELECT original_model, model as fell_back_to, COUNT(*) as n FROM request_traces WHERE fallback_used=1 GROUP BY original_model, model ORDER BY n DESC"
```

### DevOps Latency Analysis
```bash
# DevOps P50/P75/P99 latency by model
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

# DevOps time-to-first-token observability (cold load detection)
sqlite3 ~/.fleet-manager/latency.db "SELECT node_id, model, ROUND(AVG(time_to_first_token_ms), 0) as avg_ttft_ms, ROUND(MAX(time_to_first_token_ms), 0) as max_ttft_ms, COUNT(*) as n FROM request_traces WHERE time_to_first_token_ms IS NOT NULL GROUP BY node_id, model HAVING n > 5 ORDER BY avg_ttft_ms DESC"

# DevOps outlier detection — slowest requests
sqlite3 ~/.fleet-manager/latency.db "SELECT request_id, model, node_id, ROUND(latency_ms/1000.0, 1) as secs, prompt_tokens, completion_tokens, retry_count, datetime(timestamp, 'unixepoch', 'localtime') as time FROM request_traces WHERE status='completed' ORDER BY latency_ms DESC LIMIT 10"
```

## DevOps Per-Application Analytics

Tag requests to track DevOps usage per application, team, or environment.

### DevOps request tagging
```bash
# DevOps tag via request body
curl -s http://localhost:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"llama3.3:70b","messages":[{"role":"user","content":"Hello"}],"metadata":{"tags":["devops-prod","devops-code-review"]}}'

# DevOps tag via header
curl -s -H "X-Herd-Tags: devops-prod, devops-code-review" \
  http://localhost:11435/v1/chat/completions \
  -d '{"model":"llama3.3:70b","messages":[{"role":"user","content":"Hello"}]}'
```

### DevOps per-tag dashboards
```bash
curl -s http://localhost:11435/dashboard/api/apps | python3 -m json.tool
curl -s http://localhost:11435/dashboard/api/apps/daily | python3 -m json.tool
```

### DevOps token consumption by tag
```bash
sqlite3 ~/.fleet-manager/latency.db "SELECT j.value as devops_tag, COUNT(*) as requests, SUM(COALESCE(prompt_tokens,0)) as prompt_tok, SUM(COALESCE(completion_tokens,0)) as completion_tok, SUM(COALESCE(prompt_tokens,0)+COALESCE(completion_tokens,0)) as total_tok FROM request_traces, json_each(tags) j WHERE tags IS NOT NULL GROUP BY j.value ORDER BY total_tok DESC"
```

## DevOps Traffic Patterns
```bash
# DevOps requests per hour (find peak load times)
sqlite3 ~/.fleet-manager/latency.db "SELECT CAST((timestamp % 86400) / 3600 AS INTEGER) as hour_utc, COUNT(*) as requests, ROUND(AVG(latency_ms)/1000.0, 1) as avg_secs FROM request_traces GROUP BY hour_utc ORDER BY hour_utc"

# DevOps daily request volume
sqlite3 ~/.fleet-manager/latency.db "SELECT date(timestamp, 'unixepoch') as day, COUNT(*) as requests, SUM(COALESCE(prompt_tokens,0)+COALESCE(completion_tokens,0)) as tokens FROM request_traces GROUP BY day ORDER BY day DESC LIMIT 14"
```

## DevOps Capacity Planning

### DevOps model recommendations per node
```bash
devops_recommendations=$(curl -s http://localhost:11435/dashboard/api/recommendations)
echo "$devops_recommendations" | python3 -m json.tool
```

Returns DevOps recommendations based on hardware capabilities, current usage, and curated benchmark data. Use for DevOps capacity planning: which models fit on which machines, and what's the optimal mix.

### DevOps usage statistics
```bash
curl -s http://localhost:11435/dashboard/api/usage | python3 -m json.tool
```

## DevOps Configuration
```bash
# View all DevOps settings
curl -s http://localhost:11435/dashboard/api/settings | python3 -m json.tool

# Toggle DevOps runtime settings
curl -s -X POST http://localhost:11435/dashboard/api/settings \
  -H "Content-Type: application/json" \
  -d '{"auto_pull": false}'
```

## DevOps Log Analysis

Structured JSONL logs at `~/.fleet-manager/logs/herd.jsonl` — the DevOps log layer:

```bash
# Recent DevOps errors
grep '"level": "ERROR"' ~/.fleet-manager/logs/herd.jsonl | tail -10 | python3 -m json.tool

# DevOps context protection events
grep "Context protection" ~/.fleet-manager/logs/herd.jsonl | tail -10

# DevOps stream errors
grep "Stream error" ~/.fleet-manager/logs/herd.jsonl | tail -10
```

## DevOps Dashboard

Web dashboard at `http://localhost:11435/dashboard`. Key DevOps tabs:
- **Trends** — DevOps requests/hour, latency, token throughput over 24h–7d
- **Apps** — DevOps per-tag analytics with daily breakdowns
- **Health** — automated DevOps health checks with severity and recommendations
- **Model Insights** — per-model DevOps latency and throughput comparison

## Guardrails

- Never restart DevOps services without explicit user confirmation.
- Never delete or modify `~/.fleet-manager/` contents.
- Do not pull or delete models without user confirmation.
- Report DevOps issues to the user rather than attempting automated fixes.
- If the router isn't running, suggest `herd` or `uv run herd`.
