---
name: local-llm-router
description: Local LLM model router for Llama, Qwen, DeepSeek, Phi, Mistral, and Gemma across multiple devices. Self-hosted local LLM inference routing on macOS, Linux, and Windows. Local LLM 7-signal scoring engine picks the optimal machine for every local LLM request. OpenAI-compatible local LLM API with context protection, VRAM-aware fallback, and auto-retry. 本地LLM路由 inference router | LLM local enrutador de inferencia. Use when the user wants to optimize local LLM routing, reduce local LLM latency, or load balance local LLM across machines.
version: 1.0.4
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"router","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","sqlite3","pip"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["darwin","linux","windows"]}}
---

# Local LLM Router

You are managing a local LLM inference router that distributes local LLM requests across multiple Ollama instances using a 7-signal local LLM scoring engine.

## What this local LLM router solves

You have multiple machines with GPUs but your local LLM inference scripts only talk to one. Switching local LLM models between machines means editing configs and restarting. There's no way to compare local LLM latency across nodes, no automatic local LLM failover, and no visibility into which machine handles which local LLM requests.

This local LLM router sits in front of your Ollama instances and picks the optimal device for every local LLM request — based on what local LLM models are hot in memory, how much headroom each machine has, how deep the local LLM queues are, and historical local LLM latency data. Drop-in compatible with the OpenAI SDK and Ollama API.

## Setup Local LLM Router

```bash
pip install ollama-herd           # install the local LLM router
herd                              # launch the local LLM router (scores and routes)
herd-node                         # launch a local LLM node agent on each device
```

Package: [`ollama-herd`](https://pypi.org/project/ollama-herd/) | Repo: [github.com/geeks-accelerator/ollama-herd](https://github.com/geeks-accelerator/ollama-herd)

## Local LLM Router Endpoint

The local LLM router runs at `http://localhost:11435` by default. Point any OpenAI-compatible client at `http://localhost:11435/v1` for local LLM inference.

```python
# local_llm_client — connect to the local LLM router
from openai import OpenAI
local_llm_client = OpenAI(base_url="http://localhost:11435/v1", api_key="not-needed")
local_llm_response = local_llm_client.chat.completions.create(
    model="llama3.3:70b",  # local LLM model
    messages=[{"role": "user", "content": "Hello from local LLM"}],
    stream=True,
)
```

## Local LLM Scoring Engine

Every local LLM request is scored across 7 signals:

1. **Thermal state** (+50 pts) — local LLM models already loaded in GPU memory ("hot") score highest
2. **Memory fit** (+20 pts) — local LLM nodes with more available headroom score higher
3. **Queue depth** (-30 pts) — busy local LLM nodes get penalized
4. **Latency history** (-25 pts) — past p75 local LLM latency from SQLite informs expected wait
5. **Role affinity** (+15 pts) — large local LLM models prefer big machines
6. **Availability trend** (+10 pts) — local LLM nodes with stable availability patterns score higher
7. **Context fit** (+15 pts) — local LLM nodes with loaded context windows that fit the estimated token count

## Local LLM Context-size Protection

When clients send `num_ctx` in local LLM requests, the local LLM router intercepts it to prevent Ollama from reloading models unnecessarily:

- `num_ctx` <= loaded context: stripped (local LLM model already supports it)
- `num_ctx` > loaded context: auto-upgrades to a larger loaded local LLM model with sufficient context
- Configurable via `FLEET_CONTEXT_PROTECTION` (strip/warn/passthrough)

## Local LLM API Endpoints

### Local LLM Fleet Status
```bash
# local_llm_fleet_status — all local LLM nodes and queues
curl -s http://localhost:11435/fleet/status | python3 -m json.tool
```

### List all local LLM models across the fleet
```bash
# local_llm_model_list — every local LLM model on every node
curl -s http://localhost:11435/api/tags | python3 -m json.tool
```

### Local LLM models currently loaded in memory (hot)
```bash
# local_llm_hot_models — local LLM models in GPU memory
curl -s http://localhost:11435/api/ps | python3 -m json.tool
```

### OpenAI-compatible local LLM model list
```bash
curl -s http://localhost:11435/v1/models | python3 -m json.tool
```

### Local LLM Request Traces (routing decisions)
```bash
# local_llm_traces — recent local LLM routing decisions
curl -s "http://localhost:11435/dashboard/api/traces?limit=20" | python3 -m json.tool
```

Returns: local LLM model requested, node selected, score breakdown, latency, tokens, retry/fallback status.

### Local LLM Model Performance
```bash
curl -s http://localhost:11435/dashboard/api/models | python3 -m json.tool
```

### Local LLM Usage Statistics
```bash
curl -s http://localhost:11435/dashboard/api/usage | python3 -m json.tool
```

### Local LLM Fleet Health
```bash
curl -s http://localhost:11435/dashboard/api/health | python3 -m json.tool
```

### Local LLM Model Recommendations
```bash
curl -s http://localhost:11435/dashboard/api/recommendations | python3 -m json.tool
```

### Local LLM Settings
```bash
curl -s http://localhost:11435/dashboard/api/settings | python3 -m json.tool

# Toggle local LLM auto-pull
curl -s -X POST http://localhost:11435/dashboard/api/settings \
  -H "Content-Type: application/json" \
  -d '{"auto_pull": false}'
```

### Local LLM Model Management
```bash
# local_llm_model_inventory — per-node local LLM model details
curl -s http://localhost:11435/dashboard/api/model-management | python3 -m json.tool

# Pull a local LLM model onto a specific node
curl -s -X POST http://localhost:11435/dashboard/api/pull \
  -H "Content-Type: application/json" \
  -d '{"model": "llama3.3:70b", "node_id": "mac-studio"}'

# Delete a local LLM model from a specific node
curl -s -X POST http://localhost:11435/dashboard/api/delete \
  -H "Content-Type: application/json" \
  -d '{"model": "old-model:7b", "node_id": "mac-studio"}'
```

### Per-app local LLM analytics
```bash
curl -s http://localhost:11435/dashboard/api/apps | python3 -m json.tool
```

## Local LLM Dashboard

Web dashboard at `http://localhost:11435/dashboard` with eight tabs: Local LLM Fleet Overview, Trends, Local LLM Model Insights, Apps, Benchmarks, Local LLM Health, Recommendations, Settings.

## Optimizing Local LLM Latency

### Find the slowest local LLM model/node combinations
```bash
sqlite3 ~/.fleet-manager/latency.db "SELECT model, node_id, AVG(latency_ms)/1000.0 as avg_secs, COUNT(*) as n FROM request_traces WHERE status='completed' GROUP BY node_id, model HAVING n > 10 ORDER BY avg_secs DESC LIMIT 10"
```

### Check local LLM time-to-first-token
```bash
sqlite3 ~/.fleet-manager/latency.db "SELECT node_id, model, AVG(time_to_first_token_ms) as avg_ttft FROM request_traces WHERE time_to_first_token_ms IS NOT NULL GROUP BY node_id, model"
```

### Compare hot vs cold local LLM load latency
```bash
sqlite3 ~/.fleet-manager/latency.db "SELECT model, CASE WHEN time_to_first_token_ms < 1000 THEN 'hot' ELSE 'cold' END as load_type, AVG(latency_ms)/1000.0 as avg_secs, COUNT(*) as n FROM request_traces WHERE status='completed' AND time_to_first_token_ms IS NOT NULL GROUP BY model, load_type ORDER BY model"
```

### Test local LLM inference
```bash
# local LLM via OpenAI format
curl -s http://localhost:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"llama3.3:70b","messages":[{"role":"user","content":"Hello from local LLM"}],"stream":false}'

# local LLM via Ollama format
curl -s http://localhost:11435/api/chat \
  -d '{"model":"llama3.3:70b","messages":[{"role":"user","content":"Hello from local LLM"}],"stream":false}'
```

## Local LLM Resilience

- **Auto-retry** — re-scores and retries on the next-best local LLM node if failure occurs before the first chunk
- **Local LLM model fallbacks** — specify backup local LLM models; tries alternatives when the primary is unavailable
- **Local LLM context protection** — strips dangerous `num_ctx` values, auto-upgrades to larger local LLM models
- **VRAM-aware local LLM fallback** — routes to an already-loaded local LLM model in the same category
- **Zombie reaper** — detects and cleans up stuck in-flight local LLM requests
- **Local LLM auto-pull** — pulls missing local LLM models onto the best available node

## Local LLM Guardrails

- Never restart or stop the local LLM router or node agents without explicit user confirmation.
- Never delete or modify files in `~/.fleet-manager/` (contains local LLM latency data, traces, and logs).
- Do not pull or delete local LLM models without user confirmation — downloads can be 10-100+ GB.
- If a local LLM node shows as offline, report it rather than attempting to SSH into the machine.

## Local LLM Failure Handling

- Connection refused → local LLM router may not be running, suggest `herd` or `uv run herd`
- 0 local LLM nodes online → suggest starting `herd-node` on devices
- mDNS discovery fails → use `--router-url http://router-ip:11435`
- Local LLM requests hang → check for `num_ctx` in client requests; verify context protection
- Local LLM API errors → check `~/.fleet-manager/logs/herd.jsonl`
