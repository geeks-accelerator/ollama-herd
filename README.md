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

## How routing works

Every request goes through a scoring pipeline that picks the best device in real time:

1. **Elimination** — offline nodes, missing models, insufficient memory, and critical memory pressure are filtered out
2. **Thermal state** — models already loaded in GPU memory ("hot") score highest; recently unloaded ("warm") get a partial bonus
3. **Memory fit** — nodes with more available headroom score higher
4. **Queue depth** — busy nodes get penalized (capped so no node is starved)
5. **Latency history** — past p75 latency from SQLite informs expected wait time
6. **Role affinity** — large models prefer big machines, small models prefer small ones

The highest-scoring node wins. If no node is available, the request enters a holding queue and retries until one frees up or times out.

## Dashboard

The built-in dashboard at `/dashboard` provides three views:

- **Fleet Overview** — live node status, CPU/memory metrics, loaded models, and request queue depths via Server-Sent Events
- **Trends** — historical charts for requests per hour, average latency, and token throughput (prompt + completion) with selectable time ranges (24h–7d)
- **Model Insights** — per-model comparison of latency, tokens/sec, and usage; token distribution doughnut chart; clickable rows for daily breakdown

All powered by Chart.js and a SQLite-backed latency store. No external database required.

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
| `GET /dashboard/trends` | Historical trends page |
| `GET /dashboard/models` | Model insights page |
| `GET /dashboard/api/trends` | Hourly aggregated stats (JSON) |
| `GET /dashboard/api/models` | Per-model daily stats (JSON) |
| `GET /dashboard/api/overview` | Summary totals (JSON) |

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
└──────────┬──────────────────────────┬───────────────┘
           │ heartbeats               │ inference
           ▼                          ▼
┌──────────────────┐       ┌──────────────────┐
│  Herd Node A     │       │  Herd Node B     │
│  (agent + Ollama)│       │  (agent + Ollama)│
└──────────────────┘       └──────────────────┘
```

Two CLI entry points, one Python package:

- **`herd`** — FastAPI server with scoring, queues, streaming proxy, and dashboard
- **`herd-node`** — lightweight agent that collects system metrics and sends heartbeats

## Configuration

All settings via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_PORT` | `11435` | Router listen port |
| `FLEET_HOST` | `0.0.0.0` | Router bind address |
| `FLEET_HEARTBEAT_INTERVAL` | `5.0` | Heartbeat check interval (seconds) |
| `FLEET_HEARTBEAT_TIMEOUT` | `15.0` | Mark node degraded after (seconds) |
| `FLEET_HEARTBEAT_OFFLINE` | `30.0` | Mark node offline after (seconds) |

Node settings use the `FLEET_NODE_` prefix:

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_NODE_OLLAMA_HOST` | `http://localhost:11434` | Local Ollama URL |
| `FLEET_NODE_ROUTER_URL` | *(auto-discover)* | Router URL (skips mDNS) |

## Development

```bash
uv sync                              # install deps
uv run herd                          # start router
uv run herd-node                     # start node agent

uv run pytest -v                     # run all 107 tests (~0.6s)
uv run ruff check src/               # lint
uv run ruff format src/              # format
```

## Requirements

- Python 3.11+
- [Ollama](https://ollama.com) running on each device
- For multi-device setups: Ollama bound to `0.0.0.0` (`OLLAMA_HOST=0.0.0.0`)

## License

MIT
