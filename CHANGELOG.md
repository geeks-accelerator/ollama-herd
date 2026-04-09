# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.0] - 2026-04-08

### Added

- **`/api/pull` endpoint** — Ollama-compatible model pulling through the router. Auto-selects best node by available memory, streams NDJSON progress, supports `node_id` targeting. Returns install instructions for non-Ollama models (mflux, DiffusionKit, MLX)
- **Smart benchmark system** — run benchmarks from the dashboard with two modes:
  - **Default**: benchmark currently loaded models
  - **Smart**: fill available memory with recommended models (prefers on-disk, then downloads), then benchmark everything
  - Dashboard UI: run button, mode selector, duration picker, model type checkboxes (LLM/Embeddings/Image gen), dual progress bars during pull phase, gradient color bar, elapsed in m:ss format
  - Real-time progress polling with live tok/s counter
  - Multimodal: benchmarks LLM chat, embeddings, and image generation simultaneously
- **Dynamic num_ctx management** (Issue #21) — 3-phase system to eliminate KV cache waste:
  - **Phase 1 (Observe)**: `GET /dashboard/api/context-usage` — per-model p50/p75/p95/p99 of total tokens (prompt+completion), 24h rolling max, utilization %, recommended ctx, savings estimate
  - **Phase 2 (Control)**: `FLEET_DYNAMIC_NUM_CTX` toggle + per-model `num_ctx_overrides` — router injects optimal num_ctx on cold loads, configurable via dashboard settings API
  - **Phase 3 (Auto-adjust)**: `ContextOptimizer` background task auto-calculates from 7-day traces, auto-initializes overrides on startup, queues Ollama restarts via heartbeat command channel
- **4 new benchmark charts** — Model Throughput (horizontal bar), Model Latency (grouped bar: latency vs TTFT), Model Performance Over Time (multi-line across runs), Node Utilization (CPU/MEM grouped bar)
- **Context waste health check** — WARNING when allocated context > 4× actual p99 total usage, with specific per-model recommended num_ctx values in the fix message
- **Heartbeat command channel** — router can send commands (e.g., `restart_ollama` with env overrides) to nodes via heartbeat response
- **Node agent Ollama restart** — `_restart_ollama()` processes commands from router, applies env overrides, gracefully restarts
- `POST /dashboard/api/benchmarks/start` — start benchmarks from dashboard
- `GET /dashboard/api/benchmarks/progress` — real-time benchmark progress
- `POST /dashboard/api/benchmarks/cancel` — cancel running benchmarks
- `GET /dashboard/api/context-usage` — per-model context utilization analysis
- 16 health checks total (up from 15 in 0.4.1)

### Fixed

- **`_request_tokens` encapsulation** (#4) — added `pop_token_counts()` and `pop_request_meta()` public methods on `StreamingProxy`, replaced all direct private dict access in route handlers
- **`asyncio.ensure_future` deprecated** (#5) — replaced with `asyncio.create_task()` in discovery.py
- **KV cache bloat fix message** (#16) — added Windows instructions alongside macOS/Linux
- **Benchmark chart x-axis** — shows date + time ("Apr 8 2:30 PM") instead of just date, so same-day runs are distinguishable
- **Smart benchmark skips cloud models** — filters out `:cloud` suffix models (API proxies that don't load locally)
- **Smart benchmark skips embedding/image models** for LLM category coverage — `nomic-embed-text` no longer blocks loading a general-purpose LLM
- **Context recommendation uses total tokens** — was using prompt-only p99 (caused truncation at 8K), now uses p99 of prompt+completion with 50% headroom and 24h rolling max floor

### Changed

- `benchmark_engine.py` extracted from `scripts/benchmark.py` — shared core logic between CLI and server-side runner
- `scripts/benchmark.py` is now a thin CLI wrapper importing from `benchmark_engine`
- Dashboard settings API accepts `dynamic_num_ctx`, `num_ctx_auto_calculate`, and `num_ctx_overrides`
- Settings GET response includes `context` section with all num_ctx state
- `StreamingProxy.pull_model()` accepts optional `progress_cb` callback for download progress tracking
- Benchmark `per_model_results` includes `model_type` field (llm/embed/image)

## [0.4.1] - 2026-04-02

### Added

- **Thinking model support** — auto-detects thinking models (gpt-oss, deepseek-r1, qwq, phi-4-reasoning) and inflates `num_predict` by 4× (configurable via `FLEET_THINKING_OVERHEAD`) to prevent empty responses where reasoning consumes the entire token budget
- **Thinking-aware response headers** — `X-Thinking-Tokens`, `X-Output-Tokens`, `X-Budget-Used`, `X-Done-Reason` on non-streaming responses
- **Queue depth API** — `GET /fleet/queue` for client-side backoff decisions with `estimated_wait_ms`
- **KV cache bloat health check** — detects when `OLLAMA_NUM_PARALLEL` is too high by comparing VRAM vs estimated weights. Surfaces actionable fix
- **Stream reliability health checks** — "Client Disconnects" and "Incomplete Streams" dashboard cards with per-model breakdowns
- **Embedding model badges** — purple EMBED badges on Fleet Overview and Settings
- **Thinking models guide** — `docs/guides/thinking-models.md`
- 15 health checks total (up from 11 in 0.4.0)

### Fixed

- **Embeddings proxy routed to `/api/chat`** — embed requests went through the chat streaming pipeline. Now proxies directly to Ollama's `/api/embed` via the managed HTTP client with 600s timeout
- **Image/STT binary detection** — `shutil.which()` couldn't find mflux/DiffusionKit installed via `uv tool` because `~/.local/bin` wasn't in PATH. Added `_which_extended()` that checks common tool install locations
- **Client disconnects recorded as "completed"** — `GeneratorExit` now records as `client_disconnected`
- **Incomplete streams recorded as "completed"** — missing `done: true` now detected and recorded as `incomplete`
- **Error rate queries undercounting** — now counts all non-success statuses
- **LatencyStore unbounded memory** — capped to last 500 observations
- **N+1 query on cache refresh** — single SQL query with window functions
- **O(n) in-flight tracking** — dict keyed by request_id, all O(1)
- **Ollama non-streaming missing headers** — changed to explicit JSONResponse

### Changed

- `image_generation` and `transcription` default to `true` (was `false` — caused silent 503s after every restart)
- SSE stream and fleet/status include `embed_models` per node
- Queue EMBED badge color changed to purple

## [0.4.0] - 2026-04-02

### Added

- **Embeddings proxy** — `/api/embed` and `/api/embeddings` endpoints route embedding requests to the best available node via Ollama's native `/api/embed`. Supports both `input` (single or batch) and `prompt` (legacy) fields
- **OpenAI-compatible image generation** — `/v1/images/generations` wraps the fleet's image generation in OpenAI's standard API format. Works with the OpenAI SDK (`client.images.generate()`)
- **Image model discovery** — `/api/image-models` lists all image models across the fleet with backend type and which nodes have them. Image models also now appear in `/api/tags` and `/v1/models` responses
- **Request tagging for image and STT** — `metadata.tags` and `X-Herd-Tags` header now work on `/api/generate-image` and `/api/transcribe`. All four model types appear in the Apps dashboard tab
- **DeepSeek-V3 in model catalog** — 3 variants: `deepseek-v3:7b`, `deepseek-v3:32b`, `deepseek-v3:671b` (671B MoE, 404GB)
- **KV cache bloat health check** — detects when OLLAMA_NUM_PARALLEL is too high by comparing loaded model VRAM against estimated weight sizes. Surfaces actionable fix with exact commands
- **Stream reliability health checks** — "Client Disconnects" and "Incomplete Streams" cards on the Health dashboard with per-model breakdowns and active/resolved state
- **Stream reliability vitals** — `client_disconnects_24h` and `incomplete_streams_24h` counters on the Health page
- **Thinking model support** — auto-detects thinking models (gpt-oss, deepseek-r1, qwq, phi-4-reasoning) and inflates `num_predict` by 4× (configurable via `FLEET_THINKING_OVERHEAD`) with 1024 minimum to prevent empty responses where reasoning consumes the entire token budget
- **Thinking-aware response headers** — `X-Thinking-Tokens`, `X-Output-Tokens`, `X-Budget-Used`, `X-Done-Reason` on non-streaming responses for instant debugging of thinking model behavior
- **Queue depth API** — `GET /fleet/queue` returns lightweight queue depths, estimated wait time, and per-queue concurrency for client-side backoff decisions
- **Embedding model badges** — purple EMBED badges on Fleet Overview node cards and Settings page for models like nomic-embed-text
- **Expanded README** — comprehensive usage docs for all 4 model types with SDK examples, model comparison tables, discovery endpoints, and batch examples
- **Thinking models guide** — `docs/guides/thinking-models.md` with recommended settings, client-side tips, and debugging patterns
- **PyPI release process** documented in CLAUDE.md (build commands, credential location, changelog expectations)
- 32 new tests (444 total)

### Fixed

- **Client disconnects recorded as "completed"** — `GeneratorExit` (HTTP timeout, connection drop) was caught but silently marked successful. Now records as `client_disconnected` and increments `failed_count`
- **Incomplete streams recorded as "completed"** — when Ollama drops the connection without `done: true` (process death, OOM, TCP drop), the request was marked completed. Now detects missing `done: true` and records as `incomplete`
- **Embeddings proxy routing** — embed requests were going through `/api/chat` instead of Ollama's `/api/embed`. Now proxies directly to the correct Ollama endpoint via the managed HTTP client
- **Error rate queries undercounting** — `get_error_rates_24h` and `get_overall_stats_24h` only counted `status = 'failed'`, missing `client_disconnected` and `incomplete`. Now counts all non-success statuses
- **LatencyStore unbounded memory** — `get_percentile()` loaded all history into memory. Now capped to last 500 observations per (node, model) pair
- **N+1 query on cache refresh** — startup queried each (node, model) pair individually. Replaced with single SQL query using `ROW_NUMBER()` + `PERCENT_RANK()` window functions
- **O(n) in-flight tracking** — queue `in_flight` changed from list to dict keyed by request_id. All operations now O(1)

### Changed

- `/api/tags` response includes mflux, DiffusionKit, and Ollama native image models alongside LLM models
- `/v1/models` response includes image models with `type: "image"` in metadata
- SSE stream and `/fleet/status` include `embed_models` per node
- Queue EMBED type badge color changed to purple for consistency
- Embed proxy timeout increased to 600s to handle first-time model loading
- Health check count: 11 → 15 (added KV cache bloat, client disconnects, incomplete streams, stream reliability)

## [0.3.0] - 2026-03-30

### Added

- **Expanded image generation** — three backends through one endpoint
  - DiffusionKit backend: Stable Diffusion 3 Medium and SD 3.5 Large via MLX-native `diffusionkit-cli`
  - Ollama native backend: `x/z-image-turbo` and `x/flux2-klein` via standard `/api/generate`
  - mflux preferred over Ollama native to prevent LLM eviction from VRAM
  - 8 image models total across 3 backends
- **IMAGE model category** in model knowledge catalog with `is_image_model()` helper
- **DiffusionKit macOS 26 patch script** (`scripts/patch-diffusionkit-macos26.sh`)
- 19 ClawHub skills (5 new: `llama-llama3`, `mistral-codestral`, `phi-phi4`, `private-ai`, `local-coding`)
- 16 #1 keyword rankings on ClawHub
- ClawHub SEO optimization guide (`docs/guides/optimizing-skills-for-clawhub.md`)
- 34 new tests (412 total)

### Changed

- Queue type badge uses `classify_model()` from model knowledge instead of string heuristic — DiffusionKit models now correctly show `[IMAGE]` badge
- `/api/generate` detects Ollama native image models, forces non-streaming, decodes base64 PNG response
- `/api/generate-image` accepts Ollama native models alongside mflux, falls through to Ollama pipeline when needed
- Node collector detects DiffusionKit binary and reports SD3 models in heartbeat
- Image server generalized CLI builder handles both mflux and DiffusionKit flag differences

### Fixed

- mflux preferred over Ollama native to prevent LLM eviction from VRAM (was causing 500 errors on text requests)

## [0.2.0] - 2026-03-30

### Added

- **Multimodal routing** — 4 model types through one fleet
  - Image generation via mflux (`z-image-turbo`, `flux-dev`, `flux-schnell`)
  - Speech-to-text via Qwen3-ASR
  - Embeddings via Ollama (nomic-embed-text, mxbai-embed)
  - `request_type` field on InferenceRequest (text, image, stt, embed)
- **Dashboard multimodal badges** — `[TEXT]`, `[IMAGE]`, `[STT]`, `[EMBED]` on queue cards
- **Node capability badges** — `IMG z-image-turbo`, `STT qwen3-asr` on node cards
- **Transcription health check** and `/dashboard/api/transcription-stats` endpoint
- **Fleet status** includes image and transcription data per node
- **SSE events** include `image_models` and `stt_models` for real-time updates
- **Settings page** shows Image Models and STT Models rows with ports per node
- **Health vitals** grid adds Images (24h) and STT (24h) counters
- Image generation event tracking for health monitoring (last 200 events)
- 7 ClawHub skills published (ollama-herd, local-llm-router, ollama-load-balancer, gpu-cluster-manager, ollama-manager, ai-devops-toolkit, distributed-inference)
- Context protection for streaming requests
- VRAM-aware model fallback
- Request tagging with per-app analytics dashboard
- Model recommendations engine based on hardware capabilities
- Settings dashboard page with runtime toggles

### Changed

- Scoring engine updated with context fit signal (7th signal)
- Dashboard rewritten with 8 tabs (overview, trends, insights, apps, benchmarks, health, recommendations, settings)

## [0.1.0] - 2025-03-10

### Added

- Smart inference router with 7-signal scoring engine (thermal, memory fit, queue depth, wait time, role affinity, availability trend, context fit)
- OpenAI-compatible API (`/v1/chat/completions`, `/v1/models`)
- Ollama-compatible API (`/api/chat`, `/api/generate`, `/api/tags`, `/api/ps`)
- Zero-config node discovery via mDNS
- Node agent with heartbeat-based health reporting
- Per-node:model queues with dynamic concurrent workers
- Streaming proxy with auto-retry on node failure
- Model fallback chains for resilient routing
- Holding queue for requests when no nodes are immediately available
- Auto-pull for missing models
- Real-time web dashboard with SSE updates
- Benchmark tab for model performance comparison
- Capacity learner with 168-slot weekly behavioral model
- Meeting detection (macOS camera/microphone) for automatic pause
- App fingerprinting for resource-aware scheduling
- SQLite-backed latency store and request trace log
- Fleet status API (`/fleet/status`)
- JSONL structured logging
- LAN proxy for bridging localhost-bound Ollama to network
- Graceful drain on SIGTERM
- 212 tests with full async coverage

[0.4.1]: https://github.com/geeks-accelerator/ollama-herd/releases/tag/v0.4.1
[0.4.0]: https://github.com/geeks-accelerator/ollama-herd/releases/tag/v0.4.0
[0.3.0]: https://github.com/geeks-accelerator/ollama-herd/releases/tag/v0.3.0
[0.2.0]: https://github.com/geeks-accelerator/ollama-herd/releases/tag/v0.2.0
[0.1.0]: https://github.com/geeks-accelerator/ollama-herd/releases/tag/v0.1.0
