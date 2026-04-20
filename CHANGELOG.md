# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Platform connection UX** — opt-in Settings-tab card to connect a node to `platform.ollamaherd.com`. Three new OSS routes: `GET /api/platform/status`, `POST /api/platform/connect`, `POST /api/platform/disconnect`. Paste operator token in the dashboard instead of SSHing into the node to edit YAML. Validates token via `GET /api/auth/me`, generates Ed25519 keypair (mode 0600), registers the node, persists state to `~/.fleet-manager/platform.json` (mode 0600). CLI + env var parity (`--platform-token` / `FLEET_NODE_PLATFORM_TOKEN`). No data is transmitted until a feature is opted into separately. Prerequisite for usage telemetry (next plan).
- **Platform telemetry opt-in flags (not yet wired)** — `--telemetry-local-summary` and `--telemetry-include-tags` CLI flags plus env var parity. Tag transmission is a *separate* opt-in because tag values (e.g. `project:internal-audit`) can be mildly identifying. Flags persist to NodeSettings but the daily rollup emitter is a follow-up PR. Retention policy: platform keeps rollups 90 days rolling.
- **Cryptography dependency** — `cryptography>=42.0.0` added for Ed25519 keypair generation used by platform connection.
- **`__version__` reads from package metadata** — `src/fleet_manager/__init__.py` now uses `importlib.metadata.version("ollama-herd")` so it can't drift from `pyproject.toml` again. Previously hardcoded at 0.3.0 while pyproject was 0.5.2.
- **Vision embedding service** — new `/api/embed-image` endpoint serves image embeddings via DINOv2 (384-dim, 85MB), SigLIP2 (768-dim, 90MB int8), CLIP (512-dim) via ONNX Runtime. Auto-downloads from HuggingFace, runs on port 11438 internally, proxied through router on 11435. `/api/embed` auto-routes vision model names (clip, dinov2, siglip) to the embedding service. Added to `/api/tags` for client discovery.
- **Priority model preloading** — on restart, loads most-used models first based on weighted scoring: `(24h_requests * 3) + (7d_daily_avg)`. Prevents primary models like gpt-oss:120b from being evicted by whatever model happens to be requested first.
- **Priority model refresh** — every 10 minutes, reloads priority models if evicted. Respects user intent: only refreshes models with requests in the last hour (so manual `ollama stop X` isn't overridden).
- **VRAM fallback priority protection** — blocks fallback from a high-priority model to a low-priority one. Request for gpt-oss:120b no longer silently routes to gemma3:27b.
- **`/api/version` endpoint** — returns Ollama version (compatibility) + `herd_version`. Health checks from Open WebUI, LangChain, etc. now work.
- **Connection failure tracking** — node agent tracks connection failures, heartbeat reports them, health check (#17) surfaces active failures and recoveries.
- **SSE watchdog** — dashboard auto-reconnects after 10s of silence, preventing stale state after network drops. The dashboard model list now updates live (model loads/unloads trigger card rebuild).
- **Vision model support** — new `VISION` model category for image understanding (image → text). 7 vision models in catalog: gemma3 (4B/12B/27B), llama3.2-vision (11B/90B), llava (7B/13B/34B), moondream, minicpm-v
- **OpenAI image format conversion** — OpenAI `image_url` content blocks auto-convert to Ollama's `images` field. HTTP image URLs auto-fetched and converted to base64.
- **Image token estimation** — `estimate_tokens()` accounts for image tokens (~150 per image) in both OpenAI and Ollama formats
- **`is_vision_model()` helper** — programmatic detection of vision-capable models
- **Vision in model recommender** — VISION included in default category priorities
- **Fleet Intelligence enrichment** — per-model traffic breakdown, per-node disk space, all health warnings (not just first 3), previous briefing continuity (500 chars), priority model status, 2 runtime bugs fixed (KeyError + AttributeError that were silently failing briefings)
- **Health checks** — now 18 (was 16): connection failures (#17), priority models (#18)
- **Model preloader in module table** — `node/embedding_models.py`, `node/embedding_server.py`, `server/model_preloader.py`
- **Route: `server/routes/embedding_compat.py`** — vision embedding endpoint
- **Config: `FLEET_VISION_EMBEDDING`**, `FLEET_VISION_EMBEDDING_TIMEOUT`, `FLEET_EMBEDDING_USE_COREML` (opt-in)
- **Silent model fallback detection** — `trace_store.get_silent_fallback_stats()` detects requests where `original_model != model` (VRAM fallback routed away from requested model). Fleet Intelligence now surfaces these as "SILENT FALLBACK in last 24h" — catches silent degradation where requests succeed but are served by the wrong model.
- **Static "Fleet offline" briefing** — when no nodes are online, Fleet Intelligence returns a static message explaining the state instead of trying to call an LLM that doesn't exist.

### Changed

- **Fleet Intelligence refresh intervals rebalanced** — backs off under load, refreshes faster when idle:
  - Very busy (>5 in-flight): 2 hours (was 30 min) — don't compete with real requests
  - Active (1-5 in-flight): 1 hour (was 1 hour) — unchanged
  - Idle (0 in-flight): 30 min (was 6 hours) — catch overnight silent failures
  - No nodes online: 1 hour static (was 1 hour LLM call)

### Fixed

- **CoreML provider triggered macOS TCC dialogs that froze the node overnight** — `CoreMLExecutionProvider` requested Neural Engine access on first inference, producing a permission dialog that blocked the Python process until someone dismissed it. Happened twice in 5 days (April 14 + 19). Fixed by defaulting to CPU-only inference (opt-in to CoreML via `FLEET_EMBEDDING_USE_COREML=true`). CPU is fast enough on M-series (~60ms/image).
- **`/api/generate` returned empty `response` field** — proxy converted generate to chat format internally, populated `message.content` but left `response` empty. Non-streaming clients got empty strings despite model generating tokens. Now both fields populated.
- **Fleet Intelligence briefings were silently failing** — `report.score` (AttributeError) and `overall['avg_latency_ms']` (KeyError) bugs in the prompt assembly caught by bare except, so briefings appeared to work but had no health/traffic content. Fixed.
- **Priority cache wasn't populated** — VRAM fallback couldn't read priority scores because preloader called `get_model_priority_scores()` directly instead of `get_cached_priorities()`. Also fixed Python import rebinding issue where `routing.py` imported `_priority_cache` by value and saw empty list after module rebind.
- **Dashboard model list didn't auto-update** — SSE fast-path signature only checked `node_id:status`, not the loaded model list. Model loads/unloads didn't trigger card rebuild.
- **Dashboard model counts** — now shows "Ollama Models: 3 loaded, 17 on disk | Services: 8 loaded" instead of misleading unified count.
- **Vision embedding tests** — added 7 edge case tests (HTTP URL fetch, HTTP fetch failure, empty base64, mixed data URI + HTTP, token estimation, vision model fallback). 507 tests total (was 445).
- **Stale references updated** — 445 → 507 tests, 17 → 18 health checks, 0.4.1 → 0.5.2 version across all skill files and docs

## [0.5.2] - 2026-04-13

### Fixed

- **Dashboard header stats going stale** — the in-place DOM update (added in 0.5.0 to prevent card flashing) was replacing header-stats innerHTML with only Nodes + Models Loaded, wiping Queued + Completed on every SSE tick. All 4 stats now use stable element IDs updated via textContent — no more innerHTML replacement race.

## [0.5.1] - 2026-04-09

### Fixed

- **Dashboard SSE stale data** — `connect()` ran before footer DOM elements existed, causing TypeError that prevented SSE event handlers from registering. Dashboard would show "Waiting for nodes..." and never update. Added null checks to onopen/onerror handlers.

## [0.5.0] - 2026-04-09

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
- **Fleet Intelligence briefing** — LLM-powered dashboard card that analyzes fleet health, context usage, and traffic using the fleet's own models. Adaptive refresh (30min when busy, 6h when idle), dismiss/refresh buttons, history persisted to SQLite
- **Dashboard visual enhancements:**
  - Gradient progress bars — smooth HSL color transition (green→yellow→red) on all CPU, memory, availability, and benchmark bars
  - Animated health score ring — conic-gradient fills from 0% to score on page load
  - Staggered card entry — node cards fade in sequentially with 60ms delay
  - Hover card lift — cards rise 2px with shadow on hover
  - Model badge colors by type — purple (LLM), blue (embed), orange (image), green (STT) with glow on hot models
  - In-place SSE updates — node card values update without rebuilding DOM (no more flashing)
- **Shared date range selector** on Trends, Model Insights, and Tags pages — presets (24h, 48h, 72h, 7d, 30d) + custom datetime-local picker in user's local timezone
- **Settings context management UI** — per-model table showing allocated ctx, p99 total tokens, utilization %, recommended ctx, savings %, with override input and Apply/Use Rec. buttons
- **Briefing history** — `GET /dashboard/api/briefing/history` reads from SQLite, viewable on Health page with "Generate New" button
- `GET /dashboard/api/briefing` — fleet intelligence briefing with adaptive caching
- `GET /dashboard/api/tags` + `/dashboard/api/tags/daily` — renamed from `/api/apps`
- 16 health checks total (up from 15 in 0.4.1)

### Fixed

- **`_request_tokens` encapsulation** (#4) — added `pop_token_counts()` and `pop_request_meta()` public methods on `StreamingProxy`, replaced all direct private dict access in route handlers
- **`asyncio.ensure_future` deprecated** (#5) — replaced with `asyncio.create_task()` in discovery.py
- **KV cache bloat fix message** (#16) — added Windows instructions alongside macOS/Linux
- **Benchmark chart x-axis** — shows date + time ("Apr 8 2:30 PM") instead of just date, so same-day runs are distinguishable
- **Smart benchmark skips cloud models** — filters out `:cloud` suffix models (API proxies that don't load locally)
- **Smart benchmark skips embedding/image models** for LLM category coverage — `nomic-embed-text` no longer blocks loading a general-purpose LLM
- **Context recommendation uses total tokens** — was using prompt-only p99 (caused truncation at 8K), now uses p99 of prompt+completion with 50% headroom and 24h rolling max floor
- **Node card flashing** — SSE updates now modify individual values in-place instead of rebuilding entire DOM every 2 seconds
- **Fleet Intelligence prompt** — lists real commands only (herd-node, curl /api/pull, Settings toggles), bans hallucinated commands

### Changed

- `benchmark_engine.py` extracted from `scripts/benchmark.py` — shared core logic between CLI and server-side runner
- `scripts/benchmark.py` is now a thin CLI wrapper importing from `benchmark_engine`
- Dashboard settings API accepts `dynamic_num_ctx`, `num_ctx_auto_calculate`, and `num_ctx_overrides`
- Settings GET response includes `context` section with all num_ctx state
- `StreamingProxy.pull_model()` accepts optional `progress_cb` callback for download progress tracking
- Benchmark `per_model_results` includes `model_type` field (llm/embed/image)
- **Apps → Tags rename** — dashboard tab, routes (`/dashboard/tags`), and APIs (`/dashboard/api/tags`) renamed for clarity. Old `/dashboard/apps` URLs still work (backwards compat)
- Trends, Models, Tags pages use `start_ts`/`end_ts` query params instead of just `hours`/`days`
- CLAUDE.md optimized from 246 → 143 lines (42% token reduction per turn)

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
