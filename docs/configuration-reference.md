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
| `FLEET_STALE_TIMEOUT` | `600.0` | Seconds before in-flight requests are considered zombied and reaped (10 min) |

### Dynamic Context Management

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_DYNAMIC_NUM_CTX` | `false` | Enable dynamic num_ctx injection on requests. When enabled, the router injects per-model num_ctx overrides to reduce KV cache waste |
| `FLEET_NUM_CTX_AUTO_CALCULATE` | `false` | Auto-calculate optimal num_ctx from trace data. The context optimizer analyzes p99 total token usage (prompt + completion) and updates overrides every 5 minutes |

Per-model overrides are set at runtime via `POST /dashboard/api/settings` with `{"num_ctx_overrides": {"model-name": 16384}}`. When `dynamic_num_ctx` is enabled and `num_ctx_auto_calculate` is true, the optimizer auto-initializes overrides from 7-day trace history on startup.

**Tuning guidance:**
- Enable `FLEET_DYNAMIC_NUM_CTX` when a model's allocated context far exceeds actual usage (check `/dashboard/api/context-usage`)
- The recommended context is p99 of total tokens (prompt + completion) with 50% headroom, rounded to next power of 2
- Overrides only affect cold loads — already-loaded models keep their current context until Ollama restarts

### Image Generation

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_IMAGE_GENERATION` | `false` | Enable `/api/generate-image` endpoint for mflux routing |
| `FLEET_IMAGE_TIMEOUT` | `120.0` | Max seconds to wait for image generation |

### Transcription (Speech-to-Text)

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_TRANSCRIPTION` | `false` | Enable `/api/transcribe` endpoint for Qwen3-ASR routing |
| `FLEET_TRANSCRIPTION_TIMEOUT` | `300.0` | Max seconds to wait for transcription |

### Thinking Models

Thinking models (deepseek-r1, gpt-oss, qwq) split `num_predict` between internal reasoning and visible output. Small budgets result in empty responses. The router auto-detects thinking models and inflates the budget.

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_THINKING_OVERHEAD` | `4.0` | Multiply client's `num_predict` by this factor for thinking models |
| `FLEET_THINKING_MIN_PREDICT` | `1024` | Minimum `num_predict` sent to Ollama for thinking models (floor) |

Only applies when the client explicitly sets `num_predict` / `max_tokens`. If omitted, Ollama uses the model's default. See [Thinking Models Guide](guides/thinking-models.md).

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
