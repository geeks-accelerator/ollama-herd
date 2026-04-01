# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0] - 2026-04-01

### Added

- **Embeddings proxy** — `/api/embed` and `/api/embeddings` endpoints route embedding requests to the best available node. Supports both `input` (single or batch) and `prompt` (legacy) fields. Response proxied directly from Ollama.
- **OpenAI-compatible image generation** — `/v1/images/generations` wraps the fleet's image generation in OpenAI's standard API format. Works with the OpenAI SDK (`client.images.generate()`). Supports `size`, `response_format`, `steps`, `guidance`, `seed`, and `negative_prompt`.
- **Image model discovery** — `/api/image-models` lists all image models across the fleet with backend type and which nodes have them. Image models also now appear in `/api/tags` and `/v1/models` responses.
- **Request tagging for image and STT** — `metadata.tags` and `X-Herd-Tags` header now work on `/api/generate-image` and `/api/transcribe`, not just LLM endpoints. All four model types appear in the Apps dashboard tab.
- **DeepSeek-V3 in model catalog** — 3 variants added to model knowledge: `deepseek-v3:7b` (7B distill), `deepseek-v3:32b` (32B distill), and `deepseek-v3:671b` (671B MoE, 404GB). Model recommender can now suggest V3 alongside R1.
- **Expanded README** — comprehensive usage docs for all 4 model types with SDK examples, model comparison tables, discovery endpoints, and batch examples
- 24 new tests (436 total)

### Changed

- `/api/tags` response includes mflux, DiffusionKit, and Ollama native image models alongside LLM models
- `/v1/models` response includes image models with `type: "image"` in metadata

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

[0.4.0]: https://github.com/geeks-accelerator/ollama-herd/releases/tag/v0.4.0
[0.3.0]: https://github.com/geeks-accelerator/ollama-herd/releases/tag/v0.3.0
[0.2.0]: https://github.com/geeks-accelerator/ollama-herd/releases/tag/v0.2.0
[0.1.0]: https://github.com/geeks-accelerator/ollama-herd/releases/tag/v0.1.0
