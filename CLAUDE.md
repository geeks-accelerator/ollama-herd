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
uv run pytest                    # run all 445 tests (~5s)
uv run pytest tests/test_server/ # run server tests only
uv run pytest tests/test_models/ # run model tests only
uv run ruff check src/           # lint
uv run ruff format src/          # format
```

## Release to PyPI

**IMPORTANT: Never publish without running locally first.** AI agents: do NOT publish unless the user explicitly says "publish."

### Release checklist

1. Bump version in `pyproject.toml`
2. Update `CHANGELOG.md` (Keep a Changelog format)
3. `uv run pytest` — 0 failures
4. `uv run ruff check src/` — clean
5. Commit and push
6. Deploy locally — restart `herd` + `herd-node`, verify: `/fleet/status`, `/api/embed`, `/dashboard/api/health`, `/fleet/queue`
7. Soak several hours — check logs: `grep '"level":"ERROR"' ~/.fleet-manager/logs/herd.jsonl`
8. Only then: `uv build && uv publish --username __token__ --password "$(python3 -c "import configparser; c=configparser.ConfigParser(); c.read('$HOME/.pypirc'); print(c['pypi']['password'])")"`

**Package:** `ollama-herd` on [PyPI](https://pypi.org/project/ollama-herd/) | **Build:** hatchling | **Version:** `pyproject.toml`

### Local deployment

```bash
pkill -f "bin/herd" && sleep 2
uv sync && uv run herd &>/dev/null & disown
sleep 3 && uv run herd-node &>/dev/null & disown
```

### Gotchas

- **`launchctl setenv` is overridden by `~/.zshrc`** — update both shell profile AND launchctl for macOS env vars. On Linux: `sudo systemctl edit ollama`. On Windows: `[System.Environment]::SetEnvironmentVariable()`
- **`shutil.which()` can't find `uv tool` binaries** — `_which_extended()` in `collector.py` handles platform-aware fallback paths
- **Thinking models eat `num_predict` budgets** — router auto-inflates by 4× for known thinking models. Add new ones to `is_thinking_model()` in `model_knowledge.py`
- **Default context windows waste KV cache** — gpt-oss:120b allocates 131K ctx but p99 usage is ~5K tokens. Enable `FLEET_DYNAMIC_NUM_CTX=true` to auto-optimize. See `docs/plans/dynamic-num-ctx.md`

## Architecture

Single Python package (`fleet_manager`), cross-platform (macOS, Linux, Windows), two entry points:
- `herd` — FastAPI router (scoring + queues + dashboard + health + benchmarks)
- `herd-node` — node agent (heartbeats + metrics + capacity learning + Ollama management)

macOS-only features (gracefully disabled elsewhere): meeting detection, mflux/DiffusionKit image gen, MLX speech-to-text. Core routing works identically on all platforms.

### Key modules

| Module | Purpose |
|--------|---------|
| `server/scorer.py` | 7-signal scoring: thermal, memory, queue, wait, affinity, availability, context fit |
| `server/queue_manager.py` | Per `node:model` queues with dynamic concurrency + zombie reaper |
| `server/streaming.py` | httpx proxy to Ollama + NDJSON↔SSE + auto-retry + context protection + thinking model inflate |
| `server/health_engine.py` | 17 health checks (offline, degraded, memory, KV bloat, context waste, thrashing, timeouts, errors, retries, disconnects, streams, version, protection, zombies, connection failures) |
| `server/context_optimizer.py` | Dynamic num_ctx: analyzes token usage, auto-calculates optimal context, queues Ollama restarts via heartbeat commands |
| `server/benchmark_engine.py` | Benchmark core: fleet discovery, multimodal request gen (LLM + embed + image), report building |
| `server/benchmark_runner.py` | Server-side runner: smart mode (fill memory from disk/catalog), progress tracking, model type selection |
| `server/model_knowledge.py` | 40+ model catalog with benchmarks, RAM, categories (including VISION), thinking detection |
| `node/agent.py` | Main loop: mDNS discovery, heartbeat, Ollama auto-start/restart, LAN proxy, drain |
| `node/capacity_learner.py` | 168-slot weekly behavioral model, availability score, dynamic memory ceiling |

Routes: `server/routes/` — `openai_compat.py` (v1/), `ollama_compat.py` (api/), `fleet.py`, `heartbeat.py`, `dashboard.py`, `image_compat.py`, `transcription_compat.py`

### Request flow

Client → route handler → `score_with_fallbacks()` (eliminate → score 7 signals → select) → `QueueManager.enqueue()` → `StreamingProxy` (context protection + httpx stream) → response + trace to SQLite

### Configuration

All via env vars: `FLEET_` prefix (server), `FLEET_NODE_` prefix (node). See `docs/configuration-reference.md` for 47+ variables.

## Documentation

Key docs (Claude reads on demand — NOT loaded every turn):
- `docs/api-reference.md` — all endpoints with request/response schemas
- `docs/configuration-reference.md` — all 47+ env vars with tuning guidance
- `docs/operations-guide.md` — logging, traces, fallbacks, retry, drain, streaming, context protection
- `docs/fleet-manager-routing-engine.md` — 5-stage scoring pipeline deep dive
- `docs/adaptive-capacity.md` — capacity learner, meeting detection, app fingerprinting
- `docs/troubleshooting.md` — common issues, LAN debugging, operational gotchas
- `docs/openclaw-integration.md` — OpenClaw agent setup guide
- `docs/issues.md` — known issues (mark `FIXED` when resolved, never delete)
- `docs/observations.md` — operational insights (append new learnings, never delete)
- `docs/competitive-landscape.md` — 20+ competing projects analyzed
- `docs/plans/` — implementation plans for major features
- `docs/guides/` — image gen, thinking models, request tagging, agent setup, optimizing CLAUDE.md
- `docs/research/` — local fleet economics, mflux architecture
- `skills/` — 37 ClawHub skills. Strategy: `docs/skill-publishing-strategy.md`

## Design Principles

- **Node sovereignty** — each node works standalone; router coordinates, never controls
- **Two-person scale** — two commands, zero config files, zero Docker. Choose simple (HTTP, SQLite, mDNS) over "proper" (gRPC, etcd, K8s)
- **Human-readable state** — JSONL logs, SQLite traces, JSON config. `grep` and `sqlite3` are your debuggers
- **Inference request is primary** — every component serves one goal: best response, fastest, on best machine
- **AI as resident** — CLAUDE.md, traces, observations compound across sessions. AI accumulates understanding, not just executes tasks
- **Knowledge in committed files** — never `.claude/` memory. Use `CLAUDE.md`, `docs/issues.md`, `docs/observations.md`, `CHANGELOG.md`

## Issues & Observations

- `docs/issues.md` — bugs, performance, test gaps. Add with severity + proposed fix. Mark `FIXED` when resolved.
- `docs/observations.md` — patterns from operating the fleet. Add with date, evidence, insight. Never deleted.
- After significant changes: check if work produced a new observation or revealed a new issue. Append to the right file.

## Current State (as of 2026-04-14)

- **Version:** 0.5.2 (soaking locally, 0.4.1 published on PyPI)
- **Fleet:** Neons-Mac-Studio (512GB M3 Ultra), single node, `gpt-oss:120b` + `nomic-embed-text` + multi-model via dynamic num_ctx
- **Ollama settings:** `OLLAMA_NUM_PARALLEL=2`, `OLLAMA_KEEP_ALIVE=-1`, `OLLAMA_MAX_LOADED_MODELS=-1` (in `~/.zshrc`)
- **Skills:** 37 on ClawHub across `skills/`. When updating code: `grep -rn "445 tests\|17 checks" skills/`
- **Health:** 17 checks, zero errors. Monitor: `curl http://localhost:11435/dashboard/api/health`

## Conventions

- Fully async (asyncio) — no sync blocking calls
- Pydantic v2 models for all data structures
- `src/` layout with hatchling build
- Route files in `server/routes/`, one per API surface
- **Never store knowledge in `.claude/` memory files** — all in committed files
- **Never use git worktrees** — work directly on main branch

## Commit Messages

End every commit with a fun, varied line inviting contributions + star link. Example:

```
Add model fallbacks and auto-retry for resilient routing

Whether you're carbon-based or silicon-based, PRs welcome!
Star us at https://github.com/geeks-accelerator/ollama-herd

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
```
