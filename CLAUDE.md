# CLAUDE.md

## Build & Run

```bash
uv sync                          # install deps
uv run herd                      # start router on :11435
uv run herd-node                 # start node agent (auto-discovers router via mDNS)
uv run herd-node --router-url http://localhost:11435  # explicit router URL
```

## Test

```bash
uv pip install "pytest>=8.0" "pytest-asyncio>=0.24.0"  # install test deps (first time only)
uv run pytest                    # run all tests (~4s)
uv run pytest tests/test_server/ # run server tests only
uv run pytest tests/test_models/ # run model tests only
uv run pytest -v                 # verbose output
uv run ruff check src/           # lint
uv run ruff format src/          # format
```

## Architecture

Single Python package (`fleet_manager`), two CLI entry points:
- `herd` — FastAPI server (router + API + scoring + queues + dashboard)
- `herd-node` — agent that runs on each device (heartbeats + metrics + capacity learning)

### Key modules

| Module | Purpose |
|--------|---------|
| `server/registry.py` | In-memory node state tracking via heartbeats |
| `server/scorer.py` | 5-signal scoring: thermal (hot/warm/cold), memory fit, queue depth, wait time, role affinity |
| `server/queue_manager.py` | Per `node:model` queues with dynamic concurrent workers |
| `server/streaming.py` | httpx proxy to Ollama + format conversion (NDJSON ↔ SSE) + auto-retry |
| `server/latency_store.py` | aiosqlite persistence at `~/.fleet-manager/latency.db` |
| `server/trace_store.py` | Per-request trace log + usage stats in SQLite |
| `server/routes/routing.py` | Shared scoring logic with model fallback + holding queue + tag extraction |
| `server/rebalancer.py` | Background queue rebalancer + pre-warm trigger |
| `server/routes/openai_compat.py` | `/v1/chat/completions`, `/v1/models` |
| `server/routes/ollama_compat.py` | `/api/chat`, `/api/generate`, `/api/tags`, `/api/ps` |
| `server/routes/fleet.py` | `/fleet/status` — full fleet state |
| `server/routes/heartbeat.py` | `/heartbeat` — node agent heartbeat receiver |
| `server/routes/dashboard.py` | Real-time web dashboard at `/dashboard` with SSE updates |
| `node/agent.py` | Main loop: mDNS discovery, heartbeat, Ollama auto-start, SIGTERM drain |
| `node/collector.py` | Assembles HeartbeatPayload from psutil + Ollama |
| `node/capacity_learner.py` | 168-slot behavioral model, availability score, dynamic memory ceiling |
| `node/meeting_detector.py` | macOS camera/microphone detection → hard pause |
| `node/app_fingerprint.py` | Resource signature classification (idle/light/moderate/heavy/intensive) |
| `common/discovery.py` | AsyncZeroconf mDNS advertise + browse |
| `common/logging_config.py` | JSONL structured logging to `~/.fleet-manager/logs/` |

### Request flow

1. Client hits `/v1/chat/completions` or `/api/chat`
2. Route handler creates `InferenceRequest` (normalized)
3. `score_with_fallbacks()` — tries primary model, then fallbacks with holding queue
4. `ScoringEngine.score_request()` — eliminates bad nodes, scores survivors on 5 signals
5. `QueueManager.enqueue()` — places in `node:model` queue, returns Future
6. Queue worker calls `StreamingProxy.make_process_fn()` — httpx stream to Ollama with auto-retry
7. Response streamed back (SSE for OpenAI, NDJSON for Ollama format)
8. Trace recorded to SQLite, latency table updated

### Configuration

All settings via env vars with `FLEET_` prefix (server) or `FLEET_NODE_` prefix (node). See [`docs/configuration-reference.md`](docs/configuration-reference.md) for the complete 29+ variable reference.

## Documentation

| Document | Description |
|----------|-------------|
| [`docs/api-reference.md`](docs/api-reference.md) | All endpoints with request/response schemas |
| [`docs/configuration-reference.md`](docs/configuration-reference.md) | All 29+ env vars with tuning guidance |
| [`docs/operations-guide.md`](docs/operations-guide.md) | Logging, traces, fallbacks, retry, drain, pre-warm, streaming |
| [`docs/adaptive-capacity.md`](docs/adaptive-capacity.md) | Capacity learner, meeting detection, app fingerprinting |
| [`docs/fleet-manager-routing-engine.md`](docs/fleet-manager-routing-engine.md) | 5-stage scoring pipeline deep dive |
| [`docs/openclaw-integration.md`](docs/openclaw-integration.md) | Setup guide for OpenClaw agents |
| [`docs/request-tagging.md`](docs/request-tagging.md) | Per-app analytics, tagging strategies, competitive landscape |
| [`docs/troubleshooting.md`](docs/troubleshooting.md) | Common issues, LAN debugging, operational gotchas |
| [`docs/architecture-decisions.md`](docs/architecture-decisions.md) | Port selection, design trade-offs, rationale |
| [`docs/issues.md`](docs/issues.md) | Known issues, improvements, test coverage gaps |
| [`docs/project-status-and-strategy.md`](docs/project-status-and-strategy.md) | Competitive landscape and agent framework matrix |
| [`docs/agentic-router-vision.md`](docs/agentic-router-vision.md) | Vision: proactive fleet intelligence, task backlogs, agentic decomposition |

## Design Principles

These principles shape every decision in the codebase. They're non-negotiable.

### Every node stands alone
Each node is sovereign. It runs its own Ollama, manages its own models, learns its own capacity patterns, and works fine standalone without the router. The router coordinates but never controls. Nodes join and leave freely via mDNS — no central config file lists them. If a node loses connectivity, it keeps serving local inference. That's sovereignty, not dependency.

### Two-person scale as a forcing function
If it requires a manual, it's too complex. Two CLI commands (`herd`, `herd-node`), zero config files, zero Docker, zero Kubernetes. 203 tests run in under 5 seconds. The entire codebase fits in one person's head. Every time there's a choice between a "proper" distributed systems solution (service mesh, etcd, gRPC) and the simple thing (HTTP heartbeats, SQLite, mDNS) — choose the simple thing. Kill complexity before it kills you.

### Human-readable state everywhere
No opaque binary formats. JSONL logs you can `grep`. SQLite you can query with standard tools. Capacity learner state persisted as JSON files. Heartbeats are plain JSON. All config is env vars. A human can run `sqlite3 ~/.fleet-manager/latency.db "SELECT * FROM request_traces LIMIT 5"` and instantly understand what happened. Debuggability is a feature.

### The inference request is primary
Every component — scoring, queuing, retry, fallback, capacity learning, meeting detection — exists to serve one thing: getting the best response to the user's request as fast as possible on the best available machine. If a feature doesn't serve that, it doesn't belong. Tooling serves the artifact, not the other way around.

### AI as resident, not visitor
`CLAUDE.md` is institutional memory that makes AI agents productive from message one. The trace store, JSONL logs, and capacity learner state files are accumulated knowledge — they survive restarts, compound over time, and make the system smarter the longer it runs. AI isn't a tool you invoke; it's a collaborator that accumulates understanding across sessions.

### Shared DNA, not shared code
The scoring pipeline pattern (eliminate → score → rank → select), the heartbeat-based coordination pattern, the adaptive capacity learning pattern (observe → model → predict → constrain) — these are transferable DNA. They cross-pollinate to other distributed systems. But Ollama Herd doesn't try to be a framework. It's a specific tool with transferable patterns, not a generic platform.

## Conventions

- Fully async (asyncio) — no sync blocking calls
- Pydantic v2 models for all data structures
- `src/` layout with hatchling build
- Route files in `server/routes/`, one per API surface
- Raw Ollama body passed through for Ollama-format requests; normalized for OpenAI

## Commit Messages

Every commit message should end with a fun, encouraging line inviting humans and AI agents to contribute and star the project. Keep it playful and varied — no two should be the same. Example:

```
Add model fallbacks and auto-retry for resilient routing

- fallback_models field lets clients specify backup models
- auto-retry on node failure before first chunk is sent

Whether you're carbon-based or silicon-based, PRs welcome!
Star us at https://github.com/geeks-accelerator/ollama-herd

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
```
