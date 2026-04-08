# Core Concepts

The mental model behind Ollama Herd. Read this if terms like node, heartbeat, scoring signal, queue, or capacity mode are still fuzzy.

## Nodes

A **node** is any device running Ollama with the `herd-node` agent. Each node is sovereign — it runs its own Ollama, manages its own models, and works fine standalone. The router coordinates but never controls.

Nodes have three states:

| State | Meaning |
|-------|---------|
| **Online** | Heartbeating normally, available for routing |
| **Degraded** | Heartbeat delayed (>15s), still routable but penalized |
| **Offline** | No heartbeat for 30+ seconds, excluded from routing |

A node can also be **paused** (meeting detected, manual override, or very low availability score) — paused nodes are online but temporarily excluded from routing.

## Heartbeats

Every 5 seconds, each node agent sends a **heartbeat** to the router containing:

- System metrics: CPU usage, memory usage, thermal state
- Ollama state: loaded models with context lengths, models available on disk
- Disk space and Ollama version
- Capacity info: availability score, memory ceiling, capacity mode (if capacity learning is enabled)

The router uses heartbeats to maintain a real-time picture of every device in the fleet. If a heartbeat is missed, the node's state degrades. Three misses and it's offline.

## Scoring Signals

When a request arrives, the router scores every eligible node across **7 weighted signals**:

| # | Signal | What It Answers | Weight |
|---|--------|----------------|--------|
| 1 | **Model thermal state** | Is the model already loaded in GPU memory? | Up to +50 |
| 2 | **Memory fit** | How comfortably does the model fit? | Up to +20 |
| 3 | **Queue depth** | How many requests are waiting? | Up to -30 |
| 4 | **Estimated wait time** | How long until this request starts? | Up to -25 |
| 5 | **Role affinity** | Does this machine match the model's weight class? | Up to +15 |
| 6 | **Availability trend** | Is this device getting busier or freeing up? | Up to +10 |
| 7 | **Context fit** | Can this node handle the requested context size? | Up to +10 |

The highest total score wins. A hot model on an idle, powerful machine with plenty of memory scores 80+. A cold model on a busy laptop with a rising workload scores under 20.

## Queues

Each **node + model** pair has its own queue. When a request is routed to a node, it enters that node's queue for that model.

Queues have **dynamic concurrency** — the router calculates how many parallel requests each node can handle based on available memory and model size. A Mac Studio with 512GB can run 8 parallel requests. A MacBook with 16GB might handle 2.

Key queue behaviors:

- **Holding queue** — When no node can serve a request, it waits (up to 30 seconds) instead of failing. The router retries scoring as node states change.
- **Rebalancer** — Every 5 seconds, moves pending requests from overloaded queues to nodes with spare capacity (only where the model is already loaded).
- **Zombie reaper** — Detects and cleans up stuck in-flight requests that never completed.

## Capacity Modes

Each node operates in a **capacity mode** that determines how much memory the router is allowed to use:

| Mode | Memory Ceiling | When |
|------|---------------|------|
| **Full** | 80% of total RAM | Dedicated server or high availability |
| **Learned high** | 50% (max 64GB) | Normal learned availability |
| **Learned medium** | 25% (max 32GB) | Moderate workload detected |
| **Learned low** | 12.5% (max 16GB) | Heavy workload detected |
| **Paused** | 0GB | Meeting, critical memory pressure, or manual pause |
| **Bootstrap** | 0GB | First 7 days of capacity learning (observation only) |

Dedicated servers (like a Mac Studio) skip capacity learning and always run in **full** mode. Laptops and shared devices use the **capacity learner** to dynamically adjust.

## Models

Ollama Herd routes four types of models:

| Type | What | Where |
|------|------|-------|
| **LLM** | Chat completions, text generation | Any Ollama node |
| **Embedding** | Vector embeddings for RAG | Any Ollama node with embedding model |
| **Image** | Image generation (FLUX, Stable Diffusion) | Apple Silicon nodes with mflux/DiffusionKit |
| **Speech-to-text** | Audio transcription | Apple Silicon nodes with MLX + Qwen3-ASR |

A model can be in three thermal states on a given node:

| State | Meaning | Scoring Impact |
|-------|---------|---------------|
| **Hot** | Currently loaded in GPU memory | +50 points |
| **Warm** | On disk, loaded within last 30 minutes (likely OS-cached) | +30 points |
| **Cold** | On disk, not recently used | +10 points |

The router strongly prefers hot models — cold-loading a 40GB model takes 15-30 seconds.

## Auto-Pull

When a requested model doesn't exist on any node and auto-pull is enabled (default), the router automatically pulls it onto the node with the most available memory. The client waits for the download, then gets served normally.

## Fallbacks

Clients can specify **fallback models** — backup models to try if the primary isn't available. The router tries each in order through the same scoring pipeline:

```json
{
  "model": "llama3.3:70b",
  "fallback_models": ["qwen2.5:32b", "qwen2.5:7b"]
}
```

## Auto-Retry

If a node fails before the first response chunk is sent, the router **re-scores** remaining nodes (excluding the failed one) and retries. Up to 2 retries. Clients never see the failure.

## Request Tags

Any request can carry **tags** for per-app analytics:

```json
{"metadata": {"tags": ["my-app", "production"]}}
```

Or via header: `X-Herd-Tags: my-app, production`

The dashboard breaks down usage, latency, and error rates per tag — so you can see which tools consume the most fleet resources.

## Traces

Every routing decision is recorded as a **trace** in SQLite. Each trace includes: model, node, score, latency, tokens, retry/fallback status, and tags. You can query traces through the dashboard API or directly with SQLite:

```bash
sqlite3 ~/.fleet-manager/latency.db "SELECT model, node_id, latency_ms FROM request_traces ORDER BY timestamp DESC LIMIT 5"
```

## The Dashboard

A real-time web UI at `/dashboard` with 8 tabs: Fleet Overview, Trends, Model Insights, Apps, Benchmarks, Health, Recommendations, and Settings. Updated via Server-Sent Events — no polling, no page refresh.

## Next Steps

- **[Routing Engine](routing-engine.md)** — Deep dive into the 5-stage scoring pipeline
- **[Adaptive Capacity](adaptive-capacity.md)** — How the fleet learns your usage patterns
- **[Integrations](integrations.md)** — Connect your tools to the fleet
