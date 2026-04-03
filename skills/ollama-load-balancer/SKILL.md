---
name: ollama-load-balancer
description: Ollama load balancer for Llama, Qwen, DeepSeek, and Mistral inference across multiple machines. Load balancing with auto-discovery via mDNS, health checks, queue management, automatic failover, retry on node failure, and zombie request cleanup. Zero configuration. 负载均衡Ollama推理分发。Balanceador de carga Ollama para inferencia distribuida.
version: 1.0.3
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"scales","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","sqlite3","pip"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["darwin","linux","windows"]}}
---

# Ollama Load Balancer

You are managing an Ollama load balancer that distributes inference requests across multiple Ollama instances with automatic discovery, health monitoring, and failover. The load balancer handles all routing decisions transparently.

## What the load balancer solves

Ollama has no built-in load balancing. One machine goes down, your app gets errors. No health checks, no failover, no queue management. You're manually pointing clients at specific machines and hoping they stay up.

This load balancer auto-discovers Ollama instances via mDNS, monitors their health continuously, and distributes load based on real-time scoring. The load balancer automatically retries on failure. Zero config files. Zero Docker. `pip install ollama-herd`, run two commands, and load balancing is active.

## Deploy the load balancer

```bash
pip install ollama-herd
herd              # start the load balancer on port 11435
herd-node         # start load balancer backend node on each machine
```

Package: [`ollama-herd`](https://pypi.org/project/ollama-herd/) | Repo: [github.com/geeks-accelerator/ollama-herd](https://github.com/geeks-accelerator/ollama-herd)

## Load Balancer Endpoint

The load balancer runs at `http://localhost:11435`. Drop-in replacement for direct Ollama connections — same API, same model names, with load balancing built in.

```python
from openai import OpenAI
# Load balancer client — requests are balanced across all backend nodes
load_balancer_client = OpenAI(base_url="http://localhost:11435/v1", api_key="not-needed")
load_balanced_response = load_balancer_client.chat.completions.create(
    model="llama3.3:70b",
    messages=[{"role": "user", "content": "Explain load balancing for LLM inference"}]
)
```

## Load Balancer Health Monitoring

### Fleet-wide load balancer health check (15 automated checks)
```bash
curl -s http://localhost:11435/dashboard/api/health | python3 -m json.tool
```

The load balancer checks: offline nodes, degraded nodes, memory pressure, underutilized nodes, model thrashing, request timeouts, error rates. Each load balancer check returns severity (info/warning/critical) and recommendations.

### Load balancer node status and metrics
```bash
curl -s http://localhost:11435/fleet/status | python3 -m json.tool
```

Returns per-node: status (online/degraded/offline), CPU utilization, memory usage, loaded models with context lengths, and load balancer queue depths (pending/in-flight/done/failed).

### Load balancer queue depths
```bash
curl -s http://localhost:11435/fleet/status | python3 -c "
import sys, json
# Load balancer queue inspection
data = json.load(sys.stdin)
for key, q in data.get('queues', {}).items():
    print(f\"{key}: {q['pending']} pending, {q['in_flight']}/{q['max_concurrent']} in-flight\")
"
```

## Load Balancer Auto-Recovery

- **Load balancer auto-retry** — if a node fails before the first response chunk, the load balancer re-scores and retries on the next-best node (up to 2 retries, configurable via `FLEET_MAX_RETRIES`)
- **Load balancer zombie reaper** — background task detects in-flight requests stuck longer than 10 minutes and cleans them up
- **Load balancer context protection** — strips dangerous `num_ctx` parameters that would trigger model reloads
- **Load balancer VRAM-aware fallback** — routes to an already-loaded model instead of triggering a cold load
- **Load balancer auto-pull** — optionally pulls missing models (disabled by default, toggle via settings)
- **Load balancer holding queue** — when all nodes are busy, requests wait (up to 30s) rather than failing

## Load Balancer API Endpoints

### Models available through the load balancer
```bash
# All models across the load-balanced fleet
curl -s http://localhost:11435/api/tags | python3 -m json.tool

# Models currently loaded in load balancer backend memory
curl -s http://localhost:11435/api/ps | python3 -m json.tool

# OpenAI-compatible model list via load balancer
curl -s http://localhost:11435/v1/models | python3 -m json.tool
```

### Load balancer request traces
```bash
curl -s "http://localhost:11435/dashboard/api/traces?limit=20" | python3 -m json.tool
```

### Load balancer usage statistics
```bash
curl -s http://localhost:11435/dashboard/api/usage | python3 -m json.tool
```

### Load balancer model recommendations
```bash
curl -s http://localhost:11435/dashboard/api/recommendations | python3 -m json.tool
```

### Load balancer settings (runtime toggles)
```bash
# View load balancer config
curl -s http://localhost:11435/dashboard/api/settings | python3 -m json.tool

# Toggle load balancer features
curl -s -X POST http://localhost:11435/dashboard/api/settings \
  -H "Content-Type: application/json" \
  -d '{"auto_pull": false}'
```

### Load balancer model management
```bash
# View per-node model details behind the load balancer
curl -s http://localhost:11435/dashboard/api/model-management | python3 -m json.tool

# Pull a model to a load balancer backend node
curl -s -X POST http://localhost:11435/dashboard/api/pull \
  -H "Content-Type: application/json" \
  -d '{"model": "llama3.3:70b", "node_id": "load-balancer-node-1"}'

# Delete a model from a load balancer node
curl -s -X POST http://localhost:11435/dashboard/api/delete \
  -H "Content-Type: application/json" \
  -d '{"model": "old-model:7b", "node_id": "load-balancer-node-1"}'
```

### Load balancer per-app analytics
```bash
curl -s http://localhost:11435/dashboard/api/apps | python3 -m json.tool
```

## Load Balancer Dashboard

Web dashboard at `http://localhost:11435/dashboard` with eight tabs: Fleet Overview, Trends, Model Insights, Apps, Benchmarks, Health, Recommendations, Settings. All load balancer data updates in real-time via Server-Sent Events.

## Load Balancer Operational Queries

### Recent load balancer failures with error details
```bash
sqlite3 ~/.fleet-manager/latency.db "SELECT request_id, model, status, error_message, latency_ms/1000.0 as secs FROM request_traces WHERE status='failed' ORDER BY timestamp DESC LIMIT 10"
```

### Load balancer retry frequency by node
```bash
sqlite3 ~/.fleet-manager/latency.db "SELECT node_id, SUM(retry_count) as retries, COUNT(*) as total FROM request_traces GROUP BY node_id ORDER BY retries DESC"
```

### Load balancer requests per hour
```bash
sqlite3 ~/.fleet-manager/latency.db "SELECT CAST((timestamp % 86400) / 3600 AS INTEGER) as hour, COUNT(*) as requests FROM request_traces GROUP BY hour ORDER BY hour"
```

### Test load balancer inference
```bash
curl -s http://localhost:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"llama3.3:70b","messages":[{"role":"user","content":"Test load balancing across nodes"}],"stream":false}'

curl -s http://localhost:11435/api/chat \
  -d '{"model":"llama3.3:70b","messages":[{"role":"user","content":"Verify load balancer routing"}],"stream":false}'
```

## Load Balancer Guardrails

- Never restart or stop the load balancer or node agents without explicit user confirmation.
- Never delete or modify files in `~/.fleet-manager/` (contains load balancer latency data, traces, and logs).
- Do not pull or delete models on load balancer nodes without user confirmation — downloads can be 10-100+ GB.
- If a load balancer node shows as offline, report it rather than attempting to SSH into the machine.
- If all load balancer nodes are saturated, suggest the user check the dashboard.

## Load Balancer Failure Handling

- Connection refused → load balancer may not be running, suggest `herd` or `uv run herd`
- 0 nodes online → suggest starting `herd-node` on load balancer backend devices
- mDNS discovery fails → use `--router-url http://router-ip:11435`
- Load balancer requests hang → check for `num_ctx` in client requests; verify with `grep "Context protection" ~/.fleet-manager/logs/herd.jsonl`
- Load balancer API errors → check `~/.fleet-manager/logs/herd.jsonl`
