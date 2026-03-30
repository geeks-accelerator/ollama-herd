---
name: ollama-load-balancer
description: Ollama load balancer for Llama, Qwen, DeepSeek, and Mistral inference across multiple machines. Auto-discovery via mDNS, health checks, queue management, automatic failover, retry on node failure, and zombie request cleanup. Zero configuration. Use when the user needs high availability for Ollama or wants to distribute inference load across devices.
version: 1.0.2
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"scales","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","sqlite3","pip"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["darwin","linux"]}}
---

# Ollama Load Balancer

You are managing an Ollama load balancer that distributes inference requests across multiple Ollama instances with automatic discovery, health monitoring, and failover.

## What this solves

Ollama has no built-in load balancing. One machine goes down, your app gets errors. No health checks, no failover, no queue management. You're manually pointing clients at specific machines and hoping they stay up.

This load balancer auto-discovers Ollama instances via mDNS, monitors their health continuously, distributes requests based on real-time scoring, and automatically retries on failure. Zero config files. Zero Docker. `pip install ollama-herd`, run two commands, done.

## Deploy

```bash
pip install ollama-herd
herd              # start the load balancer
herd-node         # start on each backend node
```

Package: [`ollama-herd`](https://pypi.org/project/ollama-herd/) | Repo: [github.com/geeks-accelerator/ollama-herd](https://github.com/geeks-accelerator/ollama-herd)

## Endpoint

The load balancer runs at `http://localhost:11435`. Drop-in replacement for direct Ollama connections — same API, same model names.

## Health monitoring

### Fleet-wide health check (11 automated checks)
```bash
curl -s http://localhost:11435/dashboard/api/health | python3 -m json.tool
```

Checks: offline nodes, degraded nodes, memory pressure, underutilized nodes, model thrashing, request timeouts, error rates. Each check returns severity (info/warning/critical) and recommendations.

### Node status and metrics
```bash
curl -s http://localhost:11435/fleet/status | python3 -m json.tool
```

Returns per-node: status (online/degraded/offline), CPU utilization, memory usage and pressure, disk space, loaded models with context lengths, queue depths (pending/in-flight/done/failed).

### Queue depths
```bash
curl -s http://localhost:11435/fleet/status | python3 -c "
import sys, json
d = json.load(sys.stdin)
for key, q in d.get('queues', {}).items():
    print(f\"{key}: {q['pending']} pending, {q['in_flight']}/{q['max_concurrent']} in-flight, {q['done']} done, {q['failed']} failed\")
"
```

## Auto-recovery features

- **Auto-retry** — if a node fails before sending the first response chunk, the balancer re-scores all nodes and retries on the next-best one (up to 2 retries, configurable via `FLEET_MAX_RETRIES`)
- **Zombie reaper** — background task detects in-flight requests stuck longer than 10 minutes and cleans them up, freeing concurrency slots
- **Context protection** — strips dangerous `num_ctx` parameters that would trigger multi-minute model reloads
- **VRAM-aware fallback** — routes to an already-loaded model in the same category instead of triggering a cold load
- **Auto-pull** — optionally pulls missing models (disabled by default, toggle via settings API)
- **Holding queue** — when all nodes are busy, requests wait (up to 30s) rather than immediately failing

## Available API endpoints

### Models
```bash
# All models across the fleet
curl -s http://localhost:11435/api/tags | python3 -m json.tool

# Models currently loaded in memory
curl -s http://localhost:11435/api/ps | python3 -m json.tool

# OpenAI-compatible model list
curl -s http://localhost:11435/v1/models | python3 -m json.tool
```

### Request traces
```bash
curl -s "http://localhost:11435/dashboard/api/traces?limit=20" | python3 -m json.tool
```

### Usage statistics
```bash
curl -s http://localhost:11435/dashboard/api/usage | python3 -m json.tool
```

### Model recommendations
```bash
curl -s http://localhost:11435/dashboard/api/recommendations | python3 -m json.tool
```

### Settings (runtime toggles)
```bash
# View config
curl -s http://localhost:11435/dashboard/api/settings | python3 -m json.tool

# Toggle features
curl -s -X POST http://localhost:11435/dashboard/api/settings \
  -H "Content-Type: application/json" \
  -d '{"auto_pull": false}'
```

### Model management
```bash
# View per-node model details
curl -s http://localhost:11435/dashboard/api/model-management | python3 -m json.tool

# Pull a model
curl -s -X POST http://localhost:11435/dashboard/api/pull \
  -H "Content-Type: application/json" \
  -d '{"model": "llama3.3:70b", "node_id": "mac-studio"}'

# Delete a model
curl -s -X POST http://localhost:11435/dashboard/api/delete \
  -H "Content-Type: application/json" \
  -d '{"model": "old-model:7b", "node_id": "mac-studio"}'
```

### Per-app analytics
```bash
curl -s http://localhost:11435/dashboard/api/apps | python3 -m json.tool
```

## Dashboard

Web dashboard at `http://localhost:11435/dashboard` with eight tabs: Fleet Overview, Trends, Model Insights, Apps, Benchmarks, Health, Recommendations, Settings. All real-time via Server-Sent Events.

## Operational queries

### Recent failures with error details
```bash
sqlite3 ~/.fleet-manager/latency.db "SELECT request_id, model, status, error_message, latency_ms/1000.0 as secs FROM request_traces WHERE status='failed' ORDER BY timestamp DESC LIMIT 10"
```

### Retry frequency by node
```bash
sqlite3 ~/.fleet-manager/latency.db "SELECT node_id, SUM(retry_count) as retries, COUNT(*) as total FROM request_traces GROUP BY node_id ORDER BY retries DESC"
```

### Requests per hour (find peak load times)
```bash
sqlite3 ~/.fleet-manager/latency.db "SELECT CAST((timestamp % 86400) / 3600 AS INTEGER) as hour, COUNT(*) as requests FROM request_traces GROUP BY hour ORDER BY hour"
```

### Error rates by model
```bash
sqlite3 ~/.fleet-manager/latency.db "SELECT model, COUNT(CASE WHEN status='completed' THEN 1 END) as ok, COUNT(CASE WHEN status='failed' THEN 1 END) as failed, ROUND(100.0 * COUNT(CASE WHEN status='failed' THEN 1 END) / COUNT(*), 1) as fail_pct FROM request_traces GROUP BY model ORDER BY fail_pct DESC"
```

### Test inference
```bash
curl -s http://localhost:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"llama3.3:70b","messages":[{"role":"user","content":"Hello"}],"stream":false}'

curl -s http://localhost:11435/api/chat \
  -d '{"model":"llama3.3:70b","messages":[{"role":"user","content":"Hello"}],"stream":false}'
```

## Guardrails

- Never restart or stop the load balancer or node agents without explicit user confirmation.
- Never delete or modify files in `~/.fleet-manager/` (contains latency data, traces, and logs).
- Do not pull or delete models without user confirmation — downloads can be 10-100+ GB.
- If a node shows as offline, report it rather than attempting to SSH into the machine.
- If all nodes are saturated, suggest the user check the dashboard rather than attempting to fix it automatically.

## Failure handling

- Connection refused → load balancer may not be running, suggest `herd` or `uv run herd`
- 0 nodes online → suggest starting `herd-node` or `uv run herd-node` on devices
- mDNS discovery fails → use `--router-url http://router-ip:11435`
- Requests hang with 0 bytes → check for `num_ctx` in client requests; verify context protection with `grep "Context protection" ~/.fleet-manager/logs/herd.jsonl`
- API errors → check `~/.fleet-manager/logs/herd.jsonl`
