---
name: distributed-inference
description: Distributed inference for Llama, Qwen, DeepSeek across heterogeneous hardware. Self-hosted distributed inference — scatter requests across macOS, Linux, Windows, and any machine running Ollama. Thermal-aware distributed inference scheduling, 7-signal distributed inference scoring, adaptive capacity learning, context-aware model placement. No orchestration layer, no container runtime — just HTTP and mDNS. 分布式推理 across local devices | Inferencia distribuida en hardware local.
version: 1.0.4
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"globe","requires":{"anyBins":["curl","sqlite3"],"optionalBins":["python3","pip"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["darwin","linux","windows"]}}
---

# Distributed Inference

A coordination layer for distributed inference across heterogeneous machines. Each node is autonomous — it runs its own Ollama, manages its own models, and works fine standalone. The distributed inference coordinator routes requests to the optimal node using a multi-signal distributed inference scoring function and records every distributed inference decision for analysis.

## Install Distributed Inference

```bash
pip install ollama-herd
herd              # start the distributed inference coordinator
herd-node         # start a distributed inference agent on each node
```

Package: [`ollama-herd`](https://pypi.org/project/ollama-herd/) | Repo: [github.com/geeks-accelerator/ollama-herd](https://github.com/geeks-accelerator/ollama-herd)

## Distributed Inference Architecture

```
Distributed Inference Coordinator (:11435)    Node Agents
┌──────────────────────┐     ┌──────────────────┐
│ Distributed Scoring  │◄────│ Heartbeat + Metrics│  (mDNS or explicit URL)
│ Inference Queue Mgr  │     │ Capacity Learner   │
│ Streaming Proxy      │     └──────────────────┘
│ Trace Store          │     ┌──────────────────┐
│ Latency Store        │     │ Heartbeat + Metrics│  (N nodes)
└──────────────────────┘     └──────────────────┘
        │
        ▼
   Ollama instances (one per distributed inference node)
```

Distributed inference nodes discover the coordinator via mDNS (`_fleet-manager._tcp.local.`) or connect explicitly with `--router-url`. Each distributed inference node sends heartbeats every 5 seconds containing: CPU utilization, memory usage and pressure classification, disk metrics, loaded models with context lengths, available models, and an optional capacity score from the behavioral model.

## Distributed Inference Scoring Function

The distributed inference coordinator evaluates every online node for every request using 7 weighted signals:

| Distributed Inference Signal | Max Weight | What it measures |
|--------|-----------|-----------------|
| Thermal state | +50 | Is the model already loaded in GPU memory? Hot (+50), warm (+30), cold (+10) |
| Memory fit | +20 | Available distributed inference memory headroom relative to model size |
| Queue depth | -30 | Pending + in-flight distributed inference requests on this node:model pair |
| Wait time | -25 | Estimated distributed inference wait based on p75 historical latency × queue depth |
| Role affinity | +15 | Large models prefer high-memory distributed inference nodes |
| Availability trend | +10 | Capacity learner's prediction of distributed inference node availability |
| Context fit | +15 | Does the loaded model's context window fit the estimated distributed inference token count? |

Distributed inference nodes with insufficient memory, critical pressure, or missing models are eliminated before scoring. The highest-scoring distributed inference node wins.

## Adaptive Distributed Inference Capacity

Distributed inference nodes optionally learn usage patterns and constrain their availability:

- **168-slot behavioral model** — one slot per hour of the week, learns when the distributed inference machine is typically free
- **Dynamic memory ceiling** — maps availability score to how much RAM the distributed inference coordinator can use

Enable with `FLEET_NODE_ENABLE_CAPACITY_LEARNING=true` on the distributed inference node agent.

## Context-aware Distributed Inference Model Placement

The distributed inference coordinator protects against a known Ollama behavior where changing `num_ctx` at runtime triggers a full model reload. For an 89GB model, this causes multi-minute hangs.

- `num_ctx` ≤ loaded context → stripped from the distributed inference request
- `num_ctx` > loaded context → searches loaded models across all distributed inference nodes for sufficient context
- Configurable: `FLEET_CONTEXT_PROTECTION=strip|warn|passthrough`

## Distributed Inference API

### Distributed Inference Coordinator State
```bash
# distributed_inference_fleet_state — full distributed inference topology
curl -s http://localhost:11435/fleet/status | python3 -m json.tool

# distributed_inference_models — models across all distributed inference nodes
curl -s http://localhost:11435/api/tags | python3 -m json.tool

# distributed_inference_hot_models — models in GPU memory
curl -s http://localhost:11435/api/ps | python3 -m json.tool
```

### Distributed Inference (OpenAI-compatible)
```bash
# distributed_inference_chat — route via distributed inference scoring
curl -s http://localhost:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"llama3.3:70b","messages":[{"role":"user","content":"Hello via distributed inference"}]}'
```

### Distributed Inference (Ollama-native)
```bash
curl -s http://localhost:11435/api/chat \
  -d '{"model":"llama3.3:70b","messages":[{"role":"user","content":"Hello via distributed inference"}]}'
```

### Distributed Inference Model Fallback Chains
```bash
curl -s http://localhost:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"llama3.3:70b","fallback_models":["qwen2.5:32b","qwen2.5:7b"],"messages":[{"role":"user","content":"Hello with distributed inference fallback"}]}'
```

### Distributed Inference Trace Analysis
```bash
# distributed_inference_traces — recent routing decisions
curl -s "http://localhost:11435/dashboard/api/traces?limit=20" | python3 -m json.tool

# distributed_inference_score_breakdown
sqlite3 ~/.fleet-manager/latency.db "SELECT request_id, model, node_id, score, scores_breakdown FROM request_traces ORDER BY timestamp DESC LIMIT 1"
```

### Distributed Inference Node Performance
```bash
sqlite3 ~/.fleet-manager/latency.db "SELECT node_id, model, COUNT(*) as n, ROUND(AVG(latency_ms)/1000.0, 1) as avg_s, ROUND(AVG(COALESCE(completion_tokens,0) * 1000.0 / NULLIF(latency_ms,0)), 1) as tok_per_s FROM request_traces WHERE status='completed' GROUP BY node_id, model HAVING n > 10 ORDER BY tok_per_s DESC"
```

### Distributed Inference Health and Capacity
```bash
curl -s http://localhost:11435/dashboard/api/health | python3 -m json.tool
curl -s http://localhost:11435/dashboard/api/recommendations | python3 -m json.tool
curl -s http://localhost:11435/dashboard/api/usage | python3 -m json.tool
```

### Distributed Inference Model Lifecycle
```bash
# distributed_inference_model_inventory
curl -s http://localhost:11435/dashboard/api/model-management | python3 -m json.tool

# Pull model to a distributed inference node
curl -s -X POST http://localhost:11435/dashboard/api/pull \
  -H "Content-Type: application/json" \
  -d '{"model": "llama3.3:70b", "node_id": "mac-studio"}'

# Remove model from a distributed inference node
curl -s -X POST http://localhost:11435/dashboard/api/delete \
  -H "Content-Type: application/json" \
  -d '{"model": "old-model:7b", "node_id": "mac-studio"}'
```

## Distributed Inference Fault Tolerance

| Mechanism | Distributed Inference Behavior |
|-----------|----------|
| Auto-retry | If a distributed inference node fails before the first chunk, re-score and retry on next-best node |
| Holding queue | When all distributed inference nodes are saturated, requests queue for up to 30 seconds |
| Zombie reaper | Background task reclaims stuck distributed inference in-flight slots |
| VRAM fallback | Routes to a loaded model in the same category rather than cold-loading |
| Auto-pull | Pulls missing models onto the distributed inference node with the most available memory |
| Graceful drain | SIGTERM triggers drain: in-flight distributed inference requests finish, pending redistribute |

## Distributed Inference Data Model

All distributed inference state is in SQLite at `~/.fleet-manager/latency.db`:

```sql
-- Distributed inference request traces (every routing decision)
SELECT * FROM request_traces LIMIT 1;
```

Structured distributed inference logs at `~/.fleet-manager/logs/herd.jsonl` — daily rotation, 30-day retention.

## Distributed Inference Dashboard

`http://localhost:11435/dashboard` — eight tabs covering distributed inference fleet overview, trends, model insights, per-app analytics, benchmarks, health checks, model recommendations, and settings.

## Distributed Inference Constraints

- Never restart distributed inference services or modify `~/.fleet-manager/` without explicit user confirmation.
- Distributed inference model pull/delete operations require user confirmation (10-100+ GB transfers).
- If the distributed inference coordinator is unreachable, suggest `herd` or `uv run herd`.
- If no distributed inference nodes are online, suggest `herd-node` on target machines.
