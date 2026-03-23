---
name: ollama-herd
description: Manage your Ollama Herd device fleet — check node status, view queue depths, list available models, inspect request traces, monitor fleet health, manage settings, and get model recommendations. Use when the user asks about their local LLM fleet, inference routing, node status, model availability, or fleet performance.
version: 1.1.0
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"llama","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","sqlite3","pip"],"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"]},"os":["darwin","linux"]}}
---

# Ollama Herd Fleet Manager

You are managing an Ollama Herd fleet — a smart inference router that distributes LLM requests across multiple Ollama instances. It scores nodes on 7 signals (thermal state, memory fit, queue depth, latency history, role affinity, availability trend, context fit) and routes each request to the optimal device.

## Install

```bash
pip install ollama-herd
herd              # start the router
herd-node         # start a node agent (run on each device)
```

PyPI: [`ollama-herd`](https://pypi.org/project/ollama-herd/) | Source: [github.com/geeks-accelerator/ollama-herd](https://github.com/geeks-accelerator/ollama-herd)

## Router endpoint

The Herd router runs at `http://localhost:11435` by default. If the user has specified a different URL, use that instead.

## Available API endpoints

Use curl to interact with the fleet:

### Fleet status — overview of all nodes and queues
```bash
curl -s http://localhost:11435/fleet/status | python3 -m json.tool
```

Returns:
- `fleet.nodes_total` / `fleet.nodes_online` — how many devices are in the fleet
- `fleet.models_loaded` — total models currently loaded across all nodes
- `fleet.requests_active` — total in-flight requests
- `nodes[]` — per-node details: status, hardware, memory, CPU, disk, loaded models with context lengths
- `queues` — per node:model queue depths (pending, in-flight, done, failed)

### List all models available across the fleet
```bash
curl -s http://localhost:11435/api/tags | python3 -m json.tool
```

### List models currently loaded in memory
```bash
curl -s http://localhost:11435/api/ps | python3 -m json.tool
```

### OpenAI-compatible model list
```bash
curl -s http://localhost:11435/v1/models | python3 -m json.tool
```

### Usage statistics (per-node, per-model daily aggregates)
```bash
curl -s http://localhost:11435/dashboard/api/usage | python3 -m json.tool
```

### Recent request traces
```bash
curl -s "http://localhost:11435/dashboard/api/traces?limit=20" | python3 -m json.tool
```

Returns the last N routing decisions with: model requested, node selected, score, latency, tokens, retry/fallback status, tags.

### Fleet health analysis
```bash
curl -s http://localhost:11435/dashboard/api/health | python3 -m json.tool
```

Returns 11 automated health checks: offline/degraded nodes, memory pressure, underutilized nodes, VRAM fallbacks, version mismatch, context protection, zombie reaper, model thrashing, request timeouts, error rates, and retry rates.

### Model recommendations
```bash
curl -s http://localhost:11435/dashboard/api/recommendations | python3 -m json.tool
```

Returns AI-powered model mix recommendations per node based on hardware capabilities, usage patterns, and curated benchmark data.

### Settings
```bash
# View current config and node versions
curl -s http://localhost:11435/dashboard/api/settings | python3 -m json.tool

# Toggle runtime settings (auto_pull, vram_fallback)
curl -s -X POST http://localhost:11435/dashboard/api/settings \
  -H "Content-Type: application/json" \
  -d '{"auto_pull": false}'
```

### Model management
```bash
# View per-node model details with sizes and usage
curl -s http://localhost:11435/dashboard/api/model-management | python3 -m json.tool

# Pull a model onto a specific node
curl -s -X POST http://localhost:11435/dashboard/api/pull \
  -H "Content-Type: application/json" \
  -d '{"model": "llama3.3:70b", "node_id": "mac-studio"}'

# Delete a model from a specific node
curl -s -X POST http://localhost:11435/dashboard/api/delete \
  -H "Content-Type: application/json" \
  -d '{"model": "old-model:7b", "node_id": "mac-studio"}'
```

### Model insights (summary statistics)
```bash
curl -s http://localhost:11435/dashboard/api/models | python3 -m json.tool
```

### Per-app analytics (requires request tagging)
```bash
curl -s http://localhost:11435/dashboard/api/apps | python3 -m json.tool
```

## Dashboard

The web dashboard is at `http://localhost:11435/dashboard`. It has eight tabs:
- **Fleet Overview** — live node cards, queue depths, and request counts via SSE
- **Trends** — requests per hour, average latency, and token throughput charts (24h–7d)
- **Model Insights** — per-model latency, tokens/sec, usage comparison
- **Apps** — per-tag analytics with request volume, latency, tokens, error rates, and daily trends
- **Benchmarks** — capacity growth over time with per-run throughput and latency percentiles
- **Health** — 11 automated fleet health checks with severity levels and recommendations
- **Recommendations** — model mix recommendations per node with one-click pull
- **Settings** — runtime toggle switches, read-only config tables, and node version tracking

Direct the user to open this URL in their browser for visual monitoring.

## Resilience features

- **Auto-retry** — if a node fails before the first response chunk, re-scores and retries on the next-best node (up to 2 retries)
- **Model fallbacks** — clients specify backup models; tries alternatives when the primary is unavailable
- **Context protection** — strips `num_ctx` from requests when unnecessary to prevent Ollama model reload hangs; auto-upgrades to a larger loaded model when more context is needed
- **VRAM-aware fallback** — routes to an already-loaded model in the same category instead of cold-loading
- **Zombie reaper** — background task detects and cleans up stuck in-flight requests
- **Auto-pull** — automatically pulls missing models onto the best available node

## Common tasks

### Check if the fleet is healthy
1. Hit `/fleet/status` and verify `nodes_online > 0`
2. Hit `/dashboard/api/health` for automated health checks with severity levels
3. Look at queue depths — deep queues may indicate a bottleneck

### Find which node has a specific model
1. Hit `/fleet/status` and inspect each node's `ollama.models_loaded` (in memory) and `ollama.models_available` (on disk)
2. Or hit `/api/tags` for a flat list of all available models with which nodes have them

### Check if a model is loaded (hot) or cold
1. Hit `/api/ps` — models listed here are currently loaded in memory (hot) with their context lengths
2. Models in `/api/tags` but not in `/api/ps` are on disk but not loaded (cold)

### View recent inference activity
1. Hit `/dashboard/api/traces?limit=10` to see the last 10 requests
2. Each trace shows: model, node, score, latency, tokens, retry/fallback status, tags

### Diagnose slow responses
1. Check `/dashboard/api/traces` for high latency entries
2. Check `/fleet/status` for nodes with high queue depths or memory pressure
3. Check if the model had to cold-load (look for low scores in trace `scores_breakdown`)
4. Check if `num_ctx` is being sent — context protection logs show if requests triggered reloads

### Query the trace database directly
```bash
# Recent failures
sqlite3 ~/.fleet-manager/latency.db "SELECT request_id, model, status, error_message, latency_ms/1000.0 as secs FROM request_traces WHERE status='failed' ORDER BY timestamp DESC LIMIT 10"

# Slowest requests
sqlite3 ~/.fleet-manager/latency.db "SELECT model, node_id, latency_ms/1000.0 as secs, prompt_tokens, completion_tokens FROM request_traces WHERE status='completed' ORDER BY latency_ms DESC LIMIT 10"

# Per-tag usage
sqlite3 ~/.fleet-manager/latency.db "SELECT j.value as tag, COUNT(*) as requests, SUM(COALESCE(prompt_tokens,0)+COALESCE(completion_tokens,0)) as tokens FROM request_traces, json_each(tags) j WHERE tags IS NOT NULL GROUP BY j.value ORDER BY tokens DESC"
```

### Test inference through the fleet
```bash
# OpenAI format
curl -s http://localhost:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"llama3.3:70b","messages":[{"role":"user","content":"Hello"}],"stream":false}'

# Ollama format
curl -s http://localhost:11435/api/chat \
  -d '{"model":"llama3.3:70b","messages":[{"role":"user","content":"Hello"}],"stream":false}'

# With request tagging
curl -s http://localhost:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"llama3.3:70b","messages":[{"role":"user","content":"Hello"}],"metadata":{"tags":["my-app","testing"]}}'
```

## Guardrails

- Never restart or stop the Herd router or node agents without explicit user confirmation.
- Never delete or modify files in `~/.fleet-manager/` (contains latency data, traces, and logs).
- Do not pull models onto nodes without user confirmation — model downloads can be large (10-100+ GB).
- Do not delete models without user confirmation.
- If a node shows as offline, report it to the user rather than attempting to SSH into the machine.
- If all nodes are saturated, suggest the user check the dashboard rather than attempting to fix it automatically.

## Failure handling

- If curl to the router fails with connection refused, tell the user the Herd router may not be running and suggest `herd` (or `uv run herd` for dev) to start it.
- If the fleet status shows 0 nodes online, suggest the user start node agents with `herd-node` (or `uv run herd-node`) on their devices.
- If mDNS discovery fails, suggest using `--router-url http://router-ip:11435` for explicit connection.
- If requests hang with 0 bytes returned, check if the client is sending `num_ctx` — context protection should strip it, but verify with `grep "Context protection" ~/.fleet-manager/logs/herd.jsonl`.
- If a specific API endpoint returns an error, show the user the full error response and suggest checking the JSONL logs at `~/.fleet-manager/logs/herd.jsonl`.
