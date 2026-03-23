# Configuration Reference

All settings are configured via environment variables. No config files needed.

---

## Server Settings (`FLEET_` prefix)

### Network

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_HOST` | `0.0.0.0` | Bind address for the router |
| `FLEET_PORT` | `11435` | Listen port (Ollama default + 1) |

### Heartbeat Monitoring

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_HEARTBEAT_INTERVAL` | `5.0` | Seconds between heartbeat checks |
| `FLEET_HEARTBEAT_TIMEOUT` | `15.0` | Seconds before a node is marked degraded |
| `FLEET_HEARTBEAT_OFFLINE` | `30.0` | Seconds before a node is marked offline |

### Scoring Weights

These control the 7-signal scoring engine. Each incoming request is scored across all candidate nodes. Higher total score wins.

| Variable | Default | Signal | Description |
|----------|---------|--------|-------------|
| `FLEET_SCORE_MODEL_HOT` | `50.0` | Thermal | Points for model currently loaded in memory |
| `FLEET_SCORE_MODEL_WARM` | `30.0` | Thermal | Points for model loaded within last 30 min (likely OS-cached) |
| `FLEET_SCORE_MODEL_COLD` | `10.0` | Thermal | Points for model on disk but not recently used |
| `FLEET_SCORE_MEMORY_FIT_MAX` | `20.0` | Memory | Max points for comfortable memory headroom |
| `FLEET_SCORE_QUEUE_DEPTH_MAX_PENALTY` | `30.0` | Queue | Max penalty for saturated queues |
| `FLEET_SCORE_QUEUE_DEPTH_PENALTY_PER` | `6.0` | Queue | Penalty per queued request (pending + in-flight) |
| `FLEET_SCORE_WAIT_TIME_MAX_PENALTY` | `25.0` | Wait | Max penalty based on estimated wait time |
| `FLEET_SCORE_ROLE_AFFINITY_MAX` | `15.0` | Affinity | Max points for device-model role match |
| `FLEET_SCORE_ROLE_LARGE_THRESHOLD_GB` | `20.0` | Affinity | Models above this size prefer large machines |
| `FLEET_SCORE_ROLE_SMALL_THRESHOLD_GB` | `8.0` | Affinity | Models below this size prefer small machines |
| `FLEET_SCORE_AVAILABILITY_TREND_MAX` | `10.0` | Capacity | Max points for node availability (capacity learning) |
| `FLEET_SCORE_CONTEXT_FIT_MAX` | `15.0` | Context | Max points (or penalty) for context window fit vs estimated tokens |

**Tuning guidance:**
- Increase `SCORE_MODEL_HOT` to more aggressively prefer hot models over empty queues
- Decrease `SCORE_QUEUE_DEPTH_PENALTY_PER` to tolerate deeper queues before spreading load
- Increase `SCORE_ROLE_AFFINITY_MAX` to more strongly enforce "big models on big machines"
- Increase `SCORE_CONTEXT_FIT_MAX` to more strongly prefer nodes with larger context windows for long inputs

### Rebalancer

The rebalancer runs continuously, moving pending requests from overloaded queues to better alternatives.

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_REBALANCE_INTERVAL` | `5.0` | Seconds between rebalancer scans |
| `FLEET_REBALANCE_THRESHOLD` | `4` | Queue depth that triggers rebalancing |
| `FLEET_REBALANCE_MAX_PER_CYCLE` | `3` | Max requests moved per rebalancer cycle |

### Auto-Pull

When a requested model doesn't exist on any fleet node, the router can automatically pull it onto the best available node and serve the request seamlessly.

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_AUTO_PULL` | `true` | Auto-pull missing models onto the best available node |
| `FLEET_AUTO_PULL_TIMEOUT` | `300.0` | Max seconds to wait for a model pull to complete (5 min) |

The router selects the online node with the most available memory that can fit the estimated model size. Concurrent pulls of the same model are deduplicated. Disable with `FLEET_AUTO_PULL=false` to get a 404 for missing models instead.

### Context Protection

Prevents clients from triggering expensive Ollama model reloads by sending `num_ctx` in request options. When Ollama receives a `num_ctx` different from the loaded model's context window, it unloads and reloads the entire model — which can cause multi-minute hangs or deadlocks on large models.

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_CONTEXT_PROTECTION` | `strip` | How to handle client `num_ctx` values: `strip`, `warn`, or `passthrough` |

Modes:
- **`strip`** (default): Remove `num_ctx` from requests when it's ≤ the loaded model's context window. Logs an info message. If `num_ctx` exceeds the loaded context, it's preserved with a warning (client genuinely needs more context).
- **`warn`**: Keep `num_ctx` but log warnings about potential reload triggers.
- **`passthrough`**: No intervention — pass `num_ctx` through to Ollama as-is.

Only applies to Ollama-format requests (`/api/chat`, `/api/generate`). OpenAI-format requests don't have a `num_ctx` equivalent.

### Pre-Warm

Pre-warm proactively loads models on runner-up nodes before they're needed.

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_PRE_WARM_THRESHOLD` | `3` | Winner's queue depth that triggers pre-warm on runner-up |
| `FLEET_PRE_WARM_MIN_AVAILABILITY` | `0.60` | Minimum node availability score to receive pre-warm (capacity learning) |

### Retry & Reaper

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_MAX_RETRIES` | `2` | Max retry attempts on node failure (before first chunk) |
| `FLEET_STALE_TIMEOUT` | `900.0` | Seconds before in-flight requests are considered zombied and reaped (15 min) |

### mDNS Discovery

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_MDNS_SERVICE_TYPE` | `_fleet-manager._tcp.local.` | Zeroconf service type |
| `FLEET_MDNS_SERVICE_NAME` | `Fleet Manager Router` | Advertised service name |

### Storage

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_DATA_DIR` | `~/.fleet-manager` | Directory for SQLite databases and logs |

---

## Node Settings (`FLEET_NODE_` prefix)

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_NODE_NODE_ID` | *(hostname)* | Unique node identifier (auto-detected from hostname if empty) |
| `FLEET_NODE_OLLAMA_HOST` | `http://localhost:11434` | URL of the local Ollama instance |
| `FLEET_NODE_ROUTER_URL` | *(auto-discover)* | Router URL; set to skip mDNS discovery |
| `FLEET_NODE_HEARTBEAT_INTERVAL` | `5.0` | Seconds between heartbeats to the router |
| `FLEET_NODE_POLL_INTERVAL` | `5.0` | Seconds between local metric collection cycles |
| `FLEET_NODE_ENABLE_CAPACITY_LEARNING` | `false` | Enable adaptive capacity learning (see [Adaptive Capacity](adaptive-capacity.md)) |
| `FLEET_NODE_DATA_DIR` | `~/.fleet-manager` | Directory for capacity learning state and logs |
| `FLEET_NODE_MDNS_SERVICE_TYPE` | `_fleet-manager._tcp.local.` | Zeroconf service type to search for |

---

## Logging Settings

These control the JSONL structured logging system (see [Operations Guide](operations-guide.md#structured-logging)).

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_LOG_LEVEL` | `DEBUG` | Log level written to JSONL file |
| `FLEET_CONSOLE_LOG_LEVEL` | `INFO` | Log level printed to console (Rich handler) |

---

## Example: Full Custom Configuration

```bash
# Server with tuned scoring
export FLEET_PORT=11435
export FLEET_SCORE_MODEL_HOT=60
export FLEET_SCORE_QUEUE_DEPTH_PENALTY_PER=8
export FLEET_REBALANCE_THRESHOLD=3
export FLEET_MAX_RETRIES=3
uv run herd

# Node with capacity learning enabled
export FLEET_NODE_ROUTER_URL=http://macstudio.local:11435
export FLEET_NODE_ENABLE_CAPACITY_LEARNING=true
uv run herd-node
```
