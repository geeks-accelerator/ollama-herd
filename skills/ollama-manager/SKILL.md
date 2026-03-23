---
name: ollama-manager
description: Manage Ollama models across your machines — see what's loaded, what's eating disk, what's never used, and what you should pull next. Get AI-powered recommendations for the optimal model mix based on your hardware. Pull, delete, and organize models from one place instead of SSH-ing into each box.
version: 1.0.0
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"package","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","sqlite3","pip"],"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"]},"os":["darwin","linux"]}}
---

# Ollama Manager

You're helping someone wrangle their Ollama models. They've got models scattered across machines — some loaded, some sitting cold on disk, some they forgot they pulled six months ago. This skill gives you the tools to see everything, clean up the mess, and figure out what they actually need.

## The problem

Ollama makes it too easy to pull models. `ollama pull` this, `ollama pull` that — suddenly you've got 200GB of models across three machines and no idea which ones you actually use. No way to see disk usage across machines. No way to compare which model is faster on which hardware. No "hey, you haven't touched this 40GB model in two weeks, maybe delete it?"

That's what this is for.

## Get started

```bash
pip install ollama-herd
herd              # start the router (tracks all your machines)
herd-node         # run on each machine you want to manage
```

Package: [`ollama-herd`](https://pypi.org/project/ollama-herd/) | Repo: [github.com/geeks-accelerator/ollama-herd](https://github.com/geeks-accelerator/ollama-herd)

## Connect to your fleet

The manager talks to an Ollama Herd router at `http://localhost:11435`. This router already knows about all your machines — it tracks heartbeats, loaded models, disk usage, and performance history.

## See what you've got

### Everything available across all machines
```bash
curl -s http://localhost:11435/api/tags | python3 -m json.tool
```

Shows every model on every machine with sizes and which nodes have them.

### What's actually loaded in GPU memory right now
```bash
curl -s http://localhost:11435/api/ps | python3 -m json.tool
```

These are the "hot" models — ready to serve instantly. Everything else is cold on disk and needs loading time.

### Per-machine breakdown with disk usage
```bash
curl -s http://localhost:11435/dashboard/api/model-management | python3 -m json.tool
```

The real picture: model sizes, last-used timestamps, which machines have which models, and how much disk each is eating.

## Figure out what to keep

### Which models actually get used?
```bash
sqlite3 ~/.fleet-manager/latency.db "SELECT model, COUNT(*) as requests, SUM(COALESCE(completion_tokens,0)) as tokens_generated, ROUND(AVG(latency_ms)/1000.0, 1) as avg_secs FROM request_traces WHERE status='completed' GROUP BY model ORDER BY requests DESC"
```

### Which models haven't been touched?
```bash
sqlite3 ~/.fleet-manager/latency.db "SELECT model, MAX(datetime(timestamp, 'unixepoch', 'localtime')) as last_used, COUNT(*) as total_requests FROM request_traces GROUP BY model ORDER BY last_used ASC"
```

If a model's last request was weeks ago, it's a candidate for deletion.

### How much disk is each model using?
```bash
curl -s http://localhost:11435/dashboard/api/model-management | python3 -c "
import sys, json
data = json.load(sys.stdin)
for node in data:
    print(f\"\\n{node['node_id']}:\")
    total = 0
    for m in node.get('models', []):
        size = m.get('size_gb', 0)
        total += size
        print(f\"  {m['name']:40s} {size:6.1f} GB\")
    print(f\"  {'TOTAL':40s} {total:6.1f} GB\")
"
```

### What's fast and what's slow?
```bash
sqlite3 ~/.fleet-manager/latency.db "SELECT model, node_id, ROUND(AVG(latency_ms)/1000.0, 1) as avg_secs, COUNT(*) as n FROM request_traces WHERE status='completed' GROUP BY model, node_id HAVING n > 5 ORDER BY avg_secs"
```

## Get recommendations

### What should I be running?
```bash
curl -s http://localhost:11435/dashboard/api/recommendations | python3 -m json.tool
```

AI-powered recommendations based on your actual hardware — RAM, cores, GPU memory. Tells you which models fit, which are too big, and the optimal mix for your machines. Includes estimated RAM requirements and benchmark data.

## Pull and delete models

### Pull a model to a specific machine
```bash
curl -s -X POST http://localhost:11435/dashboard/api/pull \
  -H "Content-Type: application/json" \
  -d '{"model": "llama3.3:70b", "node_id": "mac-studio"}'
```

The router picks the machine with the most free disk and memory if you're not sure which node to target.

### Delete a model from a machine
```bash
curl -s -X POST http://localhost:11435/dashboard/api/delete \
  -H "Content-Type: application/json" \
  -d '{"model": "old-model:7b", "node_id": "mac-studio"}'
```

### Auto-pull (when enabled)
If a client requests a model that doesn't exist anywhere, the router can automatically pull it to the best machine. Toggle this:
```bash
# Check current setting
curl -s http://localhost:11435/dashboard/api/settings | python3 -c "import sys,json; print(json.load(sys.stdin)['config']['toggles'])"

# Toggle off
curl -s -X POST http://localhost:11435/dashboard/api/settings \
  -H "Content-Type: application/json" \
  -d '{"auto_pull": false}'
```

## Check fleet health
```bash
curl -s http://localhost:11435/dashboard/api/health | python3 -m json.tool
```

Automated checks for: model thrashing (models loading/unloading frequently — sign of memory pressure), disk pressure, and underutilized nodes that could take more models.

## Dashboard

Open `http://localhost:11435/dashboard` → **Recommendations** tab for a visual model management interface. One-click pull for recommended models. The **Fleet Overview** tab shows which models are loaded where in real time.

## Guardrails

- **Never delete models without explicit user confirmation.** Always show what will be deleted and how much disk it frees.
- **Never pull models without user confirmation.** Downloads can be 10-100+ GB.
- Never modify files in `~/.fleet-manager/`.
- If the router isn't running, suggest `herd` or `uv run herd` to start it.
