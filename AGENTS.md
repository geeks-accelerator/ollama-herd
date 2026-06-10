# AGENTS.md — Ollama Herd

> This file is the onboarding doc for AI coding agents. If you're a human, welcome — the same applies to you.

Ollama Herd is a smart inference router that turns a collection of devices running Ollama into a single local AI fleet. It routes LLM, embedding, image generation, and speech-to-text requests to the best available node using a 7-signal scoring engine, with zero config files and zero cloud dependency. The design philosophy is **node sovereignty** (each node works standalone; the router coordinates but never controls), **two-person scale** (two commands, no Docker, no Kubernetes), and **human-readable state** (JSONL logs, SQLite traces, JSON config — `grep` and `sqlite3` are your debuggers). v0.7.0 ships a native fastembed text embedding server that routes `nomic-embed-text` out of Ollama's inference queue entirely, plus a "Node Models" dashboard showing cards for all backends (Ollama, MLX, fastembed, vision embedding).

---

## Setup

**Requirements:** Python 3.11+, [uv](https://github.com/astral-sh/uv)

```bash
git clone https://github.com/geeks-accelerator/ollama-herd.git
cd ollama-herd
uv sync --all-extras          # core + all optional deps (recommended for development)
```

**Optional extras** (all included in `--all-extras`):

| Extra | What it adds | Command |
|-------|-------------|---------|
| `dev` | pytest, pytest-asyncio | `uv sync --extra dev` |
| `embedding` | onnxruntime, Pillow, numpy, huggingface-hub, fastembed | `uv sync --extra embedding` |

**macOS-only system deps** (not required for core routing):
- `uv tool install mflux` — FLUX image generation
- `uv tool install diffusionkit` — Stable Diffusion 3/3.5
- `pip install 'mlx-qwen3-asr[serve]'` — speech-to-text

---

## Running locally

```bash
uv run herd                                        # router on :11435
uv run herd-node                                   # node agent (mDNS auto-discovers router)
uv run herd-node --router-url http://localhost:11435  # explicit URL
```

**Dashboard:** `http://localhost:11435/dashboard`

**Config file:** `~/.fleet-manager/env` — auto-loaded at startup by both processes. All `FLEET_` vars go here. Shell env wins if both are set. Template: `docs/examples/fleet-env.example`.

**Logs:** `~/.fleet-manager/logs/herd.jsonl` (router) and `herd-node.jsonl` (node agent). Separated since 0.6.2 — scan both. Correct grep pattern: `'"level": "ERROR"'` (space after colon — `json.dumps` emits whitespace).

**Trace DB:** `~/.fleet-manager/latency.db` (SQLite, WAL mode).

**Port map:**

| Port | Service |
|------|---------|
| 11434 | Ollama (not ours) |
| 11435 | herd router |
| 11436 | image generation (mflux / DiffusionKit) |
| 11437 | speech-to-text |
| 11438 | vision embedding (ONNX) |
| 11439 | text embedding (fastembed) |
| 11440+ | MLX servers (one per `FLEET_NODE_MLX_SERVERS` entry) |

---

## Testing

```bash
uv sync --extra dev
uv run pytest                          # 1006 tests, ~40s
uv run pytest tests/test_server/       # server tests only
uv run pytest tests/test_node/         # node agent tests only
uv run pytest -k "test_scorer"         # single test module
uv run pytest --tb=short -q            # compact output
```

**No real Ollama required.** All tests are mocked. `tests/conftest.py` sets up in-memory SQLite, fake registries, and mock httpx transports — the full test suite runs offline with no network calls.

**Test layout:**

```
tests/
  conftest.py              shared fixtures (app, registry, trace store, mock httpx)
  test_server/             router: scoring, queues, streaming, health, routing, trace store
  test_node/               node agent: heartbeat, MLX supervisor, capacity, platform
  test_models/             Pydantic model validation
  test_common/             env file loading, shared utilities
```

The two known warnings (`coroutine '_dummy_process' was never awaited`) are pre-existing and do not indicate test failures.

---

## Code style

- **Ruff** for lint and format (`uv run ruff check src/` + `uv run ruff format src/`). Config in `pyproject.toml`.
- **Fully async** — no sync blocking calls anywhere. Every route handler, background task, and DB call must be `async`.
- **Pydantic v2** models for all data structures. No raw dicts crossing module boundaries.
- **`src/` layout** — package is `src/fleet_manager/`. Build backend is hatchling.
- **Line length:** 100 characters.
- No pre-commit hooks configured — run ruff manually before committing.

---

## Project layout

```
src/fleet_manager/
  server/
    app.py                 FastAPI app factory, lifespan, middleware
    scorer.py              7-signal routing scorer (thermal, memory, queue, wait, affinity, availability, context fit)
    queue_manager.py       per-node:model queues, dynamic concurrency, zombie reaper
    streaming.py           httpx proxy → Ollama, NDJSON↔SSE, auto-retry, context protection
    health_engine.py       36 automated health checks — all wired to /dashboard/api/health
    registry.py            in-memory node registry, heartbeat ingestion, node state
    context_optimizer.py   dynamic num_ctx: analyzes token usage, queues Ollama restarts
    trace_store.py         SQLite WAL trace storage, dual-connection (read/write separation)
    mlx_proxy.py           server-side proxy for mlx: prefixed models → mlx_lm.server
    model_knowledge.py     40+ model catalog (RAM, category, thinking detection)
    benchmark_engine.py    fleet-wide benchmark: LLM + embed + image + STT
    routes/
      openai_compat.py     /v1/* (OpenAI format)
      ollama_compat.py     /api/* (Ollama format) — embed dispatch lives here
      fleet.py             /fleet/status, /fleet/queue
      dashboard.py         /dashboard/* + SSE stream for live Node Models cards
      embedding_compat.py  vision embedding proxy → :11438
      text_embedding_compat.py  text embedding proxy → :11439
      image_compat.py      /api/generate-image routing
      heartbeat.py         POST /heartbeat ingestion
  node/
    agent.py               main loop: mDNS, heartbeat, server lifecycle, drain
    collector.py           system metrics collection, model detection
    capacity_learner.py    168-slot weekly behavioral model
    embedding_server.py    FastAPI vision embedding server (:11438, ONNX)
    embedding_models.py    vision embedding model registry (DINOv2, SigLIP, CLIP)
    text_embedding_server.py   FastAPI text embedding server (:11439, fastembed)
    text_embedding_models.py   text embedding model registry (nomic-embed-text)
    mlx_supervisor.py      subprocess lifecycle for N mlx_lm.server processes
    mlx_client.py          polls mlx_lm.server /v1/models, merges into heartbeat
  models/
    node.py                HeartbeatPayload, NodeInfo, all shared data models
    config.py              Settings (FLEET_ prefix) and NodeSettings (FLEET_NODE_ prefix)
  common/
    env_file.py            ~/.fleet-manager/env auto-loader
    logging_config.py      JSONLFormatter (emits "level": "ERROR" with space)
    system_metrics.py      cross-platform CPU/memory/thermal probe
```

---

## Key docs

| Document | When to read it |
|----------|----------------|
| `CLAUDE.md` | Deep onboarding: full module table, request flow, all gotchas, commit conventions, current fleet state |
| `docs/api-reference.md` | Every endpoint, request/response schema, heartbeat field docs |
| `docs/configuration-reference.md` | All 47+ `FLEET_` env vars with tuning guidance |
| `docs/fleet-manager-routing-engine.md` | 5-stage scoring pipeline deep dive |
| `docs/operations-guide.md` | Logging, traces, fallbacks, retry, drain, streaming, context protection |
| `docs/troubleshooting.md` | Common failure modes with root causes and fixes |
| `docs/observations.md` | Operational learnings from running the fleet — append new findings, never delete |
| `docs/issues.md` | Bug and performance issue tracking — mark `FIXED` when resolved, never delete |
| `docs/guides/claude-code-integration.md` | Pointing Claude Code CLI at the herd via `ANTHROPIC_BASE_URL` |
| `docs/guides/mlx-setup.md` | MLX server setup, multi-server config, speculative decoding |
| `docs/research/` | Deep-dives: mlx-lm stability, local fleet economics |

---

## What NOT to do

**Don't break cross-platform support.** macOS-only features (meeting detection, mflux, MLX speech-to-text) are gated with platform checks and degrade gracefully. Core routing must work identically on macOS, Linux, and Windows. Never add a hard import of a macOS-only library to a code path that runs on all platforms.

**Don't break the three API surfaces.** Ollama format (`/api/*`), OpenAI format (`/v1/*`), and Anthropic format (`/v1/messages`) are the contract. Adding a field is fine; removing or renaming a field in a response is a breaking change.

**Don't add tests that require a live Ollama instance.** All tests mock the network. A test that fails in CI because Ollama isn't running will always be rejected.

**Don't introduce sync blocking calls.** `requests`, `time.sleep`, synchronous file I/O in a hot path — all wrong. Use `httpx.AsyncClient`, `asyncio.sleep`, `aiofiles`. The whole stack is asyncio; one blocking call blocks everything.

**Don't store project knowledge in `~/.claude/` memory files.** Multiple agents work across different machines and sessions. Memory files are not portable. Anything that other agents need to know goes in `CLAUDE.md` (rules) or `docs/` (detail). The project has `docs/issues.md` and `docs/observations.md` as the living operational record — use them.

**Don't use git worktrees.** Work directly on main.

**Don't touch the port map.** The port assignments (11435–11441) are baked into documentation, health checks, and user configs. Don't reassign them or add new services on existing ports.

**Don't silently swallow errors.** `record_trace()` must be called for all request outcomes including failures. Silent failures are observability dark matter — the May 2026 incident (40,000 invisible trace write failures over 5 days) and the June 2026 embed timeout storm (202 errors undetected for 1.5h) both stemmed from missing trace calls.

**Don't publish without the checklist.** `CLAUDE.md` § "Release checklist" is non-negotiable — tests, lint, local deploy, local soak, Homebrew end-to-end install test. Skipping the brew install test is how the 0.5.x formula stayed broken for months.

---

## Commit conventions

First line: **what** changed. Body: **why** — motivation, what it enables.

End every commit with a fun line inviting contributions + star link, and a `Co-Authored-By:` footer if an AI agent made the change:

```
Add native text embedding server (fastembed, port 11439)

Routes nomic-embed-text out of Ollama entirely — no more contention
with LLM inference slots under OLLAMA_NUM_PARALLEL=2.

Whether you're carbon-based or silicon-based, PRs welcome!
Star us at https://github.com/geeks-accelerator/ollama-herd

Reinforced: structural fixes beat retries — a dedicated server per
concern eliminates the problem class.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
```

See `CONTRIBUTING.md` for the full PR process. See `CLAUDE.md` for the deep architectural context this file intentionally omits.
