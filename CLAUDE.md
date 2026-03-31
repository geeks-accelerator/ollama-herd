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
uv sync --extra dev              # install test deps (first time only)
uv run pytest                    # run all 359 tests (~5s)
uv run pytest tests/test_server/ # run server tests only
uv run pytest tests/test_models/ # run model tests only
uv run pytest -v                 # verbose output
uv run ruff check src/           # lint
uv run ruff format src/          # format
./scripts/health.sh              # full project health check
./scripts/patch-diffusionkit-macos26.sh  # fix DiffusionKit on macOS 26+
```

## Architecture

Single Python package (`fleet_manager`), two CLI entry points:
- `herd` — FastAPI server (router + API + scoring + queues + dashboard)
- `herd-node` — agent that runs on each device (heartbeats + metrics + capacity learning)

### Key modules

| Module | Purpose |
|--------|---------|
| `server/registry.py` | In-memory node state tracking via heartbeats |
| `server/scorer.py` | 7-signal scoring: thermal, memory fit, queue depth, wait time, role affinity, availability trend, context fit |
| `server/queue_manager.py` | Per `node:model` queues with dynamic concurrent workers + stale in-flight reaper |
| `server/streaming.py` | httpx proxy to Ollama + format conversion (NDJSON ↔ SSE) + auto-retry + context-size protection |
| `server/latency_store.py` | aiosqlite persistence at `~/.fleet-manager/latency.db` |
| `server/trace_store.py` | Per-request trace log + usage stats + benchmark results + timeout detection in SQLite |
| `server/health_engine.py` | Fleet health analysis: 11 checks (offline, degraded, memory pressure, underutilized, VRAM fallbacks, thrashing, timeouts, error rates, retries, version mismatch, context protection, zombie reaper) |
| `server/model_knowledge.py` | Curated catalog of 30+ Ollama models with benchmarks, RAM requirements, and category classifications |
| `server/model_recommender.py` | Analyzes fleet hardware + usage patterns to recommend optimal model mix per node |
| `server/routes/routing.py` | Shared scoring logic with model fallback + holding queue + auto-pull + tag extraction |
| `server/rebalancer.py` | Background queue rebalancer + pre-warm trigger |
| `server/routes/openai_compat.py` | `/v1/chat/completions`, `/v1/models` |
| `server/routes/ollama_compat.py` | `/api/chat`, `/api/generate`, `/api/tags`, `/api/ps` |
| `server/routes/fleet.py` | `/fleet/status` — full fleet state |
| `server/routes/heartbeat.py` | `/heartbeat` — node agent heartbeat receiver |
| `server/routes/dashboard.py` | Real-time web dashboard at `/dashboard` with SSE updates, benchmarks, health, model recommendations, model management, and settings (runtime toggles + node versions) |
| `node/agent.py` | Main loop: mDNS discovery, heartbeat, Ollama auto-start, LAN proxy, SIGTERM drain |
| `node/collector.py` | Assembles HeartbeatPayload from psutil + Ollama, rewrites localhost to LAN IP |
| `node/ollama_proxy.py` | TCP reverse proxy: bridges LAN IP → localhost Ollama (auto-started) |
| `node/capacity_learner.py` | 168-slot behavioral model, availability score, dynamic memory ceiling |
| `node/meeting_detector.py` | macOS camera/microphone detection → hard pause |
| `node/app_fingerprint.py` | Resource signature classification (idle/light/moderate/heavy/intensive) |
| `node/image_server.py` | FastAPI wrapper for mflux CLI — `/api/generate-image` on port 11436 |
| `server/routes/image_compat.py` | `/api/generate-image` — routes mflux requests to best node via queue |
| `server/routes/transcription_compat.py` | `/api/transcribe` — routes speech-to-text requests to best node via Qwen3-ASR |
| `common/discovery.py` | AsyncZeroconf mDNS advertise + browse |
| `common/logging_config.py` | JSONL structured logging to `~/.fleet-manager/logs/` |

### Request flow

1. Client hits `/v1/chat/completions` or `/api/chat`
2. Route handler creates `InferenceRequest` (model names normalized with `:latest` tag)
3. `score_with_fallbacks()` — tries primary model, then fallbacks with holding queue
4. `ScoringEngine.score_request()` — eliminates bad nodes, scores survivors on 7 signals
5. `QueueManager.enqueue()` — places in `node:model` queue, returns Future
6. Queue worker calls `StreamingProxy.make_process_fn()` — context protection strips/upgrades `num_ctx`, then httpx stream to Ollama with auto-retry
7. Response streamed back (SSE for OpenAI, NDJSON for Ollama format)
8. Trace recorded to SQLite, latency table updated

### Configuration

All settings via env vars with `FLEET_` prefix (server) or `FLEET_NODE_` prefix (node). See [`docs/configuration-reference.md`](docs/configuration-reference.md) for the complete 31+ variable reference.

## Documentation

| Document | Description |
|----------|-------------|
| [`docs/api-reference.md`](docs/api-reference.md) | All endpoints with request/response schemas |
| [`docs/configuration-reference.md`](docs/configuration-reference.md) | All 30+ env vars with tuning guidance |
| [`docs/operations-guide.md`](docs/operations-guide.md) | Logging, traces, fallbacks, retry, drain, pre-warm, streaming, context protection |
| [`docs/adaptive-capacity.md`](docs/adaptive-capacity.md) | Capacity learner, meeting detection, app fingerprinting |
| [`docs/fleet-manager-routing-engine.md`](docs/fleet-manager-routing-engine.md) | 5-stage scoring pipeline deep dive |
| [`docs/openclaw-integration.md`](docs/openclaw-integration.md) | Setup guide for OpenClaw agents |
| [`docs/request-tagging.md`](docs/request-tagging.md) | Per-app analytics, tagging strategies, competitive landscape |
| [`docs/troubleshooting.md`](docs/troubleshooting.md) | Common issues, LAN debugging, context protection, operational gotchas |
| [`docs/architecture-decisions.md`](docs/architecture-decisions.md) | Port selection, design trade-offs, rationale |
| [`docs/issues.md`](docs/issues.md) | Known issues, improvements, test coverage gaps |
| [`docs/observations.md`](docs/observations.md) | Patterns and insights extracted from operating the fleet |
| [`docs/competitive-landscape.md`](docs/competitive-landscape.md) | 20+ competing projects analyzed, feature comparison matrix |
| [`docs/skill-publishing-strategy.md`](docs/skill-publishing-strategy.md) | Multi-skill publishing approach for ClawHub marketplace |
| [`docs/skill-marketplace-analysis.md`](docs/skill-marketplace-analysis.md) | ClawHub competitive analysis, keyword gaps, tag strategy |
| [`docs/guides/image-generation.md`](docs/guides/image-generation.md) | Image generation routing setup, API reference, integration examples |
| [`docs/guides/integrate-z-image-turbo.md`](docs/guides/integrate-z-image-turbo.md) | Z-Image-Turbo integration guide for other projects |
| [`docs/guides/request-tagging-analytics.md`](docs/guides/request-tagging-analytics.md) | Request tagging for per-app analytics and insights |
| [`docs/guides/agent-setup-guide.md`](docs/guides/agent-setup-guide.md) | Complete agent setup for all 4 model types (LLM, image, STT, embeddings) |
| [`docs/research/local-fleet-economics.md`](docs/research/local-fleet-economics.md) | Economics of local AI fleets vs cloud APIs |
| [`docs/research/mflux-image-generation.md`](docs/research/mflux-image-generation.md) | mflux setup, architecture, why it bypasses/integrates with Herd |

## Design Principles

These principles shape every decision in the codebase. They're non-negotiable.

### Every node stands alone
Each node is sovereign. It runs its own Ollama, manages its own models, learns its own capacity patterns, and works fine standalone without the router. The router coordinates but never controls. Nodes join and leave freely via mDNS — no central config file lists them. If a node loses connectivity, it keeps serving local inference. That's sovereignty, not dependency.

### Two-person scale as a forcing function
If it requires a manual, it's too complex. Two CLI commands (`herd`, `herd-node`), zero config files, zero Docker, zero Kubernetes. 359 tests run in under 5 seconds. The entire codebase fits in one person's head. Every time there's a choice between a "proper" distributed systems solution (service mesh, etcd, gRPC) and the simple thing (HTTP heartbeats, SQLite, mDNS) — choose the simple thing. Kill complexity before it kills you.

### Human-readable state everywhere
No opaque binary formats. JSONL logs you can `grep`. SQLite you can query with standard tools. Capacity learner state persisted as JSON files. Heartbeats are plain JSON. All config is env vars. A human can run `sqlite3 ~/.fleet-manager/latency.db "SELECT * FROM request_traces LIMIT 5"` and instantly understand what happened. Debuggability is a feature.

### The inference request is primary
Every component — scoring, queuing, retry, fallback, capacity learning, meeting detection — exists to serve one thing: getting the best response to the user's request as fast as possible on the best available machine. If a feature doesn't serve that, it doesn't belong. Tooling serves the artifact, not the other way around.

### AI as resident, not visitor
`CLAUDE.md` is institutional memory that makes AI agents productive from message one. The trace store, JSONL logs, and capacity learner state files are accumulated knowledge — they survive restarts, compound over time, and make the system smarter the longer it runs. [`docs/observations.md`](docs/observations.md) closes the loop: raw data → extracted patterns → transferable insights. [`docs/issues.md`](docs/issues.md) tracks what's broken. Observations track what we've learned. AI isn't a tool you invoke; it's a collaborator that accumulates understanding across sessions.

### Shared DNA, not shared code
The scoring pipeline pattern (eliminate → score → rank → select), the heartbeat-based coordination pattern, the adaptive capacity learning pattern (observe → model → predict → constrain) — these are transferable DNA. They cross-pollinate to other distributed systems. But Ollama Herd doesn't try to be a framework. It's a specific tool with transferable patterns, not a generic platform.

## Issues & Observations

Two living docs track the project's accumulated knowledge:

- **[`docs/issues.md`](docs/issues.md)** — What's broken or needs improvement. Add issues when you find bugs, performance problems, test gaps, or code quality concerns. Each issue has a file reference, severity, and proposed fix. Mark issues `FIXED` when resolved — don't delete them.

- **[`docs/observations.md`](docs/observations.md)** — What we've learned from operating the fleet. Add observations when you notice patterns in the trace data, discover why something behaves unexpectedly, or extract a transferable insight. Each observation has a date, evidence (query output, log lines, metrics), and the extracted insight. Observations are never deleted — they compound.

**When to write an issue vs. an observation:**
- Something is wrong and needs fixing → **issue**
- Something worked (or failed) and we learned why → **observation**
- A workaround reveals a deeper pattern → **both** (issue for the fix, observation for the insight)

**AI agents:** After completing a significant code change, check if the work produced a new observation (a pattern, a surprise, a lesson). If it did, append it to `docs/observations.md`. After debugging a problem, check if it revealed a new issue. If it did, append it to `docs/issues.md`. This is how the project accumulates intelligence across sessions.

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
