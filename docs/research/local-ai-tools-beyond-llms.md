# Local AI Tools Beyond LLMs: What Agents Need and Why Routing Matters

## The premise

LLM inference is just one piece of what AI agents do. An agent that generates social media content needs image generation. An agent that processes meeting recordings needs speech-to-text. An agent that builds voice interfaces needs text-to-speech. An agent that reads documents needs OCR.

All of these workloads share the same characteristics as LLM inference: they're GPU-intensive, they compete for memory, they run on specific machines, and they benefit from smart routing across a fleet. The "just use a cloud API" argument breaks down for the same economic reasons documented in [Local Fleet Economics](./local-fleet-economics.md) — zero marginal cost, privacy, no rate limits, no vendor lock-in.

This document surveys the local AI tools that agents actually use in 2026, evaluates which ones benefit from fleet routing, and identifies the integration patterns that would extend Ollama Herd's architecture to cover the full multimodal stack.

## The seven workload categories

### 1. Speech-to-Text (Transcription)

**What it does:** Converts audio to text. Meeting recordings, voice memos, podcast episodes, phone calls, video subtitles.

**The tools:**

| Tool | Architecture | Apple Silicon | Speed | Notes |
|------|-------------|--------------|-------|-------|
| [MLX Whisper](https://github.com/ml-explore/mlx-examples) | MLX-native | Optimized | 2x faster than whisper.cpp | Best on Apple Silicon in 2026 |
| [Whisper.cpp](https://github.com/ggerganov/whisper.cpp) | C++ / GGML | Good (CoreML) | Fast | Widest hardware support |
| [Faster Whisper](https://github.com/SYSTRAN/faster-whisper) | CTranslate2 | Limited | 4x faster than OpenAI Whisper | Needs CUDA for full speed |
| [Qwen3-ASR Swift](https://blog.ivan.digital/qwen3-asr-swift-on-device-asr-tts-for-apple-silicon-architecture-and-benchmarks-27cbf1e4463f) | MLX / Swift | Native | Real-time | On-device, Apache 2.0 |

**Performance on Apple Silicon:**

MLX Whisper with the `large-v3-turbo` model transcribes in [1.02 seconds](https://notes.billmill.org/dev_blog/2026/01/updated_my_mlx_whisper_vs._whisper.cpp_benchmark.html) what whisper.cpp does in 1.23 seconds — a consistent 2x speed advantage. For a 1-hour audio file, this means roughly 3-5 minutes of processing time on an M3 Ultra.

**Why routing helps:**

Transcription is a batch workload. An agent processing a 3-hour meeting recording saturates a node for 10-15 minutes. If another agent needs to transcribe simultaneously, it should go to a different node. The routing pattern is identical to LLM inference — score by available memory, CPU load, and whether the node is already transcribing.

**How it would integrate with Herd:**

```
POST /api/transcribe
Content-Type: multipart/form-data
Body: audio file + options (model, language, format)
Response: JSON with transcription text, segments, timestamps
```

Node detection: `shutil.which("mlx_whisper")` or `shutil.which("whisper-cpp")`. Node wrapper: subprocess that invokes the CLI with the uploaded audio file, returns JSON output. Scoring: penalize nodes currently transcribing (one-at-a-time like image gen).

**Estimated integration effort:** ~4 hours (same pattern as image generation).

---

### 2. Text-to-Speech (Voice Synthesis)

**What it does:** Generates natural-sounding speech from text. Voice agents, audiobook narration, accessibility features, content creation, notification systems.

**The tools:**

| Tool | Quality | Speed | Apple Silicon | Notes |
|------|---------|-------|--------------|-------|
| [Fish Speech S2](https://speech.fish.audio/) | Excellent (ELO 1339) | <150ms latency | Good | 26K+ GitHub stars, emotion control, multi-speaker |
| [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) | Good | 0.1x real-time on M3 Max | Native MPS | Tiny model, runs everywhere |
| [mlx-audio](https://github.com/ml-explore/mlx-audio) | Good | Fast | Native MLX | Apple's ecosystem, STT + TTS + STS |
| [Voxtral TTS](https://mistral.ai/) | High | Fast | Via GGUF | Mistral's open-weight TTS |
| Bark | Creative | Slow | Limited | Music, sound effects, non-verbal |

**Why routing helps:**

High-quality TTS with voice cloning is compute-intensive. Fish Speech S2 generates audio in under 150ms for short phrases, but generating a full article narration (5,000 words) takes 30-60 seconds and saturates the GPU. Multiple agents generating voice content simultaneously need load distribution.

The routing decision matters more for TTS than transcription because TTS is latency-sensitive — a voice agent needs audio within 200ms to feel natural. Routing to the least-loaded node directly impacts user experience.

**How it would integrate:**

```
POST /api/synthesize
Content-Type: application/json
Body: {"text": "...", "voice": "default", "format": "wav"}
Response: audio/wav or audio/mp3 bytes
```

**Estimated integration effort:** ~4 hours.

---

### 3. Image Generation

**What it does:** Creates images from text descriptions. Social media content, marketing materials, product mockups, creative exploration.

**The tools (beyond mflux):**

| Tool | Architecture | Speed (1024px) | Quality | Apple Silicon |
|------|-------------|----------------|---------|--------------|
| [mflux](https://github.com/filipstrand/mflux) (Z-Image-Turbo) | MLX Flux | ~18s | Good | Native, optimized |
| Stable Diffusion (Core ML) | Core ML | ~8s | High | [4x faster than Python](https://embertype.com/blog/best-offline-ai-tools-mac/) via Neural Engine |
| SDXL | Various | ~25s | Excellent | Via MLX or PyTorch |
| Flux Dev | MLX | ~30s | Highest | More detailed, slower |

**Status:** Already integrated in Herd via `/api/generate-image`. See [Image Generation Guide](../guides/image-generation.md).

The pattern established for mflux — heartbeat detection, node-side HTTP wrapper, router endpoint, queue integration — is the template for every other workload category in this document.

---

### 4. Vision and OCR

**What it does:** Extracts text from images, PDFs, screenshots, and scanned documents. Also: image captioning, visual question answering, document understanding.

**The tools:**

| Tool | Type | Speed | Accuracy | Notes |
|------|------|-------|----------|-------|
| [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) | Traditional + Deep | Fast | High | 80+ languages, lightweight |
| [Docling](https://github.com/DS4SD/docling) | Document AI | Medium | Excellent | PDF/DOCX → structured data |
| [Florence-2](https://huggingface.co/microsoft/Florence-2-large) | Vision-Language | Medium | High | Via MLX, multi-task |
| [Umi-OCR](https://github.com/hiroi-sora/Umi-OCR) | Desktop app | Fast | Good | Offline, cross-platform |
| LLaVA / Ollama vision | LLM-based | Slow | Good | Already runs through Herd |

**Why routing helps:**

Document processing is inherently batchable. An agent processing 500 scanned invoices should spread them across nodes — 100 per node on a 5-machine fleet instead of 500 on one machine. This is embarrassingly parallel work that benefits more from routing than almost any other workload.

OCR also has an interesting memory profile: the model is small (~500MB) but processing large PDFs can spike memory temporarily. The scorer should account for available memory when routing OCR batches.

**How it would integrate:**

```
POST /api/ocr
Content-Type: multipart/form-data
Body: image or PDF file + options (language, output_format)
Response: JSON with extracted text, bounding boxes, confidence scores
```

**Estimated integration effort:** ~5 hours (file upload handling adds complexity).

---

### 5. Embeddings and Vector Search

**What it does:** Converts text into high-dimensional vectors for semantic search, RAG (Retrieval-Augmented Generation), duplicate detection, clustering, and recommendation systems.

**The tools:**

| Tool | Type | Speed | Notes |
|------|------|-------|-------|
| Ollama `/api/embeddings` | Ollama-native | Fast | Already routes through Herd |
| [sentence-transformers](https://www.sbert.net/) | Python | Medium | Widest model selection |
| [Chroma](https://www.trychroma.com/) | Vector DB | Fast (storage) | Lightweight, open-source |
| [Qdrant](https://qdrant.tech/) | Vector DB | Fast | Rust-based, high-performance |

**Status:** Partially integrated — Ollama's `/api/embeddings` already routes through Herd like any other Ollama request. The routing and scoring happen automatically.

**Where routing adds value:**

Batch embedding is where routing matters. Embedding 10,000 document chunks for a RAG pipeline takes significant compute. If multiple agents are building knowledge bases simultaneously, the requests should spread across nodes. Herd already handles this for Ollama embeddings.

For non-Ollama embedding models (sentence-transformers), the same node-wrapper pattern would work. But the practical value is lower since Ollama embeddings cover most use cases.

**Estimated integration effort:** ~2 hours (Ollama embeddings already work, non-Ollama would follow the image gen pattern).

---

### 6. Code Execution (Sandboxed)

**What it does:** Runs AI-generated code in isolated environments. Data analysis, file manipulation, API testing, automated debugging.

**The tools:**

| Tool | Architecture | Isolation | Notes |
|------|-------------|-----------|-------|
| [Open Interpreter](https://openinterpreter.com/) | Local subprocess | Low | Natural language → code execution |
| [E2B](https://e2b.dev/) | Cloud sandbox | High | Secure, but cloud-dependent |
| Docker containers | Container | Medium | Standard isolation |
| macOS sandbox | OS-level | Medium | Built-in, no extra software |

**Why routing helps (differently):**

Code execution routing isn't about GPU — it's about isolation and resource management. You might want code execution on dedicated sandbox nodes that don't run production inference. A router could:

- Route code execution to nodes with Docker installed
- Keep code execution off your primary inference nodes
- Track which agent ran what code for audit trails
- Kill long-running code automatically

This is architecturally different from the other workloads. The "scoring" isn't about GPU load — it's about which nodes are designated for sandboxed execution.

**Estimated integration effort:** ~6 hours (fundamentally different pattern from GPU workloads).

---

### 7. Video Processing

**What it does:** Frame extraction, clip generation, video analysis, transcription of video audio tracks, thumbnail generation, content moderation.

**The tools:**

| Tool | Type | Notes |
|------|------|-------|
| FFmpeg | Processing | Universal video manipulation |
| Vision models + frame extraction | Analysis | Extract frames → run through LLaVA/Florence |
| [LocalAI](https://github.com/mudler/LocalAI) | Platform | Supports video as a first-class input |
| Whisper (audio track) | Transcription | Extract audio → transcribe |

**Why routing helps:**

Video is the heaviest local AI workload. Processing a single 4K video can saturate a machine for 10-30 minutes. A 1-hour video at 30fps = 108,000 frames. Even sampling every 5 seconds produces 720 frames that each need vision model analysis.

A video processing pipeline on a fleet would:
1. Route the video to the node with the most available memory
2. Extract frames and distribute vision analysis across nodes
3. Extract audio and route transcription to a different node
4. Aggregate results back at the router

This is the most complex integration because it involves multiple sub-tasks that should themselves be routed independently.

**Estimated integration effort:** ~10 hours (multi-stage pipeline).

---

## The unified routing pattern

Every workload above follows the same architecture that Herd already implements for LLMs and images:

```
                    ┌──────────────────┐
   Request          │   Ollama Herd    │
   (any modality)  →│     Router       │
                    └──────┬───────────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
        ┌──────────┐ ┌──────────┐ ┌──────────┐
        │ Node A   │ │ Node B   │ │ Node C   │
        │ Ollama   │ │ Ollama   │ │ Ollama   │
        │ mflux    │ │ Whisper  │ │ mflux    │
        │ Fish TTS │ │ mflux   │ │ PaddleOCR│
        └──────────┘ └──────────┘ └──────────┘
```

### Four things every integration needs

| Component | What it does | Example (image gen) |
|-----------|-------------|-------------------|
| **Heartbeat field** | Node reports capability | `image: ImageMetrics` with models_available |
| **Node wrapper** | HTTP endpoint wrapping the tool | `image_server.py` → mflux subprocess |
| **Router endpoint** | Accepts requests, scores, proxies | `POST /api/generate-image` |
| **Scoring logic** | Picks the best node | Penalize busy nodes, prefer more memory |

### What the router provides (for any workload)

| Capability | Without router | With router |
|------------|---------------|-------------|
| Discovery | Hardcode IPs and ports | Auto-detect via heartbeats |
| Load balancing | Manual or round-robin | Score-based (memory, CPU, busy state) |
| Failover | Request fails, client retries | Auto-route to next best node |
| Visibility | Check each machine manually | Dashboard shows all workloads |
| Queuing | Client manages concurrency | Queue serializes per-node |
| Tracking | Custom logging per tool | Unified stats API and health checks |

---

## Priority ranking: what to integrate next

Based on agent usage patterns, fleet economics, and integration complexity:

| Priority | Workload | Why | Effort | Impact |
|----------|----------|-----|--------|--------|
| 1 | **Speech-to-Text** | Agents process meetings, calls, podcasts constantly. 10-15 min processing blocks a node. | 4h | High |
| 2 | **Text-to-Speech** | Voice agents are the fastest-growing agent category in 2026. Latency-sensitive — routing directly impacts UX. | 4h | High |
| 3 | **OCR / Document AI** | Batch processing 100s of documents is embarrassingly parallel and benefits enormously from distribution. | 5h | Medium |
| 4 | **Video Processing** | Heaviest workload. A single video analysis can block a node for 30 min. But fewer agents need it today. | 10h | Medium |
| 5 | **Code Execution** | Different architecture (isolation, not GPU). Important but architecturally distinct from other workloads. | 6h | Low-Medium |

Embeddings are excluded because Ollama already handles them through Herd's existing routing.

---

## The multimodal agent pipeline

The end state isn't seven separate endpoints — it's a unified pipeline where an agent can chain modalities:

```
Agent receives voice message
  → POST /api/transcribe (speech-to-text on best node)
  → POST /api/chat (LLM reasoning on best node)
  → POST /api/generate-image (illustration on best node)
  → POST /api/synthesize (voice response on best node)
  → Return to user: text + image + audio
```

Each step routes independently to the optimal node. The agent doesn't know or care which machine handles each step. It just hits `localhost:11435` for everything.

This is where fleet routing transforms from a convenience into an architectural advantage. A single machine can't run all these workloads simultaneously without resource contention. A fleet of 3-5 machines with Herd routing can — each step goes to whichever node has capacity.

### Cascaded vs unified models

The industry is split between two approaches:

**Cascaded pipelines** (what Herd enables): Separate specialized models for each modality, chained together. Each stage can run on a different node, can be swapped independently, and provides visibility at every step. This is the pragmatic choice for 2026 — you get observability, flexibility, and the ability to route each stage optimally.

**Unified multimodal models**: Single models that handle multiple modalities (GPT-4o, Gemini). These process everything internally but offer less control, less visibility, and require cloud APIs for the best versions.

For local fleets, cascaded pipelines win. You can upgrade your TTS model without touching your transcription model. You can route transcription to the node with Whisper while routing TTS to the node with Fish Speech. You get granular performance data for each modality instead of one opaque number.

---

## The economic case

From [Local Fleet Economics](./local-fleet-economics.md), the break-even for LLM inference is 8-10 months. Adding more modalities accelerates the payback because:

1. **Same hardware, more value** — The M3 Ultra already has the compute for transcription, TTS, and image generation. Adding more routing just uses capacity that's sitting idle between LLM requests.

2. **Cloud multimodal APIs are expensive** — ElevenLabs TTS costs $0.30 per 1,000 characters. Whisper API costs $0.006 per minute. DALL-E costs $0.04 per image. At fleet scale with multiple agents, these costs add up fast.

3. **One router for everything** — You don't need separate infrastructure for each modality. Herd's heartbeat + scoring + proxy pattern covers all of them. One dashboard, one health system, one set of operational tools.

**Estimated annual savings for a fleet running all modalities:**

| Modality | Cloud cost/month | Local cost/month | Annual savings |
|----------|-----------------|-----------------|----------------|
| LLM inference | $200-500 | $4 (electricity) | $2,352-5,952 |
| Image generation | $50-200 | $0 | $600-2,400 |
| Transcription | $30-100 | $0 | $360-1,200 |
| TTS | $50-150 | $0 | $600-1,800 |
| OCR | $20-80 | $0 | $240-960 |
| **Total** | **$350-1,030** | **$4** | **$4,152-12,312** |

The hardware (Mac Studio M3 Ultra, $10,000) pays for itself in 10-29 months on LLM inference alone. Add four more modalities and the payback drops to 10-12 months — then you're saving $4,000-12,000 per year.

---

## What this means for Ollama Herd

Herd started as an LLM router. It became an image generation router. The architecture supports becoming a **universal local AI router** — one endpoint for any AI workload, routed to the best available node in your fleet.

The pattern is proven. The four components (heartbeat detection, node wrapper, router endpoint, scoring logic) take 4-6 hours per modality. The economic case compounds with each addition. The operational benefit (one dashboard for everything) grows with each workload.

The question isn't whether to expand — it's which modality to add next. Based on current agent usage patterns, speech-to-text (Whisper) and text-to-speech (Fish Speech) are the highest-impact additions.

---

*Written March 2026. Research compiled from benchmarks, open-source project documentation, and operational experience running a fleet of AI agents on Apple Silicon via Ollama Herd.*

## Sources

- [MLX Framework](https://mlx-framework.org/) — Apple's array framework for machine learning on Apple Silicon
- [MLX Whisper vs whisper.cpp benchmark (Jan 2026)](https://notes.billmill.org/dev_blog/2026/01/updated_my_mlx_whisper_vs._whisper.cpp_benchmark.html) — 2x speed advantage for MLX Whisper
- [Whisper Performance on Apple Silicon](https://www.voicci.com/blog/apple-silicon-whisper-performance.html) — M1/M2/M3/M4 benchmarks
- [Fish Speech S2](https://speech.fish.audio/) — Open-source TTS with <150ms latency, 26K+ stars
- [Fish Speech S2 Review](https://emelia.io/hub/fish-speech-s2-tts) — Competing with ElevenLabs quality
- [Qwen3-ASR Swift](https://blog.ivan.digital/qwen3-asr-swift-on-device-asr-tts-for-apple-silicon-architecture-and-benchmarks-27cbf1e4463f) — On-device ASR + TTS for Apple Silicon
- [Best Open-Source TTS Models 2026](https://www.siliconflow.com/articles/en/best-open-source-text-to-speech-models) — Comprehensive comparison
- [Best Offline AI Tools for Mac 2026](https://embertype.com/blog/best-offline-ai-tools-mac/) — Core ML Stable Diffusion 4x faster
- [LocalAI](https://github.com/mudler/LocalAI) — Run any model (LLMs, vision, voice, image, video) on any hardware
- [AI Agent Tools Landscape 2026](https://www.stackone.com/blog/ai-agent-tools-landscape-2026/) — 120+ tools mapped across 11 categories
- [10 Open Source Tools for Local AI Agents](https://dev.to/james_miller_8dc58a89cb9e/10-open-source-tools-to-build-production-grade-local-ai-agents-in-2026-say-goodbye-to-sky-high-apis-1ipg)
- [Apple's Sleeper Advantage for Local LLMs](https://www.xda-developers.com/apple-sleeper-advantage-local-llms/)
- [Multimodal AI Agents: Voice, Vision, and Text](https://www.chanl.ai/blog/multimodal-ai-agents-voice-vision-text-production) — Production architecture patterns
- [Open Source Voice Cloning Models 2026](https://www.siliconflow.com/articles/en/best-open-source-models-for-voice-cloning) — Fish Speech, Bark, XTTS comparison
- [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) — 80+ language OCR
- [Docling](https://github.com/DS4SD/docling) — Document AI for PDF/DOCX
- [Open Interpreter](https://openinterpreter.com/) — Natural language code execution
