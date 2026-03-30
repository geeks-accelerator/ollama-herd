---
name: local-llm-router
description: Local LLM model router for Llama, Qwen, DeepSeek, Phi, Mistral, and Gemma across multiple devices. Self-hosted local AI inference routing on Mac Studio, Mac Mini, MacBook Pro, and Linux. 7-signal scoring engine picks the optimal machine for every request. OpenAI-compatible API with context protection, VRAM-aware fallback, and auto-retry. Use when the user wants to optimize inference routing, reduce latency, or load balance Ollama across machines.
version: 1.0.2
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"router","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","sqlite3","pip"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["darwin","linux"]}}
---

# Local LLM Router

You are managing a local LLM inference router that distributes requests across multiple Ollama instances using a 7-signal scoring engine.

## What this solves

You have multiple machines with GPUs but your inference scripts only talk to one. Switching models between machines means editing configs and restarting. There's no way to compare latency across nodes, no automatic failover, and no visibility into which machine handles which requests.

This router sits in front of your Ollama instances and picks the optimal device for every request — based on what models are hot in memory, how much headroom each machine has, how deep the queues are, and historical latency data. Drop-in compatible with the OpenAI SDK and Ollama API.

## Setup

```bash
pip install ollama-herd
herd              # launch the router (scores and routes requests)
herd-node         # launch a node agent on each inference device
```

Package: [`ollama-herd`](https://pypi.org/project/ollama-herd/) | Repo: [github.com/geeks-accelerator/ollama-herd](https://github.com/geeks-accelerator/ollama-herd)

## Router endpoint

The router runs at `http://localhost:11435` by default. Point any OpenAI-compatible client at `http://localhost:11435/v1`.

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:11435/v1", api_key="not-needed")
response = client.chat.completions.create(
    model="llama3.3:70b",
    messages=[{"role": "user", "content": "Hello"}],
    stream=True,
)
```

## Scoring engine

Every request is scored across 7 signals:

1. **Thermal state** (+50 pts) — models already loaded in GPU memory ("hot") score highest
2. **Memory fit** (+20 pts) — nodes with more available headroom score higher
3. **Queue depth** (-30 pts) — busy nodes get penalized
4. **Latency history** (-25 pts) — past p75 latency from SQLite informs expected wait time
5. **Role affinity** (+15 pts) — large models prefer big machines, small models prefer small ones
6. **Availability trend** (+10 pts) — nodes with stable availability patterns score higher
7. **Context fit** (+15 pts) — nodes with loaded context windows that fit the estimated token count score higher

## Context-size protection

When clients send `num_ctx` in requests, the router intercepts it to prevent Ollama from reloading models unnecessarily:

- `num_ctx` <= loaded context: stripped (model already supports it, prevents expensive reload)
- `num_ctx` > loaded context: auto-upgrades to a larger loaded model with sufficient context, if available
- Configurable via `FLEET_CONTEXT_PROTECTION` (strip/warn/passthrough)

## Available API endpoints

### Fleet status
```bash
curl -s http://localhost:11435/fleet/status | python3 -m json.tool
```

### List all models across the fleet
```bash
curl -s http://localhost:11435/api/tags | python3 -m json.tool
```

### Models currently loaded in memory (hot)
```bash
curl -s http://localhost:11435/api/ps | python3 -m json.tool
```

### OpenAI-compatible model list
```bash
curl -s http://localhost:11435/v1/models | python3 -m json.tool
```

### Request traces (routing decisions)
```bash
curl -s "http://localhost:11435/dashboard/api/traces?limit=20" | python3 -m json.tool
```

Returns: model requested, node selected, score breakdown, latency, tokens, retry/fallback status, tags.

### Model performance statistics
```bash
curl -s http://localhost:11435/dashboard/api/models | python3 -m json.tool
```

Returns per-model: total requests, average latency, tokens/sec, prompt and completion token counts.

### Usage statistics
```bash
curl -s http://localhost:11435/dashboard/api/usage | python3 -m json.tool
```

### Fleet health analysis
```bash
curl -s http://localhost:11435/dashboard/api/health | python3 -m json.tool
```

### Model recommendations
```bash
curl -s http://localhost:11435/dashboard/api/recommendations | python3 -m json.tool
```

### Settings
```bash
curl -s http://localhost:11435/dashboard/api/settings | python3 -m json.tool

curl -s -X POST http://localhost:11435/dashboard/api/settings \
  -H "Content-Type: application/json" \
  -d '{"auto_pull": false}'
```

### Model management
```bash
curl -s http://localhost:11435/dashboard/api/model-management | python3 -m json.tool

curl -s -X POST http://localhost:11435/dashboard/api/pull \
  -H "Content-Type: application/json" \
  -d '{"model": "llama3.3:70b", "node_id": "mac-studio"}'

curl -s -X POST http://localhost:11435/dashboard/api/delete \
  -H "Content-Type: application/json" \
  -d '{"model": "old-model:7b", "node_id": "mac-studio"}'
```

### Per-app analytics
```bash
curl -s http://localhost:11435/dashboard/api/apps | python3 -m json.tool
```

## Dashboard

Web dashboard at `http://localhost:11435/dashboard` with eight tabs: Fleet Overview, Trends, Model Insights, Apps, Benchmarks, Health, Recommendations, Settings.

## Optimizing latency

### Find the slowest model/node combinations
```bash
sqlite3 ~/.fleet-manager/latency.db "SELECT model, node_id, AVG(latency_ms)/1000.0 as avg_secs, COUNT(*) as n FROM request_traces WHERE status='completed' GROUP BY node_id, model HAVING n > 10 ORDER BY avg_secs DESC LIMIT 10"
```

### Check time-to-first-token distribution
```bash
sqlite3 ~/.fleet-manager/latency.db "SELECT node_id, model, AVG(time_to_first_token_ms) as avg_ttft, MIN(time_to_first_token_ms) as min_ttft FROM request_traces WHERE time_to_first_token_ms IS NOT NULL GROUP BY node_id, model"
```

### Compare hot vs cold load latency
```bash
sqlite3 ~/.fleet-manager/latency.db "SELECT model, CASE WHEN time_to_first_token_ms < 1000 THEN 'hot' ELSE 'cold' END as load_type, AVG(latency_ms)/1000.0 as avg_secs, COUNT(*) as n FROM request_traces WHERE status='completed' AND time_to_first_token_ms IS NOT NULL GROUP BY model, load_type ORDER BY model"
```

### Test inference
```bash
curl -s http://localhost:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"llama3.3:70b","messages":[{"role":"user","content":"Hello"}],"stream":false}'

curl -s http://localhost:11435/api/chat \
  -d '{"model":"llama3.3:70b","messages":[{"role":"user","content":"Hello"}],"stream":false}'
```

## Resilience

- **Auto-retry** — re-scores and retries on the next-best node if a node fails before the first chunk (up to 2 retries)
- **Model fallbacks** — specify backup models; tries alternatives when the primary is unavailable
- **Context protection** — strips dangerous `num_ctx` values, auto-upgrades to larger models when needed
- **VRAM-aware fallback** — routes to an already-loaded model in the same category instead of cold-loading
- **Zombie reaper** — detects and cleans up stuck in-flight requests
- **Auto-pull** — pulls missing models onto the best available node automatically

## Guardrails

- Never restart or stop the router or node agents without explicit user confirmation.
- Never delete or modify files in `~/.fleet-manager/` (contains latency data, traces, and logs).
- Do not pull or delete models without user confirmation — downloads can be 10-100+ GB.
- If a node shows as offline, report it rather than attempting to SSH into the machine.

## Failure handling

- Connection refused → router may not be running, suggest `herd` or `uv run herd`
- 0 nodes online → suggest starting `herd-node` or `uv run herd-node` on devices
- mDNS discovery fails → use `--router-url http://router-ip:11435`
- Requests hang with 0 bytes → check for `num_ctx` in client requests; verify context protection with `grep "Context protection" ~/.fleet-manager/logs/herd.jsonl`
- API errors → check `~/.fleet-manager/logs/herd.jsonl`
