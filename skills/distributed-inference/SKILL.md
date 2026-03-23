---
name: distributed-inference
description: Run distributed LLM inference across heterogeneous hardware without the complexity. Scatter requests across Apple Silicon Macs, Linux boxes, and any machine running Ollama. Automatic node discovery, thermal-aware scheduling, adaptive capacity learning, and context-aware model placement. No orchestration layer, no container runtime, no shared filesystem — just HTTP and mDNS.
version: 1.0.0
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"globe","requires":{"anyBins":["curl","sqlite3"],"optionalBins":["python3","pip"],"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"]},"os":["darwin","linux"]}}
---

# Distributed Inference

A coordination layer for running LLM inference across heterogeneous machines. Each node is autonomous — it runs its own Ollama, manages its own models, and works fine standalone. The coordinator routes requests to the optimal node using a multi-signal scoring function and records every decision for analysis.

## Install

```bash
pip install ollama-herd
herd              # start the coordinator
herd-node         # start an agent on each node in the topology
```

Package: [`ollama-herd`](https://pypi.org/project/ollama-herd/) | Repo: [github.com/geeks-accelerator/ollama-herd](https://github.com/geeks-accelerator/ollama-herd)

## Architecture

```
Coordinator (:11435)           Node Agents
┌──────────────────┐     ┌──────────────────┐
│ Scoring Engine   │◄────│ Heartbeat + Metrics│  (mDNS or explicit URL)
│ Queue Manager    │     │ Capacity Learner   │
│ Streaming Proxy  │     │ Meeting Detector   │
│ Trace Store      │     │ App Fingerprinter  │
│ Latency Store    │     └──────────────────┘
└──────────────────┘     ┌──────────────────┐
        │                │ Heartbeat + Metrics│  (N nodes)
        ▼                └──────────────────┘
   Ollama instances
   (one per node)
```

Nodes discover the coordinator via mDNS (`_fleet-manager._tcp.local.`) or connect explicitly with `--router-url`. Each node sends heartbeats every 5 seconds containing: CPU utilization, memory usage and pressure classification, disk metrics, loaded models with context lengths, available models, and an optional capacity score from the behavioral model.

## Scoring function

The coordinator evaluates every online node for every request using 7 weighted signals:

| Signal | Max Weight | What it measures |
|--------|-----------|-----------------|
| Thermal state | +50 | Is the model already loaded in GPU memory? Hot (+50), warm (+30), cold (+10) |
| Memory fit | +20 | Available memory headroom relative to model size |
| Queue depth | -30 | Pending + in-flight requests on this node:model pair |
| Wait time | -25 | Estimated wait based on p75 historical latency × queue depth |
| Role affinity | +15 | Large models prefer high-memory nodes; small models prefer small nodes |
| Availability trend | +10 | Capacity learner's prediction of node availability |
| Context fit | +15 | Does the loaded model's context window fit the estimated token count? |

Nodes with insufficient memory, critical pressure, or missing models are eliminated before scoring. The highest-scoring node wins. Ties are broken by the node with the most available memory.

## Adaptive capacity

Nodes optionally learn usage patterns and constrain their availability:

- **168-slot behavioral model** — one slot per hour of the week, learns when the machine is typically free
- **Meeting detection** — detects active camera/microphone on macOS, triggers hard pause
- **App fingerprinting** — classifies current workload intensity from resource signatures (idle/light/moderate/heavy/intensive) without reading app names
- **Dynamic memory ceiling** — maps availability score to how much RAM the coordinator can use

Enable with `FLEET_NODE_ENABLE_CAPACITY_LEARNING=true` on the node agent.

## Context-aware model placement

The coordinator protects against a known Ollama behavior where changing `num_ctx` at runtime triggers a full model reload. For an 89GB model, this causes multi-minute hangs.

- `num_ctx` ≤ loaded context → stripped from the request (model already has sufficient context)
- `num_ctx` > loaded context → searches loaded models across all nodes for one with sufficient context and more parameters, auto-switches if found
- Configurable: `FLEET_CONTEXT_PROTECTION=strip|warn|passthrough`

## API

### Coordinator state
```bash
# Full fleet state
curl -s http://localhost:11435/fleet/status | python3 -m json.tool

# Models across all nodes
curl -s http://localhost:11435/api/tags | python3 -m json.tool

# Models currently in GPU memory
curl -s http://localhost:11435/api/ps | python3 -m json.tool
```

### Inference (OpenAI-compatible)
```bash
curl -s http://localhost:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"llama3.3:70b","messages":[{"role":"user","content":"Hello"}]}'
```

### Inference (Ollama-native)
```bash
curl -s http://localhost:11435/api/chat \
  -d '{"model":"llama3.3:70b","messages":[{"role":"user","content":"Hello"}]}'
```

### Model fallback chains
```bash
curl -s http://localhost:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"llama3.3:70b","fallback_models":["qwen2.5:32b","qwen2.5:7b"],"messages":[{"role":"user","content":"Hello"}]}'
```

### Trace analysis
```bash
# Recent routing decisions
curl -s "http://localhost:11435/dashboard/api/traces?limit=20" | python3 -m json.tool

# Score breakdown for a specific request
sqlite3 ~/.fleet-manager/latency.db "SELECT request_id, model, node_id, score, scores_breakdown FROM request_traces ORDER BY timestamp DESC LIMIT 1"
```

### Node performance comparison
```bash
sqlite3 ~/.fleet-manager/latency.db "SELECT node_id, model, COUNT(*) as n, ROUND(AVG(latency_ms)/1000.0, 1) as avg_s, ROUND(AVG(COALESCE(completion_tokens,0) * 1000.0 / NULLIF(latency_ms,0)), 1) as tok_per_s FROM request_traces WHERE status='completed' GROUP BY node_id, model HAVING n > 10 ORDER BY tok_per_s DESC"
```

### Health and capacity
```bash
curl -s http://localhost:11435/dashboard/api/health | python3 -m json.tool
curl -s http://localhost:11435/dashboard/api/recommendations | python3 -m json.tool
curl -s http://localhost:11435/dashboard/api/usage | python3 -m json.tool
```

### Model lifecycle
```bash
# Per-node model inventory
curl -s http://localhost:11435/dashboard/api/model-management | python3 -m json.tool

# Pull model to a node
curl -s -X POST http://localhost:11435/dashboard/api/pull \
  -H "Content-Type: application/json" \
  -d '{"model": "llama3.3:70b", "node_id": "mac-studio"}'

# Remove model from a node
curl -s -X POST http://localhost:11435/dashboard/api/delete \
  -H "Content-Type: application/json" \
  -d '{"model": "old-model:7b", "node_id": "mac-studio"}'
```

### Configuration
```bash
curl -s http://localhost:11435/dashboard/api/settings | python3 -m json.tool

curl -s -X POST http://localhost:11435/dashboard/api/settings \
  -H "Content-Type: application/json" \
  -d '{"vram_fallback": true}'
```

## Fault tolerance

| Mechanism | Behavior |
|-----------|----------|
| Auto-retry | If a node fails before the first chunk, re-score and retry on next-best node (up to N retries, configurable) |
| Holding queue | When all nodes are saturated, requests queue for up to 30 seconds before timing out |
| Zombie reaper | Background task reclaims in-flight slots stuck longer than 15 minutes |
| VRAM fallback | Routes to a loaded model in the same category rather than cold-loading the requested model |
| Auto-pull | Pulls missing models onto the node with the most available memory |
| Graceful drain | SIGTERM triggers drain: in-flight requests finish, pending requests redistribute |

## Data model

All state is in SQLite at `~/.fleet-manager/latency.db`:

```sql
-- Request traces (every routing decision)
SELECT * FROM request_traces LIMIT 1;
-- Columns: id, request_id, model, original_model, node_id, score, scores_breakdown,
--          status, latency_ms, time_to_first_token_ms, prompt_tokens, completion_tokens,
--          retry_count, fallback_used, excluded_nodes, client_ip, original_format,
--          error_message, timestamp, tags
```

Structured logs at `~/.fleet-manager/logs/herd.jsonl` — daily rotation, 30-day retention, queryable with `grep` and `jq`.

## Dashboard

`http://localhost:11435/dashboard` — eight tabs covering fleet overview, trends, model insights, per-app analytics, benchmarks, health checks, model recommendations, and settings.

## Constraints

- Never restart services or modify `~/.fleet-manager/` without explicit user confirmation.
- Model pull/delete operations require user confirmation (10-100+ GB transfers).
- If the coordinator is unreachable, suggest `herd` or `uv run herd`.
- If no nodes are online, suggest `herd-node` or `uv run herd-node` on target machines.
- If mDNS fails, use `--router-url http://coordinator-ip:11435`.
