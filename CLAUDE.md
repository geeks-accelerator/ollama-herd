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
uv run pytest                    # run all 145 tests (~0.8s)
uv run pytest tests/test_server/ # run server tests only
uv run pytest tests/test_models/ # run model tests only
uv run pytest -v                 # verbose output
uv run ruff check src/           # lint
uv run ruff format src/          # format
```

## Architecture

Single Python package (`fleet_manager`), two CLI entry points:
- `herd` — FastAPI server (router + API + scoring + queues + dashboard)
- `herd-node` — agent that runs on each device (heartbeats + metrics)

### Key modules

| Module | Purpose |
|--------|---------|
| `server/registry.py` | In-memory node state tracking via heartbeats |
| `server/scorer.py` | 5-signal scoring: thermal (hot/warm/cold), memory fit, queue depth, wait time, role affinity |
| `server/queue_manager.py` | Per `node:model` queues with async workers |
| `server/streaming.py` | httpx proxy to Ollama + format conversion (NDJSON <-> SSE) + auto-retry |
| `server/latency_store.py` | aiosqlite persistence at `~/.fleet-manager/latency.db` |
| `server/trace_store.py` | Per-request trace log + usage stats in SQLite |
| `server/routes/routing.py` | Shared scoring logic with model fallback support |
| `server/rebalancer.py` | Background queue rebalancer + pre-warm trigger |
| `server/routes/dashboard.py` | Real-time web dashboard at `/dashboard` with SSE updates |
| `node/agent.py` | Main loop: mDNS discovery, heartbeat, SIGTERM drain |
| `node/collector.py` | Assembles HeartbeatPayload from psutil + Ollama |
| `common/discovery.py` | AsyncZeroconf mDNS advertise + browse |
| `common/logging_config.py` | JSONL structured logging to `~/.fleet-manager/logs/` |

### Request flow

1. Client hits `/v1/chat/completions` or `/api/chat`
2. Route handler creates `InferenceRequest` (normalized)
3. `ScoringEngine.score_request()` — eliminates bad nodes, scores survivors
4. `QueueManager.enqueue()` — places in `node:model` queue, returns Future
5. Queue worker calls `StreamingProxy.stream_from_node()` — httpx stream to Ollama
6. Response streamed back (SSE for OpenAI, NDJSON for Ollama format)

### Configuration

All settings via env vars with `FLEET_` prefix (server) or `FLEET_NODE_` prefix (node). See `models/config.py` for all options.

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
