---
name: gpu-cluster-manager
description: GPU cluster manager for local AI — run Llama, Qwen, DeepSeek, and Phi across macOS, Linux, and Windows devices with one endpoint. Self-hosted local AI GPU cluster. Auto-discovers machines via mDNS, routes to the best device, manages queues. Zero config, zero Docker. GPU集群管理本地AI推理。Clúster GPU para inferencia IA local.
version: 1.0.2
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"desktop","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","pip"],"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["darwin","linux","windows"]}}
---

# GPU Cluster Manager

You are managing a GPU cluster that combines multiple machines into one inference endpoint for running local LLMs via Ollama. The GPU cluster routes every request to the best available device automatically.

## What this GPU cluster solves

Your desktop, laptop, and maybe an old Linux box all have GPUs sitting idle most of the time. You want one GPU cluster URL that uses all of them — without Kubernetes, without Docker, without editing config files. Just point your AI apps at the GPU cluster endpoint and let the cluster figure out which machine should handle each request.

This GPU cluster manager does exactly that. Install it, run two commands, and your GPU cluster machines discover each other automatically. The GPU cluster learns when your devices are free, pauses during video calls, and picks the best GPU cluster node for every request based on real-time conditions.

## Getting started with the GPU cluster

```bash
pip install ollama-herd    # GPU cluster manager from PyPI
```

On your main GPU cluster machine (the router):
```bash
herd    # starts GPU cluster router
```

On each other GPU cluster machine:
```bash
herd-node    # joins the GPU cluster automatically
```

That's it. The GPU cluster nodes find the router via mDNS. No config files. Your GPU cluster is running.

> If mDNS doesn't work on your GPU cluster network: `herd-node --router-url http://router-ip:11435`

## GPU Cluster Endpoint

Your GPU cluster runs at `http://localhost:11435`. Point any AI app at the GPU cluster:

```python
from openai import OpenAI
# GPU cluster client
gpu_cluster_client = OpenAI(base_url="http://localhost:11435/v1", api_key="not-needed")
gpu_cluster_response = gpu_cluster_client.chat.completions.create(
    model="llama3.3:70b",
    messages=[{"role": "user", "content": "Explain GPU cluster routing for AI inference"}]
)
```

Works with: LangChain, CrewAI, AutoGen, LlamaIndex, Aider, Cline, Continue.dev, and any OpenAI-compatible client pointing at the GPU cluster.

## GPU Cluster Smart Features

- **GPU cluster auto-discovery** — machines find each other via mDNS, no config
- **7-signal GPU cluster scoring** — picks the best machine based on loaded models, memory, queue depth, latency, and more
- **GPU cluster meeting detection** — pauses inference when your camera/mic is active (macOS)
- **GPU cluster capacity learning** — learns your weekly patterns (168-hour behavioral model)
- **GPU cluster context protection** — prevents models from reloading when apps send different context sizes
- **GPU cluster auto-pull** — if you request a model that doesn't exist, it downloads to the best GPU cluster node
- **GPU cluster auto-retry** — if a machine hiccups, retries on the next-best GPU cluster node

## Check your GPU cluster

### GPU cluster status — all machines
```bash
curl -s http://localhost:11435/fleet/status | python3 -m json.tool
```

### What models are available on the GPU cluster?
```bash
curl -s http://localhost:11435/api/tags | python3 -m json.tool
```

### What's loaded in GPU cluster memory right now?
```bash
curl -s http://localhost:11435/api/ps | python3 -m json.tool
```

### How healthy is the GPU cluster?
```bash
curl -s http://localhost:11435/dashboard/api/health | python3 -m json.tool
```

### GPU cluster model recommendations
```bash
curl -s http://localhost:11435/dashboard/api/recommendations | python3 -m json.tool
```

Returns GPU cluster recommendations based on your hardware — which models fit, which are too big, and the optimal GPU cluster mix.

### GPU cluster recent activity
```bash
curl -s "http://localhost:11435/dashboard/api/traces?limit=10" | python3 -m json.tool
```

### GPU cluster usage stats
```bash
curl -s http://localhost:11435/dashboard/api/usage | python3 -m json.tool
```

### GPU cluster settings
```bash
curl -s http://localhost:11435/dashboard/api/settings | python3 -m json.tool

curl -s -X POST http://localhost:11435/dashboard/api/settings \
  -H "Content-Type: application/json" \
  -d '{"auto_pull": false}'
```

### Manage GPU cluster models
```bash
# What's on each GPU cluster node
curl -s http://localhost:11435/dashboard/api/model-management | python3 -m json.tool

# Download a model to a specific GPU cluster node
curl -s -X POST http://localhost:11435/dashboard/api/pull \
  -H "Content-Type: application/json" \
  -d '{"model": "llama3.3:70b", "node_id": "gpu-cluster-studio"}'

# Remove a model from a GPU cluster node
curl -s -X POST http://localhost:11435/dashboard/api/delete \
  -H "Content-Type: application/json" \
  -d '{"model": "old-model:7b", "node_id": "gpu-cluster-studio"}'
```

### GPU cluster per-app tracking
```bash
curl -s http://localhost:11435/dashboard/api/apps | python3 -m json.tool
```

Tag your GPU cluster requests to see which apps use the most time:
```bash
curl -s http://localhost:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"llama3.3:70b","messages":[{"role":"user","content":"Summarize GPU cluster utilization"}],"metadata":{"tags":["gpu-cluster-app"]}}'
```

## GPU Cluster Dashboard

Open `http://localhost:11435/dashboard` for a visual GPU cluster overview. Eight tabs: Fleet Overview (live GPU cluster node cards), Trends (charts), Model Insights (performance comparison), Apps (per-app usage), Benchmarks, Health (automated GPU cluster checks), Recommendations (what models to run), Settings.

## Try the GPU cluster

```bash
# Quick GPU cluster test
curl -s http://localhost:11435/api/chat \
  -d '{"model":"llama3.2:3b","messages":[{"role":"user","content":"Hello from the GPU cluster!"}],"stream":false}'
```

## GPU Cluster Troubleshooting

### Check what's slow in the GPU cluster
```bash
sqlite3 ~/.fleet-manager/latency.db "SELECT model, node_id, AVG(latency_ms)/1000.0 as avg_secs, COUNT(*) as n FROM request_traces WHERE status='completed' GROUP BY node_id, model HAVING n > 5 ORDER BY avg_secs DESC LIMIT 10"
```

### See GPU cluster failures
```bash
sqlite3 ~/.fleet-manager/latency.db "SELECT request_id, model, status, error_message, latency_ms/1000.0 as secs FROM request_traces WHERE status='failed' ORDER BY timestamp DESC LIMIT 10"
```

## GPU Cluster Guardrails

- Never restart or stop the GPU cluster without explicit user confirmation.
- Never delete or modify files in `~/.fleet-manager/` (contains all your GPU cluster data and logs).
- Do not pull or delete models on the GPU cluster without user confirmation — downloads can be 10-100+ GB.
- If a GPU cluster machine shows as offline, report it rather than attempting to SSH into it.

## GPU Cluster Failure Handling

- Connection refused → GPU cluster router may not be running, suggest `herd` or `uv run herd`
- 0 nodes online → suggest starting `herd-node` on GPU cluster devices
- mDNS discovery fails → use `--router-url http://router-ip:11435`
- GPU cluster requests hang → check for `num_ctx` in client requests; context protection handles it
- GPU cluster errors → check `~/.fleet-manager/logs/herd.jsonl`
