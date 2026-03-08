# Ollama Herd

Smart inference router that herds your Ollama instances into one endpoint. Auto-discovers nodes via mDNS, scores them on 5 signals (thermal state, memory fit, queue depth, latency history, role affinity), and routes each request to the optimal device. OpenAI-compatible API with real-time dashboard.

## Why

You have multiple machines with GPUs sitting around. You want one endpoint that makes them act like one system — picking the right device for each request automatically, without manual load balancing or config files.

## Quick start

```bash
uv sync  # or: pip install .
```

**On your router machine:**

```bash
herd
```

**On each device running Ollama:**

```bash
herd-node
```

That's it. The node discovers the router via mDNS and starts sending heartbeats. No config files needed.

> To skip mDNS and connect directly: `herd-node --router-url http://router-ip:11435`

## Usage

> **Already using Ollama or the OpenAI SDK?** Just swap your base URL to the router. No code changes needed — same model names, same API, same streaming. The router handles picking the best machine.

Point any OpenAI-compatible client at the router:

```python
from openai import OpenAI

client = OpenAI(base_url="http://router-ip:11435/v1", api_key="not-needed")
response = client.chat.completions.create(
    model="llama3.2:3b",
    messages=[{"role": "user", "content": "Hello!"}],
    stream=True,
)
for chunk in response:
    print(chunk.choices[0].delta.content, end="")
```

Or use the Ollama API directly:

```bash
curl http://router-ip:11435/api/chat -d '{
  "model": "llama3.2:3b",
  "messages": [{"role": "user", "content": "Hello!"}]
}'
```

Both formats support streaming and non-streaming. Responses include real token usage counts.

### Model Fallbacks

Specify backup models in case the primary isn't available:

```bash
curl http://router-ip:11435/v1/chat/completions -d '{
  "model": "llama3.3:70b",
  "fallback_models": ["qwen2.5:32b", "qwen2.5:7b"],
  "messages": [{"role": "user", "content": "Hello!"}]
}'
```

The router tries each model in order. If one is unavailable, it seamlessly falls back to the next. See [Operations Guide](docs/operations-guide.md#model-fallbacks).

### Request Tagging

Tag requests to track performance and usage per application, team, or environment:

```bash
curl http://router-ip:11435/v1/chat/completions -d '{
  "model": "llama3.2:3b",
  "metadata": {"tags": ["my-app", "production"]},
  "messages": [{"role": "user", "content": "Hello!"}]
}'
```

Or via header (useful for proxies and middleware):

```bash
curl -H "X-Herd-Tags: my-app, production" \
  http://router-ip:11435/v1/chat/completions -d '{
  "model": "llama3.2:3b",
  "messages": [{"role": "user", "content": "Hello!"}]
}'
```

Tags appear in the **Apps** dashboard tab with per-tag latency, tokens, error rates, and daily trends. See [Request Tagging](docs/request-tagging.md) for the full guide including competitive landscape and framework integration examples.

### Switching your AI agent to Herd

Copy-paste this to any AI coding agent to have it update your Ollama configuration:

> Update the Ollama configuration to use Ollama Herd as the inference router. Ollama Herd is a smart inference router that sits in front of multiple Ollama instances across a LAN, auto-discovers nodes via mDNS, and routes each request to the optimal device based on thermal state, memory fit, queue depth, latency history, and role affinity. Comment out the current direct Ollama base URL (e.g., `http://localhost:11434`) but keep it as a comment for reference. Replace it with the Ollama Herd router URL: `http://<router-ip>:11435`. For OpenAI-compatible clients, use `http://<router-ip>:11435/v1` as the base URL. The API key can be any non-empty string (e.g., `"not-needed"`). The API is fully compatible with both OpenAI and Ollama formats — same model names, same endpoints, same streaming. No other code changes are needed.

**Tagging requests for per-project analytics:**

> Tag all requests to Ollama Herd so we can track usage per project and process. Add a `metadata` field with a `tags` array to every request body. Use two tags: one for the project name and one for the script or process making the request. For example: `"metadata": {"tags": ["my-project", "code-review"]}`. If you're using the OpenAI SDK, pass it via `extra_body`: `client.chat.completions.create(..., extra_body={"metadata": {"tags": ["my-project", "code-review"]}})`. If you can't modify the request body (e.g., reverse proxy or middleware), use the `X-Herd-Tags` header instead: `X-Herd-Tags: my-project, code-review`. Tags appear in the Herd dashboard under the Apps tab with per-tag latency, token counts, error rates, and daily trends. Keep tag names short, lowercase, and hyphenated.

## How routing works

Every request goes through a scoring pipeline that picks the best device in real time:

1. **Elimination** — offline nodes, missing models, insufficient memory, and critical memory pressure are filtered out
2. **Thermal state** (+50 pts) — models already loaded in GPU memory ("hot") score highest; recently unloaded ("warm") get a partial bonus
3. **Memory fit** (+20 pts) — nodes with more available headroom score higher
4. **Queue depth** (−30 pts) — busy nodes get penalized (capped so no node is starved)
5. **Latency history** (−25 pts) — past p75 latency from SQLite informs expected wait time
6. **Role affinity** (+15 pts) — large models prefer big machines, small models prefer small ones

The highest-scoring node wins. If no node is available, the request enters a holding queue and retries until one frees up or times out.

For full details on the scoring algorithm, pre-warm triggers, and rebalancer: [Fleet Manager Routing Engine](docs/fleet-manager-routing-engine.md).

## Resilience

- **Auto-retry** — if a node fails before the first response chunk, the router re-scores and retries on the next-best node (up to 2 retries)
- **Model fallbacks** — clients specify backup models; the router tries alternatives when the primary model has no available nodes
- **Holding queue** — requests wait (up to 30s) when all nodes are busy rather than immediately failing
- **Graceful drain** — when a node shuts down, in-flight requests finish and pending requests are redistributed

See [Operations Guide](docs/operations-guide.md) for details.

## Adaptive Capacity Learning

Laptops aren't servers — their owners use them for meetings, coding, and browsing. The adaptive capacity system learns when each device has spare compute:

- **168-slot behavioral model** — learns your weekly usage patterns (7 days × 24 hours)
- **Meeting detection** — camera/mic active → hard pause (macOS)
- **App fingerprinting** — classifies workload intensity from resource signatures, privacy-first (no app name reading)
- **Dynamic memory ceiling** — availability score maps to how much RAM the router can use for Ollama

Enable with `FLEET_NODE_ENABLE_CAPACITY_LEARNING=true`. See [Adaptive Capacity Learning](docs/adaptive-capacity.md).

## Dashboard

The built-in dashboard at `/dashboard` provides five views:

- **Fleet Overview** — live node status, CPU/memory metrics, loaded models, and request queue depths via Server-Sent Events
- **Trends** — historical charts for requests per hour, average latency, and token throughput (prompt + completion) with selectable time ranges (24h–7d)
- **Model Insights** — per-model comparison of latency, tokens/sec, and usage; token distribution doughnut chart; clickable rows for daily breakdown
- **Apps** — per-tag analytics with request volume, latency, tokens, error rates, and daily trends; tag your requests to see per-application breakdowns
- **Benchmarks** — capacity growth over time with per-run throughput, latency percentiles, per-model and per-node breakdowns

All powered by Chart.js and a SQLite-backed latency store. No external database required.

## Observability

- **Per-request traces** — every routing decision is recorded with scores, node selection, latency, tokens, tags, retry/fallback status
- **Per-app analytics** — tag requests with `metadata.tags` or `X-Herd-Tags` header for per-application breakdowns
- **Usage stats** — per-node, per-model, per-day aggregates via `/dashboard/api/usage`
- **JSONL structured logging** — daily rotation to `~/.fleet-manager/logs/herd.jsonl`, 30-day retention

See [Operations Guide](docs/operations-guide.md) for log queries, trace access, and debugging.

## API endpoints

| Endpoint | Description |
|----------|-------------|
| `POST /v1/chat/completions` | OpenAI-compatible chat (streaming + non-streaming) |
| `GET /v1/models` | List all models across the herd |
| `POST /api/chat` | Ollama-compatible chat |
| `POST /api/generate` | Ollama-compatible generate |
| `GET /api/tags` | Ollama-compatible model list |
| `GET /api/ps` | Running models across all nodes |
| `GET /fleet/status` | Herd state: nodes, queues, metrics |
| `GET /dashboard` | Real-time web dashboard |
| `GET /dashboard/events` | SSE stream for live fleet updates |
| `GET /dashboard/api/trends` | Hourly aggregated stats (JSON) |
| `GET /dashboard/api/models` | Per-model daily stats (JSON) |
| `GET /dashboard/api/overview` | Summary totals (JSON) |
| `GET /dashboard/api/usage` | Per-node per-model usage (JSON) |
| `GET /dashboard/api/apps` | Per-tag aggregated stats (JSON) |
| `GET /dashboard/api/apps/daily` | Per-tag daily breakdown (JSON) |
| `GET /dashboard/api/traces` | Recent request traces (JSON) |
| `GET /dashboard/api/benchmarks` | Benchmark run history (JSON) |
| `POST /dashboard/api/benchmarks` | Save benchmark results (JSON) |
| `GET /dashboard/benchmarks` | Benchmarks dashboard page |

Full request/response schemas: [API Reference](docs/api-reference.md).

## Agent Framework Integration

Every major agent framework supports custom `base_url` — point it at Herd and your agents run across your entire device fleet:

```python
# LangChain
llm = ChatOpenAI(base_url="http://router-ip:11435/v1", model="llama3.3:70b", api_key="none")

# CrewAI
llm = LLM(model="ollama/llama3.3:70b", base_url="http://router-ip:11435")

# OpenHands
export LLM_BASE_URL=http://router-ip:11435/v1
```

Compatible with: OpenClaw, LangChain, CrewAI, AutoGen, LlamaIndex, Haystack, smolagents, OpenHands, Aider, Cline, Continue.dev, Bolt.diy, and any OpenAI-compatible client.

See [OpenClaw Integration Guide](docs/openclaw-integration.md) and [Project Strategy](docs/project-status-and-strategy.md#agent-framework-integration) for the full compatibility matrix.

## Design Philosophy

Six principles shape every decision in this project:

- **Every node stands alone** — Each device is sovereign. It runs its own Ollama, manages its own models, learns its own capacity patterns, and works fine without the router. The router coordinates but never controls. No central config file. No dependency chains. A node that loses connectivity keeps serving local inference.

- **Two-person scale** — Two CLI commands, zero config files, zero Docker. If it requires a manual, it's too complex. Every architectural choice picks the simple thing (HTTP heartbeats over gRPC, SQLite over Postgres, mDNS over etcd). The whole codebase fits in one person's head.

- **Human-readable state** — JSONL logs you can `grep`. SQLite you can query with standard tools. JSON config on disk. Env vars for settings. No opaque binary formats. If you can't debug it with `cat` and `sqlite3`, it's wrong.

- **The inference request is primary** — Scoring, queuing, retry, fallback, capacity learning, meeting detection — everything exists to serve one thing: get the best response on the best machine as fast as possible. If a feature doesn't serve that, it doesn't belong.

- **AI as resident, not visitor** — The system accumulates knowledge over time. The capacity learner builds a 168-slot behavioral model of your week. The latency store remembers which nodes are fast for which models. The trace store records every routing decision. It gets smarter the longer it runs.

- **Shared DNA, not shared code** — The scoring pipeline (eliminate → score → rank → select), heartbeat-based coordination, and adaptive capacity learning are transferable patterns, not a framework. Specific tool, transferable DNA.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  Client (OpenAI SDK, curl, any HTTP client)         │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│  Herd Router (:11435)                               │
│  ┌────────────┐ ┌──────────┐ ┌───────────────────┐  │
│  │  Scoring    │ │  Queue   │ │  Streaming Proxy  │  │
│  │  Engine     │ │  Manager │ │  (format convert) │  │
│  └────────────┘ └──────────┘ └───────────────────┘  │
│  ┌────────────┐ ┌──────────┐ ┌───────────────────┐  │
│  │  Latency   │ │  Rebal-  │ │  Dashboard +      │  │
│  │  Store     │ │  ancer   │ │  SSE + Charts     │  │
│  └────────────┘ └──────────┘ └───────────────────┘  │
│  ┌────────────┐ ┌──────────┐                        │
│  │  Trace     │ │  Pre-    │                        │
│  │  Store     │ │  Warm    │                        │
│  └────────────┘ └──────────┘                        │
└──────────┬──────────────────────────┬───────────────┘
           │ heartbeats               │ inference
           ▼                          ▼
┌──────────────────┐       ┌──────────────────┐
│  Herd Node A     │       │  Herd Node B     │
│  (agent + Ollama)│       │  (agent + Ollama)│
│  ┌────────────┐  │       │  ┌────────────┐  │
│  │  Capacity  │  │       │  │  LAN Proxy  │  │
│  │  Learner   │  │       │  │  (auto TCP) │  │
│  └────────────┘  │       └──└────────────┘──┘
└──────────────────┘
```

Two CLI entry points, one Python package:

- **`herd`** — FastAPI server with scoring, queues, streaming proxy, trace store, and dashboard
- **`herd-node`** — lightweight agent that collects system metrics, sends heartbeats, and optionally learns capacity patterns

## Optimize Ollama for your hardware

Ollama's defaults are conservative. On machines with lots of memory, you're probably leaving performance on the table. These settings tell Ollama to actually use the hardware you paid for:

```bash
# Keep models loaded permanently (default: 5m — unloads after 5 minutes of idle!)
# On a 512GB Mac Studio, there's zero reason to unload a model after 5 minutes
launchctl setenv OLLAMA_KEEP_ALIVE "-1"

# Allow multiple models in memory simultaneously (default: auto, but often conservative)
# Set to -1 for unlimited — let Ollama load as many as fit in memory
launchctl setenv OLLAMA_MAX_LOADED_MODELS "-1"

# Restart Ollama app after changing these (⌘Q and reopen)
```

**Herd handles this automatically for routed requests** — every request proxied through the router includes `keep_alive: -1`, so models loaded via Herd stay loaded regardless of Ollama's server-side default. But you should still set the env var to cover models loaded directly (e.g., `ollama run`) and to prevent Ollama from evicting idle models between requests.

| Setting | Default | Recommended | Why |
|---------|---------|-------------|-----|
| `OLLAMA_KEEP_ALIVE` | `5m` | `-1` (forever) | Don't unload models from memory when you have RAM to spare |
| `OLLAMA_MAX_LOADED_MODELS` | auto | `-1` (unlimited) | Let multiple models stay hot simultaneously |
| `OLLAMA_NUM_PARALLEL` | auto | Leave default | Ollama auto-calculates from available memory per model |

**Quick check** — run `ollama ps` and look at the "Until" column:
```
NAME              SIZE     UNTIL
gpt-oss:120b      88 GB    Forever     ← good: model stays loaded
qwen3.5:122b      87 GB    Forever     ← good: both hot, no thrashing
```

If you see a timestamp instead of "Forever", your keep-alive is too short.

> **macOS note:** `launchctl setenv` sets the variable for the GUI session. For `ollama serve` from the terminal, use `export OLLAMA_KEEP_ALIVE=-1` instead. On Linux, add it to your systemd service file or shell profile.

## Configuration

All settings via environment variables. See [Configuration Reference](docs/configuration-reference.md) for the complete list of 29+ variables with tuning guidance.

### Common variables

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_PORT` | `11435` | Router listen port |
| `FLEET_HOST` | `0.0.0.0` | Router bind address |
| `FLEET_HEARTBEAT_INTERVAL` | `5.0` | Heartbeat check interval (seconds) |
| `FLEET_HEARTBEAT_TIMEOUT` | `15.0` | Mark node degraded after (seconds) |
| `FLEET_HEARTBEAT_OFFLINE` | `30.0` | Mark node offline after (seconds) |
| `FLEET_MAX_RETRIES` | `2` | Auto-retry attempts on node failure |
| `FLEET_LOG_LEVEL` | `DEBUG` | JSONL log file level |

Node settings use the `FLEET_NODE_` prefix:

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_NODE_OLLAMA_HOST` | `http://localhost:11434` | Local Ollama URL |
| `FLEET_NODE_ROUTER_URL` | *(auto-discover)* | Router URL (skips mDNS) |
| `FLEET_NODE_ENABLE_CAPACITY_LEARNING` | `false` | Enable adaptive capacity system |

## Development

```bash
uv sync                              # install deps
uv run herd                          # start router
uv run herd-node                     # start node agent

uv run pytest -v                     # run all 212 tests (~4s)
uv run ruff check src/               # lint
uv run ruff format src/              # format
```

## Documentation

| Document | Description |
|----------|-------------|
| [API Reference](docs/api-reference.md) | All endpoints with request/response schemas |
| [Configuration Reference](docs/configuration-reference.md) | All 29+ environment variables with tuning guidance |
| [Operations Guide](docs/operations-guide.md) | Logging, traces, fallbacks, retry, drain, pre-warm, streaming |
| [Adaptive Capacity](docs/adaptive-capacity.md) | Capacity learner, meeting detection, app fingerprinting |
| [Routing Engine](docs/fleet-manager-routing-engine.md) | 5-stage scoring pipeline deep dive |
| [OpenClaw Integration](docs/openclaw-integration.md) | Setup guide for OpenClaw agents |
| [Request Tagging](docs/request-tagging.md) | Per-app analytics, tagging strategies, competitive landscape |
| [Troubleshooting](docs/troubleshooting.md) | Common issues, LAN debugging, operational gotchas |
| [Architecture Decisions](docs/architecture-decisions.md) | Port selection, design trade-offs, rationale |
| [Project Strategy](docs/project-status-and-strategy.md) | Competitive landscape and agent framework matrix |
| [Agentic Router Vision](docs/agentic-router-vision.md) | What's next: proactive fleet intelligence and task backlogs |

## What's Next

The fleet is smart but passive — it waits for requests. The next evolution is an **agentic router** that uses idle compute proactively:

- **Task backlogs** — drop tasks throughout the day, the fleet chews through them when idle
- **Pattern-driven pre-warming** — the capacity learner already knows your weekly rhythm, the router should act on it
- **Agentic decomposition** — complex tasks broken into subtask DAGs, executed in parallel across the fleet
- **Fleet health opinions** — the router surfaces observations, not just metrics

> *"Your fleet doesn't just wait for requests — it works for you while you sleep."*

See [Agentic Router Vision](docs/agentic-router-vision.md) for the full design.

## Scale your AI agent's brain

Running an AI coding agent like OpenClaw, Aider, or Continue.dev with a local Ollama? You're limited to one machine's GPU. Ollama Herd turns every device on your network into extra capacity — your laptop, your desktop, that Mac Mini in the closet.

1. Install Ollama on each device and pull the models you want
2. Run `herd-node` on each device (one command, zero config)
3. Run `herd` on any machine to start the router
4. Point your agent at `http://router-ip:11435/v1` instead of `http://localhost:11434`

Your agent doesn't know or care that multiple machines are behind the endpoint. It sees one API with the same models, same streaming, same formats. The router picks the best device for each request — the one with the model already loaded, the most free memory, the lowest queue depth. When one machine is busy in a meeting, requests flow to the others automatically.

This is especially powerful for agentic workflows that fire many parallel requests — code review, test generation, documentation — the fleet absorbs the burst across all available GPUs instead of queuing everything on one.

## Contributing

Whether you're carbon-based or silicon-based, contributions are welcome. This project is built by humans and AI agents working together — every commit, every observation, every pattern.

**For humans:** Fork it, run the tests (`uv run pytest`), make your change, open a PR. The codebase is designed to fit in one person's head. Start with [Architecture Decisions](docs/architecture-decisions.md) to understand why things are the way they are.

**For AI agents:** Read `CLAUDE.md` first — it's your onboarding doc. The project uses [`docs/issues.md`](docs/issues.md) to track what's broken and [`docs/observations.md`](docs/observations.md) to accumulate what we've learned. After making a significant change, check if your work produced a new observation or revealed a new issue, and append it. That's how the project gets smarter across sessions.

**Good first contributions:**
- Pick an open issue from [`docs/issues.md`](docs/issues.md) and fix it
- Add test coverage for an untested module (see issue #10)
- Run the fleet and add an observation to [`docs/observations.md`](docs/observations.md)
- Integrate with a new agent framework and document it

⭐ **If Ollama Herd is useful to you, [star the repo](https://github.com/geeks-accelerator/ollama-herd)** — it helps others discover the project and keeps the herd growing.

## Requirements

- Python 3.11+
- [Ollama](https://ollama.com) running on each device
- Multi-device setups work automatically — the node agent starts a LAN proxy if Ollama is only listening on localhost

## License

MIT
