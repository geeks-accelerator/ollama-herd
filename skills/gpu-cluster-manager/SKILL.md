---
name: gpu-cluster-manager
description: Turn your spare GPUs into one inference endpoint. Auto-discovers machines on your network, routes requests to the best available device, learns when your machines are free, and pauses during meetings. Zero config, zero Docker — pip install and two commands. Use when the user wants to use multiple machines for local AI inference, manage a home lab GPU cluster, or combine Apple Silicon devices into a fleet.
version: 1.0.0
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"desktop","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","pip"],"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"]},"os":["darwin","linux"]}}
---

# GPU Cluster Manager

You are managing a GPU cluster that combines multiple machines into one inference endpoint for running local LLMs via Ollama.

## What this solves

Your Mac Studio, MacBook, and maybe an old Linux box all have GPUs sitting idle most of the time. You want one URL that uses all of them — without Kubernetes, without Docker, without editing config files. Just point your AI apps at one endpoint and let the cluster figure out which machine should handle each request.

This tool does exactly that. Install it, run two commands, and your machines discover each other automatically. It learns when your devices are free, pauses during video calls, and picks the best machine for every request based on what's actually happening right now.

## Getting started

```bash
pip install ollama-herd
```

On your main machine (the router):
```bash
herd
```

On each other machine:
```bash
herd-node
```

That's it. The nodes find the router via mDNS. No config files. Your cluster is running.

> If mDNS doesn't work on your network: `herd-node --router-url http://router-ip:11435`

## Endpoint

Your cluster runs at `http://localhost:11435`. Point any AI app there:

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:11435/v1", api_key="not-needed")
```

Works with: LangChain, CrewAI, AutoGen, LlamaIndex, Aider, Cline, Continue.dev, and any OpenAI-compatible client.

## Smart features

- **Auto-discovery** — machines find each other via mDNS, no config
- **7-signal scoring** — picks the best machine based on what models are loaded, memory, queue depth, latency, and more
- **Meeting detection** — pauses inference when your camera/mic is active (macOS)
- **Capacity learning** — learns your weekly patterns (168-hour behavioral model) to predict when machines are available
- **Context protection** — prevents models from reloading when apps send different context sizes
- **Auto-pull** — if you request a model that doesn't exist, it downloads automatically to the best machine
- **Auto-retry** — if a machine hiccups, retries on the next-best one before you notice

## Check your cluster

### See all machines and their status
```bash
curl -s http://localhost:11435/fleet/status | python3 -m json.tool
```

### What models are available?
```bash
curl -s http://localhost:11435/api/tags | python3 -m json.tool
```

### What's loaded in memory right now?
```bash
curl -s http://localhost:11435/api/ps | python3 -m json.tool
```

### How healthy is the cluster?
```bash
curl -s http://localhost:11435/dashboard/api/health | python3 -m json.tool
```

### What models should I run?
```bash
curl -s http://localhost:11435/dashboard/api/recommendations | python3 -m json.tool
```

Returns recommendations based on your hardware — which models fit, which are too big, and the optimal mix.

### Recent activity
```bash
curl -s "http://localhost:11435/dashboard/api/traces?limit=10" | python3 -m json.tool
```

### Usage stats
```bash
curl -s http://localhost:11435/dashboard/api/usage | python3 -m json.tool
```

### Settings
```bash
curl -s http://localhost:11435/dashboard/api/settings | python3 -m json.tool

curl -s -X POST http://localhost:11435/dashboard/api/settings \
  -H "Content-Type: application/json" \
  -d '{"auto_pull": false}'
```

### Manage models
```bash
# What's on each machine
curl -s http://localhost:11435/dashboard/api/model-management | python3 -m json.tool

# Download a model to a specific machine
curl -s -X POST http://localhost:11435/dashboard/api/pull \
  -H "Content-Type: application/json" \
  -d '{"model": "llama3.3:70b", "node_id": "mac-studio"}'

# Remove a model
curl -s -X POST http://localhost:11435/dashboard/api/delete \
  -H "Content-Type: application/json" \
  -d '{"model": "old-model:7b", "node_id": "mac-studio"}'
```

### Per-app tracking
```bash
curl -s http://localhost:11435/dashboard/api/apps | python3 -m json.tool
```

Tag your requests to see which apps use the most GPU time:
```bash
curl -s http://localhost:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"llama3.3:70b","messages":[{"role":"user","content":"Hello"}],"metadata":{"tags":["my-app"]}}'
```

## Dashboard

Open `http://localhost:11435/dashboard` in your browser for a visual overview. Eight tabs: Fleet Overview (live node cards), Trends (charts), Model Insights (performance comparison), Apps (per-app usage), Benchmarks, Health (automated checks), Recommendations (what models to run), Settings (toggles and config).

## Try it out

```bash
# Quick test
curl -s http://localhost:11435/api/chat \
  -d '{"model":"llama3.2:3b","messages":[{"role":"user","content":"Hello!"}],"stream":false}'
```

## Troubleshooting

### Check what's slow
```bash
sqlite3 ~/.fleet-manager/latency.db "SELECT model, node_id, AVG(latency_ms)/1000.0 as avg_secs, COUNT(*) as n FROM request_traces WHERE status='completed' GROUP BY node_id, model HAVING n > 5 ORDER BY avg_secs DESC LIMIT 10"
```

### See failures
```bash
sqlite3 ~/.fleet-manager/latency.db "SELECT request_id, model, status, error_message, latency_ms/1000.0 as secs FROM request_traces WHERE status='failed' ORDER BY timestamp DESC LIMIT 10"
```

## Guardrails

- Never restart or stop the cluster without explicit user confirmation.
- Never delete or modify files in `~/.fleet-manager/` (contains all your cluster data and logs).
- Do not pull or delete models without user confirmation — downloads can be 10-100+ GB.
- If a machine shows as offline, report it rather than attempting to SSH into it.

## Failure handling

- Connection refused → router may not be running, suggest `herd` or `uv run herd`
- 0 nodes online → suggest starting `herd-node` or `uv run herd-node` on devices
- mDNS discovery fails → use `--router-url http://router-ip:11435`
- Requests hang → check for `num_ctx` in client requests; context protection should handle it automatically
- Errors → check `~/.fleet-manager/logs/herd.jsonl`
