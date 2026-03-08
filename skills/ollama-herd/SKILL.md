---
name: ollama-herd
description: Manage your Ollama Herd device fleet — check node status, view queue depths, list available models, inspect request traces, and monitor fleet health. Use when the user asks about their local LLM fleet, inference routing, node status, model availability, or fleet performance.
version: 1.0.0
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"llama","requires":{"anyBins":["curl","wget"]},"os":["darwin","linux"]}}
---

# Ollama Herd Fleet Manager

You are managing an Ollama Herd fleet — a smart inference router that distributes LLM requests across multiple Ollama instances on Apple Silicon devices.

## Router endpoint

The Herd router runs at `http://localhost:8080` by default. If the user has specified a different URL, use that instead.

## Available API endpoints

Use curl to interact with the fleet:

### Fleet status — overview of all nodes and queues
```bash
curl -s http://localhost:8080/fleet/status | python3 -m json.tool
```

Returns:
- `fleet.nodes_total` / `fleet.nodes_online` — how many devices are in the fleet
- `fleet.models_loaded` — total models currently loaded across all nodes
- `fleet.requests_active` — total in-flight requests
- `nodes[]` — per-node details: status, hardware, memory, loaded models
- `queues` — per node:model queue depths (pending + in-flight)

### List all models available across the fleet
```bash
curl -s http://localhost:8080/api/tags | python3 -m json.tool
```

### List models currently loaded in memory
```bash
curl -s http://localhost:8080/api/ps | python3 -m json.tool
```

### OpenAI-compatible model list
```bash
curl -s http://localhost:8080/v1/models | python3 -m json.tool
```

### Usage statistics (per-node, per-model daily aggregates)
```bash
curl -s http://localhost:8080/dashboard/api/usage | python3 -m json.tool
```

Returns requests, tokens, and latency broken down by node and model per day.

### Recent request traces
```bash
curl -s "http://localhost:8080/dashboard/api/traces?limit=20" | python3 -m json.tool
```

Returns the last N routing decisions with: model requested, node selected, score, latency, tokens, retry/fallback status.

### Model insights (summary statistics)
```bash
curl -s http://localhost:8080/dashboard/api/models | python3 -m json.tool
```

Returns per-model statistics: total requests, average latency, tokens/sec, prompt and completion token counts.

## Dashboard

The web dashboard is at `http://localhost:8080` (or `http://localhost:8080/dashboard`). It has three tabs:
- **Fleet Overview** — live node cards and queue depths
- **Trends** — requests per hour and token throughput charts
- **Model Insights** — per-model summary cards, latency and token distribution charts, detailed model table

Direct the user to open this URL in their browser for visual monitoring.

## Common tasks

### Check if the fleet is healthy
1. Hit `/fleet/status` and verify `nodes_online > 0`
2. Check that nodes show `status: "online"`
3. Look at queue depths — deep queues may indicate a bottleneck

### Find which node has a specific model
1. Hit `/fleet/status` and inspect each node's `ollama.models_loaded` (in memory) and `ollama.models_available` (on disk)
2. Or hit `/api/tags` for a flat list of all available models

### Check if a model is loaded (hot) or cold
1. Hit `/api/ps` — models listed here are currently loaded in memory (hot)
2. Models in `/api/tags` but not in `/api/ps` are on disk but not loaded (cold)

### View recent inference activity
1. Hit `/dashboard/api/traces?limit=10` to see the last 10 requests
2. Each trace shows: model, node, score, latency, tokens, whether retry or fallback was used

### Diagnose slow responses
1. Check `/dashboard/api/traces` for high latency entries
2. Check `/fleet/status` for nodes with high queue depths or memory pressure
3. Check if the model had to cold-load (look for low scores in trace `scores_breakdown`)

### Test inference through the fleet
```bash
# OpenAI format
curl -s http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"llama3.3:70b","messages":[{"role":"user","content":"Hello"}],"stream":false}'

# Ollama format
curl -s http://localhost:8080/api/chat \
  -d '{"model":"llama3.3:70b","messages":[{"role":"user","content":"Hello"}],"stream":false}'
```

## Guardrails

- Never restart or stop the Herd router or node agents without explicit user confirmation.
- Never delete or modify files in `~/.fleet-manager/` (contains latency data and logs).
- Do not pull models onto nodes without user confirmation — model downloads can be large (10-100+ GB).
- If a node shows as offline, report it to the user rather than attempting to SSH into the machine.
- If all nodes are saturated, suggest the user check the dashboard rather than attempting to fix it automatically.

## Failure handling

- If curl to the router fails with connection refused, tell the user the Herd router may not be running and suggest `uv run herd` to start it.
- If the fleet status shows 0 nodes online, suggest the user start node agents with `uv run herd-node` on their other devices.
- If a specific API endpoint returns an error, show the user the full error response and suggest checking the JSONL logs at `~/.fleet-manager/logs/herd.jsonl`.
