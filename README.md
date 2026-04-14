# Ollama Herd

[![PyPI version](https://img.shields.io/pypi/v/ollama-herd?color=00c853)](https://pypi.org/project/ollama-herd/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

Turn all your devices into one local AI cluster. Ollama Herd is a smart inference router and load balancer that auto-discovers Ollama nodes via mDNS, routes LLMs, image generation, speech-to-text, and embeddings to the optimal device using intelligent scoring. OpenAI-compatible API. Zero config. Zero cost.

<!-- TODO: Add dashboard screenshot here -->
<!-- ![Ollama Herd Dashboard](docs/images/dashboard-screenshot.png) -->

## Why Ollama Herd?

- **Your spare Mac is wasting compute** — pool all your devices into one fleet
- **Single Ollama bottlenecks agents** — distribute requests across machines automatically
- **Cloud APIs cost $450-1,800/month at fleet scale** — local inference is zero marginal cost
- **No config files, no Docker, no Kubernetes** — two commands, mDNS auto-discovery
- **Not just LLMs** — routes image generation (FLUX), speech-to-text (Qwen3-ASR), and embeddings too
- **The fleet gets smarter over time** — capacity learning, thermal awareness, meeting detection

## Quick Start

```bash
pip install ollama-herd
```

Or with Homebrew (macOS/Linux):

```bash
brew tap geeks-accelerator/ollama-herd
brew install ollama-herd
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

## Features

| Feature | Description |
|---------|------------|
| **Smart Scoring** | Routes to the best device based on thermal state, memory fit, queue depth, latency, affinity, availability, and context fit |
| **Zero-Config Discovery** | mDNS auto-discovery — no IPs, no config files, no manual setup |
| **Multimodal Routing** | LLMs, embeddings, image gen (FLUX via mflux/DiffusionKit), speech-to-text (Qwen3-ASR) |
| **Live Dashboard** | Fleet overview, trends, model insights, per-app analytics, benchmarks, health, recommendations, settings |
| **Capacity Learning** | 168-slot weekly behavioral model per device — learns when your machines are available |
| **Auto-Retry & Fallbacks** | Transparent retry on failure + client-specified backup models |
| **Thinking Model Support** | Auto-detects DeepSeek-R1, QwQ, phi-4-reasoning and inflates token budgets to prevent empty responses |
| **Smart Benchmarks** | Auto-discovers fleet, benchmarks all 4 model types, tracks performance over time |
| **Dynamic Context** | Measures actual token usage, auto-adjusts context windows to free KV cache memory |
| **Fleet Intelligence** | AI-generated fleet briefings with health summaries, trend analysis, and actionable recommendations |
| **Health Engine** | 17 automated checks: memory, thermal, context waste, thrashing, timeouts, errors, zombies, and more |
| **Request Tagging** | Per-app analytics via tags — track usage, latency, and errors per application or team |

## Usage

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

### Model Fallbacks

```bash
curl http://router-ip:11435/v1/chat/completions -d '{
  "model": "llama3.3:70b",
  "fallback_models": ["qwen2.5:32b", "qwen2.5:7b"],
  "messages": [{"role": "user", "content": "Hello!"}]
}'
```

The router tries each model in order, falling back seamlessly if one is unavailable.

## Beyond LLMs

The same router handles four model types — install a backend on any node and it's automatically detected.

### Image Generation

```bash
# Install a backend (any node)
uv tool install mflux

# Generate
curl -o sunset.png http://router-ip:11435/api/generate-image \
  -d '{"model": "z-image-turbo", "prompt": "a sunset over mountains", "width": 1024, "height": 1024}'
```

Supports mflux (FLUX), DiffusionKit (Stable Diffusion 3/3.5), and Ollama native models. See [Image Generation Guide](docs/guides/image-generation.md).

### Speech-to-Text

```bash
# Install backend (any node)
pip install 'mlx-qwen3-asr[serve]'

# Transcribe
curl http://router-ip:11435/api/transcribe -F "file=@meeting.wav" -F "model=qwen3-asr"
```

### Embeddings

```bash
curl http://router-ip:11435/api/embed \
  -d '{"model": "nomic-embed-text", "input": ["first document", "second document"]}'
```

Works with any Ollama embedding model: `nomic-embed-text`, `mxbai-embed-large`, `all-minilm`, `snowflake-arctic-embed`.

## Works With

Ollama Herd is a drop-in replacement — just change the base URL:

| Framework | Integration |
|-----------|------------|
| **Open WebUI** | Set Ollama URL to `http://router-ip:11435` in admin settings |
| **LangChain** | `ChatOpenAI(base_url="http://router-ip:11435/v1")` |
| **CrewAI** | `LLM(base_url="http://router-ip:11435")` |
| **Aider** | `--openai-api-base http://router-ip:11435/v1` |
| **Continue.dev** | Set `apiBase` in config.json |
| **OpenHands** | `LLM_BASE_URL=http://router-ip:11435/v1` |
| **OpenClaw** | See [OpenClaw Integration Guide](docs/openclaw-integration.md) |
| **Any OpenAI client** | Change `base_url` to `http://router-ip:11435/v1` |

## Platform Support

Ollama Herd runs on **macOS, Linux, and Windows** — anywhere Ollama runs.

| Feature | macOS | Linux | Windows |
|---------|:-----:|:-----:|:-------:|
| LLM routing, scoring, queues | Yes | Yes | Yes |
| Embeddings proxy | Yes | Yes | Yes |
| mDNS auto-discovery | Yes | Yes | Yes |
| Dashboard & traces | Yes | Yes | Yes |
| Image gen (mflux, DiffusionKit) | Yes (Apple Silicon) | -- | -- |
| Image gen (Ollama native) | Yes | Yes | Yes |
| Speech-to-text (MLX) | Yes (Apple Silicon) | -- | -- |
| Meeting detection (camera/mic) | Yes | -- | -- |
| Memory pressure detection | Yes | Yes | -- |

Core routing works identically on all platforms. macOS-only features degrade gracefully.

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
│  │  Trace     │ │  Health  │ │  Dashboard +      │  │
│  │  Store     │ │  Engine  │ │  SSE + Charts     │  │
│  └────────────┘ └──────────┘ └───────────────────┘  │
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

- **`herd`** — FastAPI server with scoring, queues, streaming proxy, trace store, health engine, and dashboard
- **`herd-node`** — lightweight agent that collects system metrics, sends heartbeats, and optionally learns capacity patterns

## Documentation

| Document | Description |
|----------|-------------|
| [API Reference](docs/api-reference.md) | All endpoints with request/response schemas |
| [Configuration Reference](docs/configuration-reference.md) | All 47+ environment variables with tuning guidance |
| [Operations Guide](docs/operations-guide.md) | Logging, traces, fallbacks, retry, drain, streaming, context protection |
| [Routing Engine](docs/fleet-manager-routing-engine.md) | Scoring pipeline deep dive |
| [Adaptive Capacity](docs/adaptive-capacity.md) | Capacity learner, meeting detection, app fingerprinting |
| [Request Tagging](docs/request-tagging.md) | Per-app analytics and tagging strategies |
| [Thinking Models](docs/guides/thinking-models.md) | Chain-of-thought models, budget inflation, diagnostic headers |
| [Image Generation](docs/guides/image-generation.md) | mflux, DiffusionKit, Ollama native setup |
| [Troubleshooting](docs/troubleshooting.md) | Common issues, LAN debugging, operational gotchas |
| [Changelog](CHANGELOG.md) | What's new in each release |

## Optimize Ollama for Your Hardware

Ollama's defaults are conservative. On machines with lots of memory, set these to actually use the hardware you paid for:

| Setting | Default | Recommended | Why |
|---------|---------|-------------|-----|
| `OLLAMA_KEEP_ALIVE` | `5m` | `-1` (forever) | Don't unload models from memory when you have RAM to spare |
| `OLLAMA_MAX_LOADED_MODELS` | auto | `-1` (unlimited) | Let multiple models stay hot simultaneously |
| `OLLAMA_NUM_PARALLEL` | auto | `2`-`4` | Prevents KV cache bloat on high-memory machines |

Set via `launchctl setenv` (macOS), `systemctl edit ollama` (Linux), or system environment variables (Windows). See [Configuration Reference](docs/configuration-reference.md) for details.

## Development

```bash
git clone https://github.com/geeks-accelerator/ollama-herd.git
cd ollama-herd
uv sync                              # install deps
uv run herd                          # start router
uv run herd-node                     # start node agent

uv sync --extra dev                  # install test deps
uv run pytest                        # run all tests (~5s)
uv run ruff check src/               # lint
uv run ruff format src/              # format
```

## Contributing

Whether you're carbon-based or silicon-based, contributions are welcome. This project is built by humans and AI agents working together.

**For humans:** Fork it, run the tests (`uv run pytest`), make your change, open a PR. Start with [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines and [Architecture Decisions](docs/architecture-decisions.md) for context.

**For AI agents:** Read `CLAUDE.md` first — it's your onboarding doc. The project uses [`docs/issues.md`](docs/issues.md) for bug tracking and [`docs/observations.md`](docs/observations.md) for operational learnings.

**Good first contributions:**
- Pick an open issue from [`docs/issues.md`](docs/issues.md)
- Integrate with a new agent framework and document it
- Run the fleet and add an observation to [`docs/observations.md`](docs/observations.md)

Questions? Open a [Discussion](https://github.com/geeks-accelerator/ollama-herd/discussions).

**If Ollama Herd is useful to you, [star the repo](https://github.com/geeks-accelerator/ollama-herd)** — it helps others discover the project and keeps the herd growing.

## Requirements

- Python 3.11+
- [Ollama](https://ollama.com) running on each device
- Multi-device setups work automatically — the node agent starts a LAN proxy if Ollama is only listening on localhost

## License

MIT
