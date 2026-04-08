---
name: ollama-herd
description: Ollama multimodal model router for Llama, Qwen, DeepSeek, Phi, and Mistral — plus mflux image generation, speech-to-text, and embeddings. Self-hosted Ollama local AI (macOS, Linux, Windows) with 7-signal scoring, Ollama queue management, real-time dashboard, and Ollama health monitoring. Routes Ollama LLM, image, STT, and embedding requests across macOS, Linux, and Windows devices. Ollama本地推理路由 | Ollama enrutador IA local. Use when the user asks about their Ollama fleet, Ollama inference routing, Ollama node status, or Ollama fleet performance.
version: 1.5.3
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"llama","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","sqlite3","pip"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["darwin","linux","windows"]}}
---

# Ollama Herd Fleet Manager

You are managing an Ollama Herd fleet — a smart Ollama multimodal router that distributes Ollama AI workloads across multiple devices. Ollama Herd handles 4 model types: Ollama LLM inference, image generation (mflux), speech-to-text (Qwen3-ASR), and Ollama embeddings. The Ollama scoring engine evaluates nodes on 7 signals (thermal state, memory fit, queue depth, latency history, role affinity, availability trend, context fit) and routes each Ollama request to the optimal device.

## Install Ollama Herd

```bash
pip install ollama-herd          # install Ollama Herd from PyPI
herd                             # start the Ollama router
herd-node                        # start an Ollama node agent (run on each device)
```

PyPI: [`ollama-herd`](https://pypi.org/project/ollama-herd/) | Source: [github.com/geeks-accelerator/ollama-herd](https://github.com/geeks-accelerator/ollama-herd)

## Ollama Router endpoint

The Ollama Herd router runs at `http://localhost:11435` by default. If the user has specified a different Ollama URL, use that instead.

## Ollama API endpoints

Use curl to interact with the Ollama fleet:

### Ollama fleet status — overview of all Ollama nodes and queues
```bash
# ollama_fleet_status — check Ollama node health
curl -s http://localhost:11435/fleet/status | python3 -m json.tool
```

Returns:
- `fleet.nodes_total` / `fleet.nodes_online` — how many Ollama devices are in the fleet
- `fleet.models_loaded` — total Ollama models currently loaded across all nodes
- `fleet.requests_active` — total in-flight Ollama requests
- `nodes[]` — per-node details: Ollama status, hardware, memory, CPU, disk, loaded Ollama models with context lengths
- `queues` — per Ollama node:model queue depths (pending, in-flight, done, failed)

### List all Ollama models available across the fleet
```bash
# ollama_model_list — all Ollama models on all nodes
curl -s http://localhost:11435/api/tags | python3 -m json.tool
```

### Pull an Ollama model onto the fleet
```bash
# ollama_pull_model — pull a model (auto-selects best node, streams progress)
curl -N http://localhost:11435/api/pull -d '{"name": "codestral"}'

# pull to a specific node
curl -N http://localhost:11435/api/pull -d '{"name": "llama3.3:70b", "node_id": "mac-studio"}'

# non-streaming (blocks until complete)
curl http://localhost:11435/api/pull -d '{"name": "phi4", "stream": false}'
```

### List Ollama models currently loaded in memory
```bash
# ollama_loaded_models — hot Ollama models in GPU memory
curl -s http://localhost:11435/api/ps | python3 -m json.tool
```

### OpenAI-compatible Ollama model list
```bash
curl -s http://localhost:11435/v1/models | python3 -m json.tool
```

### Ollama usage statistics (per-node, per-model daily aggregates)
```bash
curl -s http://localhost:11435/dashboard/api/usage | python3 -m json.tool
```

### Recent Ollama request traces
```bash
# ollama_traces — recent Ollama routing decisions
curl -s "http://localhost:11435/dashboard/api/traces?limit=20" | python3 -m json.tool
```

Returns the last N Ollama routing decisions with: model requested, node selected, score, latency, tokens, retry/fallback status, tags.

### Ollama fleet health analysis
```bash
curl -s http://localhost:11435/dashboard/api/health | python3 -m json.tool
```

Returns 15 automated Ollama health checks: offline/degraded nodes, memory pressure, underutilized nodes, VRAM fallbacks, KV cache bloat (OLLAMA_NUM_PARALLEL too high), version mismatch, context protection, zombie reaper, Ollama model thrashing, request timeouts, error rates, retry rates, client disconnects, and incomplete streams.

### Ollama model recommendations
```bash
curl -s http://localhost:11435/dashboard/api/recommendations | python3 -m json.tool
```

Returns AI-powered Ollama model mix recommendations per node based on hardware capabilities, Ollama usage patterns, and curated benchmark data.

### Ollama settings
```bash
# View current Ollama config and node versions
curl -s http://localhost:11435/dashboard/api/settings | python3 -m json.tool

# Toggle Ollama runtime settings (auto_pull, vram_fallback)
curl -s -X POST http://localhost:11435/dashboard/api/settings \
  -H "Content-Type: application/json" \
  -d '{"auto_pull": false}'
```

### Ollama model management
```bash
# View per-node Ollama model details with sizes and usage
curl -s http://localhost:11435/dashboard/api/model-management | python3 -m json.tool

# Pull an Ollama model onto a specific node
curl -s -X POST http://localhost:11435/dashboard/api/pull \
  -H "Content-Type: application/json" \
  -d '{"model": "llama3.3:70b", "node_id": "mac-studio"}'

# Delete an Ollama model from a specific node
curl -s -X POST http://localhost:11435/dashboard/api/delete \
  -H "Content-Type: application/json" \
  -d '{"model": "old-model:7b", "node_id": "mac-studio"}'
```

### Ollama model insights (summary statistics)
```bash
curl -s http://localhost:11435/dashboard/api/models | python3 -m json.tool
```

### Per-app Ollama analytics (requires request tagging)
```bash
curl -s http://localhost:11435/dashboard/api/apps | python3 -m json.tool
```

## Ollama Dashboard

The Ollama web dashboard is at `http://localhost:11435/dashboard`. It has eight tabs:
- **Fleet Overview** — live Ollama node cards, queue depths, and request counts via SSE
- **Trends** — Ollama requests per hour, average latency, and token throughput charts (24h–7d)
- **Model Insights** — per-Ollama-model latency, tokens/sec, usage comparison
- **Apps** — per-tag Ollama analytics with request volume, latency, tokens, error rates
- **Benchmarks** — Ollama capacity growth over time with per-run throughput and latency percentiles
- **Health** — 15 automated Ollama fleet health checks with severity levels
- **Recommendations** — Ollama model mix recommendations per node with one-click pull
- **Settings** — Ollama runtime toggle switches, read-only config tables, and node version tracking

Direct the user to open this URL in their browser for visual Ollama monitoring.

## Ollama Resilience features

- **Auto-retry** — if an Ollama node fails before the first response chunk, re-scores and retries on the next-best Ollama node (up to 2 retries)
- **Ollama model fallbacks** — clients specify backup Ollama models; tries alternatives when the primary is unavailable
- **Context protection** — strips `num_ctx` from Ollama requests when unnecessary to prevent Ollama model reload hangs; auto-upgrades to a larger loaded model
- **VRAM-aware fallback** — routes to an already-loaded Ollama model in the same category instead of cold-loading
- **Zombie reaper** — background task detects and cleans up stuck in-flight Ollama requests
- **Auto-pull** — automatically pulls missing Ollama models onto the best available node

## Common Ollama tasks

### Check if the Ollama fleet is healthy
1. Hit `/fleet/status` and verify `nodes_online > 0`
2. Hit `/dashboard/api/health` for automated Ollama health checks with severity levels
3. Look at Ollama queue depths — deep queues may indicate a bottleneck

### Find which Ollama node has a specific model
1. Hit `/fleet/status` and inspect each Ollama node's `ollama.models_loaded` and `ollama.models_available`
2. Or hit `/api/tags` for a flat list of all available Ollama models with which nodes have them

### Check if an Ollama model is loaded (hot) or cold
1. Hit `/api/ps` — Ollama models listed here are currently loaded in memory (hot)
2. Models in `/api/tags` but not in `/api/ps` are on disk but not loaded (cold)

### View recent Ollama inference activity
1. Hit `/dashboard/api/traces?limit=10` to see the last 10 Ollama requests
2. Each trace shows: Ollama model, node, score, latency, tokens, retry/fallback status

### Diagnose slow Ollama responses
1. Check `/dashboard/api/traces` for high latency Ollama entries
2. Check `/fleet/status` for Ollama nodes with high queue depths or memory pressure
3. Check if the Ollama model had to cold-load (look for low scores in trace)
4. Check if `num_ctx` is being sent — Ollama context protection logs show if requests triggered reloads

### Query the Ollama trace database directly
```bash
# Recent Ollama failures
sqlite3 ~/.fleet-manager/latency.db "SELECT request_id, model, status, error_message FROM request_traces WHERE status='failed' ORDER BY timestamp DESC LIMIT 10"

# Slowest Ollama requests
sqlite3 ~/.fleet-manager/latency.db "SELECT model, node_id, latency_ms/1000.0 as secs FROM request_traces WHERE status='completed' ORDER BY latency_ms DESC LIMIT 10"
```

### Test Ollama inference through the fleet
```bash
# Ollama via OpenAI format
curl -s http://localhost:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"llama3.3:70b","messages":[{"role":"user","content":"Hello from Ollama"}],"stream":false}'

# Ollama native format
curl -s http://localhost:11435/api/chat \
  -d '{"model":"llama3.3:70b","messages":[{"role":"user","content":"Hello from Ollama"}],"stream":false}'
```

## Ollama Guardrails

- Never restart or stop the Ollama Herd router or Ollama node agents without explicit user confirmation.
- Never delete or modify files in `~/.fleet-manager/` (contains Ollama latency data, traces, and logs).
- Do not pull Ollama models onto nodes without user confirmation — Ollama model downloads can be large (10-100+ GB).
- Do not delete Ollama models without user confirmation.
- If an Ollama node shows as offline, report it to the user rather than attempting to SSH into the machine.

## Ollama Failure handling

- If curl to the Ollama router fails with connection refused, tell the user the Ollama Herd router may not be running and suggest `herd` to start it.
- If the Ollama fleet status shows 0 nodes online, suggest starting Ollama node agents with `herd-node` on their devices.
- If Ollama mDNS discovery fails, suggest using `--router-url http://router-ip:11435` for explicit connection.
- If Ollama requests hang with 0 bytes returned, check if the client is sending `num_ctx` — Ollama context protection should strip it.
- If a specific Ollama API endpoint returns an error, show the user the full error response and suggest checking the Ollama JSONL logs at `~/.fleet-manager/logs/herd.jsonl`.
