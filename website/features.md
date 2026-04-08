# Features

Everything Ollama Herd does, and why it matters.

## Intelligent Routing

### 7-Signal Scoring Engine

Every request is scored across seven signals to find the optimal node. This isn't round-robin or random selection — it's a weighted decision that considers the physical reality of each device.

- **Model thermal state** — A model already loaded in GPU memory (hot) gets +50 points. Cold-loading a 40GB model takes 15-30 seconds. The router avoids it whenever possible.
- **Memory fit** — Not just "is there enough RAM?" but how comfortably the model fits given current utilization and the node's dynamic memory ceiling.
- **Queue depth** — A hot model on a saturated node loses to a warm model on an empty node. Load spreads naturally.
- **Estimated wait time** — Uses real per-node, per-model latency history (p75) to estimate actual wait time. A queue of 3 on a fast model is very different from a queue of 3 on a slow model.
- **Role affinity** — Large models route to powerful machines. Small models route to lighter hardware, preserving big-machine capacity for what it's uniquely suited for.
- **Availability trend** — Is this device freeing up or getting busier right now? Prevents sending a long inference request to a machine whose owner just sat down.
- **Context fit** — Can this node handle the requested context size without triggering a model reload?

### Model Fallbacks

Clients can specify backup models. If the primary model isn't available anywhere in the fleet, the router tries alternatives in order — same scoring pipeline, just different model.

### Auto-Retry

If a node fails before the first response chunk is sent, the router re-scores the remaining nodes and retries on the next-best option. Up to 2 retries. Clients never see the failure.

### Context Protection

Strips unnecessary `num_ctx` from requests to prevent Ollama model reload hangs. Auto-upgrades to a larger loaded model in the same category when the requested model is cold but a compatible one is hot.

## Zero-Config Discovery

### mDNS Auto-Discovery

Run `herd-node` on any device on the same network. It finds the router automatically via mDNS (Bonjour/Avahi). No IP addresses to configure, no config files to maintain, no DNS entries to manage.

### Heartbeat-Based Health

Each node sends heartbeats every 5 seconds with full system state: CPU, memory, GPU utilization, thermal state, loaded models, disk space, Ollama version. The router knows the exact state of every device in real time.

### LAN Proxy

The node agent automatically bridges LAN traffic to localhost Ollama. Other devices can reach each node's Ollama through the fleet without manual port forwarding.

## Adaptive Learning

### Capacity Learner

A 168-slot behavioral model (one slot per hour of the week) learns each device's availability patterns. After a few weeks, the router knows your MacBook is busy Tuesday mornings and your Mac Studio is always available. Routing decisions reflect these patterns.

### Meeting Detection (macOS)

Detects active cameras and microphones and hard-pauses the node. No inference competes with your video calls. The node resumes automatically when the meeting ends.

### App Fingerprinting

Classifies the current workload on each device (idle / light / moderate / heavy / intensive) using CPU, memory, and network patterns — without reading app names or window titles. Heavy workloads reduce the node's memory ceiling, shifting requests to other machines.

### Latency Tables

Per-node, per-model response times tracked in SQLite. The scoring engine uses historical latency to estimate wait times accurately. A node that's consistently slow for a particular model gradually gets fewer requests for that model.

## Queue Management

### Per Node:Model Queues

Each node+model pair has its own queue with dynamic concurrency. The router knows how many parallel requests each device can handle without degrading performance.

### Holding Queue

When all nodes are at capacity, requests wait in a holding queue instead of failing. The router retries scoring every 5 seconds as node states change.

### Pre-Warming

When a primary node's queue gets deep, the router proactively loads the same model on the runner-up node. The next request hits a hot model instead of waiting.

### Background Rebalancer

Runs every 5 seconds, moving queued requests from overloaded nodes to nodes with spare capacity — but only where the model is already loaded.

### Zombie Reaper

Detects and cleans up stuck in-flight requests that never completed. Keeps queues accurate.

## Multimodal Support

### LLM Inference

Full support for chat completions and text generation. Both streaming and non-streaming. OpenAI and Ollama API formats.

### Embeddings

Route embedding requests to the node with the embedding model loaded. Supports `/api/embed`, `/api/embeddings`, and `/v1/embeddings`.

### Image Generation

Routes image generation requests to Apple Silicon nodes running mflux (FLUX models) or DiffusionKit. Supports FLUX Schnell, FLUX Dev, Stable Diffusion 3, and Ollama native image models. OpenAI-compatible `/v1/images/generations` endpoint included.

### Speech-to-Text

Routes transcription requests to nodes with MLX and Qwen3-ASR installed. Apple Silicon only.

### Model Pulling

Pull models onto fleet nodes through the router. Auto-selects the node with the most available memory, or target a specific node. Streams progress in real time.

## Real-Time Dashboard

A web dashboard at `/dashboard` with eight tabs:

- **Fleet Overview** — Live node cards, queue depths, request counts via Server-Sent Events
- **Trends** — Requests per hour, average latency, token throughput charts (24h-7d)
- **Model Insights** — Per-model latency, tokens/sec, usage comparison
- **Apps** — Per-app analytics with request volume, latency, tokens, error rates
- **Benchmarks** — Capacity growth over time with per-run throughput and latency percentiles
- **Health** — 15 automated health checks with severity levels
- **Recommendations** — AI-powered model mix recommendations per node
- **Settings** — Runtime toggles, config overview, node version tracking

No external dependencies. No build process. Opens in any browser.

## Health Monitoring

### 15 Automated Health Checks

The health engine continuously monitors:

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

Each check has a severity level and actionable recommendation. Available via the dashboard and the `/dashboard/api/health` endpoint.

## API Compatibility

### OpenAI Format
- `POST /v1/chat/completions` — chat completions (streaming + non-streaming)
- `GET /v1/models` — list available models
- `POST /v1/embeddings` — generate embeddings
- `POST /v1/images/generations` — generate images

### Ollama Format
- `POST /api/chat` — chat completions
- `POST /api/generate` — text generation
- `POST /api/pull` — pull models onto fleet nodes
- `GET /api/tags` — list all models
- `GET /api/ps` — list loaded models
- `POST /api/embed` — generate embeddings
- `POST /api/embeddings` — generate embeddings (alternative)

### Fleet Management
- `GET /fleet/status` — full fleet state
- `GET /fleet/queue` — lightweight queue depths

Works with Open WebUI, LangChain, CrewAI, AutoGen, Aider, Continue.dev, LlamaIndex, LiteLLM, and any OpenAI-compatible client. Just change the base URL.

## Request Tagging

Tag requests with an app identifier to get per-application analytics. Add `X-App-Tag: my-app` to any request and the dashboard breaks down usage by app — request volume, latency, tokens, error rates. See which tools consume the most fleet resources.

## Platform Support

| Feature | macOS | Linux | Windows |
|---------|:-----:|:-----:|:-------:|
| LLM routing, scoring, queues | Yes | Yes | Yes |
| Embeddings | Yes | Yes | Yes |
| mDNS auto-discovery | Yes | Yes | Yes |
| Dashboard & traces | Yes | Yes | Yes |
| Image gen (mflux, DiffusionKit) | Apple Silicon | -- | -- |
| Image gen (Ollama native) | Yes | Yes | Yes |
| Speech-to-text (MLX) | Apple Silicon | -- | -- |
| Meeting detection | Yes | -- | -- |
| Memory pressure detection | Yes | Yes | -- |

Core routing works identically on all platforms. macOS-only features degrade gracefully on other OSes.

## Configuration

All settings via environment variables with `FLEET_` prefix (server) or `FLEET_NODE_` prefix (node). 44+ configuration options covering scoring weights, queue behavior, retry limits, heartbeat intervals, and more. Sensible defaults mean you don't need to touch any of them to get started.
