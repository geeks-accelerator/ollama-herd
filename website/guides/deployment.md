# Deployment

Production setup, monitoring, log analysis, health checks, and operational tips.

## Architecture Decisions

Before deploying, decide:

**Which machine runs the router?** Pick a machine that's always on. The router is lightweight (minimal CPU/memory) — it doesn't run inference, just coordinates. Your most powerful machine is usually the best choice since it's likely always on and also runs Ollama.

**Which devices enable capacity learning?** Dedicated servers (Mac Studio, Linux boxes) should leave it disabled — they always run at full capacity. Laptops and shared devices should enable it so routing adapts to usage patterns.

**Which devices run which models?** The router handles this dynamically, but you control which models are pulled to each node. Large models go on high-RAM machines. Small fast models go on everything else. Embedding models go on one or two nodes.

## Starting the Fleet

**Router:**

```bash
pip install ollama-herd
herd
```

**Each node:**

```bash
pip install ollama-herd
herd-node
```

For nodes that double as workstations:

```bash
FLEET_NODE_ENABLE_CAPACITY_LEARNING=true herd-node
```

### Running as Background Services

**macOS (launchd):**

```bash
# Router
nohup herd &>/dev/null & disown

# Node
nohup herd-node &>/dev/null & disown
```

Or create a `~/Library/LaunchAgents/com.ollama-herd.router.plist` for automatic startup.

**Linux (systemd):**

```ini
# /etc/systemd/system/ollama-herd.service
[Unit]
Description=Ollama Herd Router
After=network.target

[Service]
ExecStart=/usr/local/bin/herd
Restart=always
User=your-user

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now ollama-herd
```

## Monitoring

### Dashboard

Open `http://router-ip:11435/dashboard` for real-time fleet monitoring with 8 tabs:

- **Fleet Overview** — Live node cards, queue depths, request counts
- **Trends** — Requests/hour, latency, token throughput (24h-7d)
- **Model Insights** — Per-model performance comparison
- **Apps** — Per-app analytics (requires request tagging)
- **Benchmarks** — Capacity growth over time
- **Health** — 15 automated health checks
- **Recommendations** — AI-powered model mix suggestions
- **Settings** — Runtime toggles and node versions

### Health API

```bash
curl -s http://localhost:11435/dashboard/api/health | python3 -m json.tool
```

Returns 15 automated checks:

1. Offline nodes
2. Degraded nodes
3. Memory pressure
4. Underutilized nodes
5. VRAM fallbacks
6. KV cache bloat
7. Model thrashing
8. Request timeouts
9. Error rates
10. Retry rates
11. Client disconnects
12. Incomplete streams
13. Version mismatch
14. Context protection events
15. Zombie reaper activity

Each check has a severity (info/warning/critical) and an actionable recommendation.

### Fleet Status

```bash
curl -s http://localhost:11435/fleet/status | python3 -m json.tool
```

Returns per-node details: status, hardware, memory, CPU, loaded models, queue depths.

### Queue Depths (Lightweight)

```bash
curl -s http://localhost:11435/fleet/queue | python3 -m json.tool
```

Returns just queue depths — designed for client-side backoff logic.

## Log Analysis

### Structured Logs (JSONL)

All events are written to `~/.fleet-manager/logs/herd.jsonl` — one JSON object per line, daily rotation, 30-day retention.

```bash
# Tail the live log
tail -f ~/.fleet-manager/logs/herd.jsonl | python3 -m json.tool

# Find errors
grep '"level":"ERROR"' ~/.fleet-manager/logs/herd.jsonl

# Find events for a specific model
grep '"model":"llama3.3:70b"' ~/.fleet-manager/logs/herd.jsonl

# Count errors by component
grep '"level":"ERROR"' ~/.fleet-manager/logs/herd.jsonl | \
  python3 -c "import sys,json; from collections import Counter; \
  c=Counter(json.loads(l)['logger'] for l in sys.stdin); \
  print('\n'.join(f'{v:4d} {k}' for k,v in c.most_common()))"
```

### Log Levels

| Variable | Default | Controls |
|----------|---------|----------|
| `FLEET_LOG_LEVEL` | `DEBUG` | What's written to JSONL |
| `FLEET_CONSOLE_LOG_LEVEL` | `INFO` | What's printed to terminal |

Set `FLEET_LOG_LEVEL=INFO` in production to reduce file size.

### Request Traces (SQLite)

Every routing decision is recorded in `~/.fleet-manager/latency.db`:

```bash
# Recent requests
sqlite3 ~/.fleet-manager/latency.db \
  "SELECT model, node_id, latency_ms, status FROM request_traces ORDER BY timestamp DESC LIMIT 10"

# Failures
sqlite3 ~/.fleet-manager/latency.db \
  "SELECT model, node_id, error_message FROM request_traces WHERE status='failed' ORDER BY timestamp DESC LIMIT 10"

# Average latency per model
sqlite3 ~/.fleet-manager/latency.db \
  "SELECT model, ROUND(AVG(latency_ms)/1000.0, 1) as avg_secs, COUNT(*) as requests \
   FROM request_traces WHERE status='completed' GROUP BY model ORDER BY requests DESC"

# Error rate over the last hour
sqlite3 ~/.fleet-manager/latency.db \
  "SELECT status, COUNT(*) FROM request_traces \
   WHERE timestamp > strftime('%s','now') - 3600 GROUP BY status"
```

## Resilience Features

### Auto-Retry

If a node fails before the first chunk, the router re-scores and retries on the next-best node. Up to 2 retries (configurable via `FLEET_MAX_RETRIES`).

### Model Fallbacks

Clients specify backup models: `"fallback_models": ["qwen2.5:32b", "qwen2.5:7b"]`. The router tries each in order through the full scoring pipeline.

### Auto-Pull

Missing models are automatically pulled to the best available node. Configurable via `FLEET_AUTO_PULL` (default: true).

### Context Protection

Strips unnecessary `num_ctx` from requests to prevent model reload hangs. Auto-upgrades to a larger loaded model when possible. Configurable via `FLEET_CONTEXT_PROTECTION` (default: strip).

### Graceful Drain

Send SIGTERM to a node agent:

1. Capacity learner state saves to disk
2. Drain heartbeat sent to router
3. Router stops routing new requests to this node
4. In-flight requests complete normally
5. Pending requests rebalance to other nodes
6. Agent shuts down cleanly

### Zombie Reaper

Background task detects in-flight requests that never completed (connection drops, Ollama crashes) and cleans them up so queues stay accurate.

## Configuration

All settings via environment variables. No config files.

**Key server variables:**

| Variable | Default | What |
|----------|---------|------|
| `FLEET_PORT` | `11435` | Router listen port |
| `FLEET_HEARTBEAT_TIMEOUT` | `15.0` | Seconds before node is degraded |
| `FLEET_HEARTBEAT_OFFLINE` | `30.0` | Seconds before node is offline |
| `FLEET_MAX_RETRIES` | `2` | Max retry attempts per request |
| `FLEET_AUTO_PULL` | `true` | Auto-pull missing models |
| `FLEET_CONTEXT_PROTECTION` | `strip` | Context size protection mode |
| `FLEET_LOG_LEVEL` | `DEBUG` | JSONL log level |

**Key node variables:**

| Variable | Default | What |
|----------|---------|------|
| `FLEET_NODE_ENABLE_CAPACITY_LEARNING` | `false` | Enable adaptive capacity |
| `FLEET_NODE_DATA_DIR` | `~/.fleet-manager` | State file directory |

See the [full configuration reference](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/configuration-reference.md) for all 44+ variables with tuning guidance.

## Ollama Settings

For best results with a fleet, set these Ollama environment variables on each node:

```bash
# In ~/.zshrc (macOS) or ~/.bashrc (Linux)
export OLLAMA_NUM_PARALLEL=2        # Allow 2 concurrent requests
export OLLAMA_KEEP_ALIVE=-1         # Never unload models
export OLLAMA_MAX_LOADED_MODELS=-1  # No limit on loaded models
```

`KEEP_ALIVE=-1` prevents model thrashing. `MAX_LOADED_MODELS=-1` lets Ollama manage memory naturally.

## Data Storage

All persistent data lives in `~/.fleet-manager/` (configurable via `FLEET_DATA_DIR`):

```
~/.fleet-manager/
  latency.db                           # SQLite: traces, latency, usage, benchmarks
  logs/
    herd.jsonl                         # Structured logs (daily rotation)
  capacity-learner-{node-id}.json      # Learned behavioral data (per node)
```

SQLite uses WAL mode for concurrent read/write. All files are human-readable and can be backed up, queried, or deleted at will.

## Next Steps

- **[Routing Engine](routing-engine.md)** — Understanding and tuning scoring decisions
- **[Adaptive Capacity](adaptive-capacity.md)** — Configuring capacity learning per device
- **[API Reference](api-reference.md)** — All endpoints and response formats
