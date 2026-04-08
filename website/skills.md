# AI Agent Skill

Ollama Herd ships with a ClawHub skill that gives AI agents instant knowledge of your fleet. Install it once and any OpenClaw-compatible agent can check node health, route requests, pull models, run diagnostics, and manage your fleet — all through natural language.

## What the Skill Does

The Ollama Herd skill teaches your AI agent:

- **Fleet operations** — check node status, queue depths, loaded models, health scores
- **Request routing** — send LLM, embedding, image generation, and speech-to-text requests through the fleet
- **Model management** — pull models to specific nodes, list available models, check hot vs. cold state
- **Diagnostics** — query the trace database, read JSONL logs, identify slow requests and bottlenecks
- **Health monitoring** — 15 automated health checks covering offline nodes, memory pressure, thermal throttling, model thrashing, error rates, and more
- **Dashboard access** — point users to the real-time web dashboard for visual monitoring

## Install

```bash
clawhub install ollama-herd
```

Or search for it on [ClawHub](https://clawhub.com):

```bash
clawhub search "ollama herd"
```

## What Your Agent Can Do After Install

**Check fleet health:**
> "How's my Ollama fleet doing?"

The agent hits `/fleet/status` and `/dashboard/api/health`, then summarizes node status, loaded models, queue depths, and any health warnings.

**Route a request:**
> "Ask llama3.3:70b to explain quicksort"

The agent sends the request through the fleet router. Herd picks the best node automatically.

**Pull a model:**
> "Pull codestral onto the Mac Studio"

The agent calls `/api/pull` with the target node. Streams progress in real time.

**Diagnose issues:**
> "Why were my responses slow yesterday?"

The agent queries the trace database for high-latency entries, checks for thermal throttling or memory pressure events, and reports what it finds.

**Get recommendations:**
> "What models should I run on each machine?"

The agent hits `/dashboard/api/recommendations` for AI-powered model mix suggestions based on your hardware and usage patterns.

## Supported Platforms

The skill works with Ollama Herd on **macOS, Linux, and Windows**. Platform-specific features (image generation, speech-to-text, meeting detection) are noted in the skill and the agent adapts its suggestions accordingly.

## API Endpoints the Skill Uses

| Endpoint | Purpose |
|----------|---------|
| `GET /fleet/status` | Full fleet state — nodes, models, queues |
| `GET /fleet/queue` | Lightweight queue depths for backoff |
| `POST /api/chat` | Ollama-format chat completions |
| `POST /v1/chat/completions` | OpenAI-format chat completions |
| `POST /api/pull` | Pull models onto fleet nodes |
| `GET /api/tags` | List all available models |
| `GET /api/ps` | List models currently loaded in memory |
| `POST /api/embed` | Generate embeddings |
| `GET /dashboard/api/health` | 15 automated health checks |
| `GET /dashboard/api/traces` | Recent request traces with scoring details |
| `GET /dashboard/api/recommendations` | Model mix recommendations per node |
| `GET /dashboard/api/usage` | Per-node, per-model usage statistics |
| `GET /dashboard/api/settings` | Current fleet configuration |

## Source

- **ClawHub:** [ollama-herd](https://clawhub.com/skills/ollama-herd)
- **GitHub:** [geeks-accelerator/ollama-herd](https://github.com/geeks-accelerator/ollama-herd)
- **PyPI:** [ollama-herd](https://pypi.org/project/ollama-herd/)
