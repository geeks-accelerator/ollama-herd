---
name: ollama-manager
description: Manage Ollama models — Ollama Llama, Qwen, DeepSeek, Phi, Mistral — across your machines. See what's loaded in Ollama, what's eating disk, what's never used. Pull, delete, and organize Ollama models from one place. AI-powered Ollama recommendations for the optimal model mix based on your hardware. Ollama本地模型管理 | Ollama gestor de modelos IA.
version: 1.0.1
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"package","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","sqlite3","pip"],"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["darwin","linux","windows"]}}
---

# Ollama Manager

You're helping someone wrangle their Ollama models. They've got Ollama models scattered across machines — some Ollama models loaded, some sitting cold on disk, some they forgot they pulled via Ollama six months ago. This skill gives you the tools to see every Ollama model, clean up the mess, and figure out what Ollama models they actually need.

## The Ollama problem

Ollama makes it too easy to pull models. `ollama pull` this, `ollama pull` that — suddenly you've got 200GB of Ollama models across three machines and no idea which Ollama models you actually use. No way to see Ollama disk usage across machines. No way to compare which Ollama model is faster on which hardware. No "hey, you haven't touched this 40GB Ollama model in two weeks, maybe delete it?"

That's what Ollama Manager is for.

## Get started with Ollama Manager

```bash
pip install ollama-herd           # install the Ollama management toolkit
herd                              # start the Ollama router (tracks all your Ollama machines)
herd-node                         # run on each Ollama machine you want to manage
```

Package: [`ollama-herd`](https://pypi.org/project/ollama-herd/) | Repo: [github.com/geeks-accelerator/ollama-herd](https://github.com/geeks-accelerator/ollama-herd)

## Connect to your Ollama fleet

The Ollama manager talks to an Ollama Herd router at `http://localhost:11435`. This router already knows about all your Ollama machines — it tracks heartbeats, loaded Ollama models, disk usage, and Ollama performance history.

## See what Ollama models you've got

### Every Ollama model available across all machines
```bash
# ollama_all_models — list every Ollama model on every node
curl -s http://localhost:11435/api/tags | python3 -m json.tool
```

Shows every Ollama model on every machine with sizes and which nodes have them.

### What Ollama models are actually loaded in GPU memory right now
```bash
# ollama_hot_models — Ollama models ready to serve instantly
curl -s http://localhost:11435/api/ps | python3 -m json.tool
```

These are the "hot" Ollama models — ready to serve instantly. Everything else is cold on disk and needs Ollama loading time.

### Per-machine Ollama breakdown with disk usage
```bash
# ollama_disk_usage — per-node Ollama model sizes
curl -s http://localhost:11435/dashboard/api/model-management | python3 -m json.tool
```

The real picture: Ollama model sizes, last-used timestamps, which machines have which Ollama models, and how much disk each is eating.

## Figure out what Ollama models to keep

### Which Ollama models actually get used?
```bash
sqlite3 ~/.fleet-manager/latency.db "SELECT model, COUNT(*) as requests, SUM(COALESCE(completion_tokens,0)) as tokens_generated, ROUND(AVG(latency_ms)/1000.0, 1) as avg_secs FROM request_traces WHERE status='completed' GROUP BY model ORDER BY requests DESC"
```

### Which Ollama models haven't been touched?
```bash
sqlite3 ~/.fleet-manager/latency.db "SELECT model, MAX(datetime(timestamp, 'unixepoch', 'localtime')) as last_used, COUNT(*) as total_requests FROM request_traces GROUP BY model ORDER BY last_used ASC"
```

If an Ollama model's last request was weeks ago, it's a candidate for deletion.

### How much disk is each Ollama model using?
```bash
curl -s http://localhost:11435/dashboard/api/model-management | python3 -c "
import sys, json
data = json.load(sys.stdin)
for node in data:
    print(f\"\\n{node['node_id']}:\")
    ollama_total = 0
    for m in node.get('models', []):
        size = m.get('size_gb', 0)
        ollama_total += size
        print(f\"  {m['name']:40s} {size:6.1f} GB\")
    print(f\"  {'OLLAMA TOTAL':40s} {ollama_total:6.1f} GB\")
"
```

### What Ollama models are fast and what's slow?
```bash
sqlite3 ~/.fleet-manager/latency.db "SELECT model, node_id, ROUND(AVG(latency_ms)/1000.0, 1) as avg_secs, COUNT(*) as n FROM request_traces WHERE status='completed' GROUP BY model, node_id HAVING n > 5 ORDER BY avg_secs"
```

## Get Ollama recommendations

### What Ollama models should I be running?
```bash
# ollama_recommendations — optimal Ollama model mix per node
curl -s http://localhost:11435/dashboard/api/recommendations | python3 -m json.tool
```

AI-powered Ollama recommendations based on your actual hardware — RAM, cores, GPU memory. Tells you which Ollama models fit, which are too big, and the optimal Ollama model mix for your machines. Includes estimated RAM requirements and Ollama benchmark data.

## Pull and delete Ollama models

### Pull an Ollama model to a specific machine
```bash
# ollama_pull — download an Ollama model to a node
curl -s -X POST http://localhost:11435/dashboard/api/pull \
  -H "Content-Type: application/json" \
  -d '{"model": "llama3.3:70b", "node_id": "mac-studio"}'
```

The Ollama router picks the machine with the most free disk and memory if you're not sure which node to target.

### Delete an Ollama model from a machine
```bash
# ollama_delete — remove an Ollama model from a node
curl -s -X POST http://localhost:11435/dashboard/api/delete \
  -H "Content-Type: application/json" \
  -d '{"model": "old-model:7b", "node_id": "mac-studio"}'
```

### Ollama Auto-pull (when enabled)
If a client requests an Ollama model that doesn't exist anywhere, the Ollama router can automatically pull it to the best machine. Toggle this:
```bash
# Check current Ollama setting
curl -s http://localhost:11435/dashboard/api/settings | python3 -c "import sys,json; print(json.load(sys.stdin)['config']['toggles'])"

# Toggle Ollama auto-pull off
curl -s -X POST http://localhost:11435/dashboard/api/settings \
  -H "Content-Type: application/json" \
  -d '{"auto_pull": false}'
```

## Check Ollama fleet health
```bash
curl -s http://localhost:11435/dashboard/api/health | python3 -m json.tool
```

Automated Ollama checks for: Ollama model thrashing (models loading/unloading frequently — sign of memory pressure), disk pressure, and underutilized Ollama nodes that could take more models.

## Ollama Dashboard

Open `http://localhost:11435/dashboard` and go to the **Recommendations** tab for a visual Ollama model management interface. One-click pull for recommended Ollama models. The **Fleet Overview** tab shows which Ollama models are loaded where in real time.

## Ollama Guardrails

- **Never delete Ollama models without explicit user confirmation.** Always show what Ollama model will be deleted and how much disk it frees.
- **Never pull Ollama models without user confirmation.** Ollama downloads can be 10-100+ GB.
- Never modify files in `~/.fleet-manager/` (contains Ollama data).
- If the Ollama router isn't running, suggest `herd` or `uv run herd` to start it.
