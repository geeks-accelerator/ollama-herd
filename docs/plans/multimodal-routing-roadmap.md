# Multimodal Routing Roadmap

**Status**: Planning
**Date**: March 2026
**Related**: [Local AI Tools Beyond LLMs](../research/local-ai-tools-beyond-llms.md) | [Local Fleet Economics](../research/local-fleet-economics.md) | [Image Generation (implemented)](../guides/image-generation.md)

## Where we are

Ollama Herd routes two workloads today:

1. **LLM inference** (Ollama) — 7-signal scoring, queue management, auto-retry, context protection, VRAM fallback
2. **Image generation** (mflux) — node detection, subprocess wrapper, queue integration, dashboard visibility

Both follow the same proven pattern: heartbeat detection → node wrapper → router endpoint → queue → dashboard. Adding a new modality takes ~4-6 hours because the architecture is already built.

## Where we're going

The fleet should handle any AI workload an agent needs — text, images, speech, documents. One endpoint, any modality, best available node. This roadmap prioritizes by agent demand, quality of available local tools, and economic impact.

## Priority 1: Speech-to-Text with Speaker Diarization

**Endpoint**: `POST /api/transcribe`
**Why first**: Every agent that processes meetings, calls, podcasts, or voice notes needs transcription. It's the most requested missing modality.
**Estimated effort**: 5 hours

### The model choice matters

Vanilla Whisper is outdated. The landscape has three tiers:

| Tier | Model | WER | Diarization | Speed | Runs locally |
|------|-------|-----|-------------|-------|-------------|
| Cloud best | OpenAI gpt-4o-mini-transcribe | ~3-4% | Built-in | Fast | No |
| Local best | Qwen3-ASR | ~5% | No | 1000+ RTFx | Yes (MLX/Swift) |
| Local + diarization | WhisperX + Pyannote 3.1 | ~7% + 10% DER | Yes | Moderate | Yes |

**Recommendation: Support both Qwen3-ASR and WhisperX.**

- **Qwen3-ASR** for pure accuracy — new state-of-the-art open-source STT in 2026, beats Whisper on almost all metrics, has a native Swift/MLX package for Apple Silicon
- **WhisperX** for when you need speaker labels — combines Whisper with Pyannote 3.1 for automatic diarization (who said what)

The quality gap between local (Qwen3-ASR at ~5% WER) and cloud (gpt-4o-mini-transcribe at ~3-4%) is now only 1-2 percentage points. For most agent use cases (meeting notes, podcast summaries, voice memo processing), this gap is imperceptible.

### API design

```
POST /api/transcribe
Content-Type: multipart/form-data

Fields:
  audio: <file>              # WAV, MP3, M4A, etc.
  model: "qwen3-asr"         # or "whisperx"
  language: "en"             # optional, auto-detect if omitted
  diarize: false             # true to enable speaker labels (WhisperX only)
  format: "json"             # or "text", "srt", "vtt"

Response (JSON):
{
  "text": "Full transcription text...",
  "segments": [
    {
      "start": 0.0,
      "end": 4.5,
      "text": "Hello, how are you?",
      "speaker": "SPEAKER_01"    // only if diarize=true
    }
  ],
  "language": "en",
  "duration_seconds": 3600,
  "processing_time_ms": 45000
}

Headers:
  X-Fleet-Node: mac-studio
  X-Transcription-Time: 45000
```

### Implementation

**Node detection**: Check for `mlx_whisper`, `whisperx`, or `qwen3-asr` binaries/packages.

**Node wrapper**: `node/transcription_server.py` — accepts audio file upload, runs the model as subprocess or Python call, returns JSON.

**Scoring**: Penalize nodes currently transcribing (one long-running job at a time). Prefer nodes with more available memory. Weight by audio duration — a 3-hour file needs more capacity than a 30-second voice note.

**Queue key**: `Neons-Mac-Studio:qwen3-asr:latest` — same pattern as LLM and image queues.

### Why not just use Ollama's audio models?

Ollama doesn't support audio input. Whisper/Qwen3-ASR are separate model architectures that need their own inference pipelines. This is genuinely a different workload that can't go through the existing Ollama proxy.

### Economic impact

- Cloud: Whisper API $0.006/min, gpt-4o-mini-transcribe ~$0.01/min
- Processing 2 hours of audio per day across a fleet: $12-20/month cloud
- Local: $0/month after hardware
- Annual savings: $144-240 (modest, but compounds with fleet scale)

The real value isn't cost — it's privacy (meeting transcriptions contain sensitive business content) and availability (no rate limits during batch processing).

---

## Priority 2: Text-to-Speech

**Endpoint**: `POST /api/synthesize`
**Why second**: Voice agents are the fastest-growing agent category in 2026. Every voice-enabled bot needs TTS. Latency-sensitive — routing directly impacts user experience.
**Estimated effort**: 4 hours

### The model choice

| Model | Latency | Quality (ELO) | Voice cloning | Memory | Local |
|-------|---------|---------------|---------------|--------|-------|
| Fish Speech S2 | <150ms | 1339 | Yes | ~2GB | Yes |
| Kokoro-82M | ~100ms | Good | No | ~200MB | Yes (MPS) |
| mlx-audio | Fast | Good | Limited | ~1GB | Yes (MLX) |
| ElevenLabs (cloud) | ~200ms | Best | Yes | — | No |

**Recommendation: Fish Speech S2.**

26K+ GitHub stars, emotion control through natural language tags, multi-speaker generation in a single pass, and quality that rivals ElevenLabs. The 150ms latency is under the 200ms threshold for natural-feeling voice interaction.

Kokoro-82M is a great fallback for devices with limited memory — at 82M parameters it runs on anything, including Mac Minis with 8GB.

### API design

```
POST /api/synthesize
Content-Type: application/json

{
  "text": "Hello, welcome to the fleet.",
  "model": "fish-speech-s2",     // or "kokoro"
  "voice": "default",            // voice preset or cloned voice ID
  "format": "wav",               // or "mp3", "opus"
  "speed": 1.0,                  // playback speed multiplier
  "emotion": "cheerful"          // Fish Speech S2 emotion tags
}

Response:
Content-Type: audio/wav
X-Fleet-Node: mac-mini-2
X-Synthesis-Time: 1250
Body: <raw audio bytes>
```

### Why TTS matters for fleets

TTS is uniquely latency-sensitive. A voice agent needs the first audio chunk within 200ms to feel natural. If one node is generating a long narration (30 seconds of audio), other TTS requests should route to a different node immediately. This is where fleet routing provides the biggest UX improvement.

A single Mac Mini can handle ~5 concurrent short TTS requests with Kokoro-82M, or 1-2 with Fish Speech S2. A fleet of 3 Minis can handle 15+ concurrent voice interactions.

### Economic impact

- Cloud: ElevenLabs $0.30/1000 chars, Amazon Polly $4/1M chars
- 10,000 characters per day across agents: $3-9/month cloud
- At scale (100K chars/day): $30-90/month
- Annual savings: $360-1,080

---

## Priority 3: Additional Image Models

**Endpoint**: Same `/api/generate-image` — extend model support
**Why third**: mflux already works well. This is optimization, not a new capability.
**Estimated effort**: 3 hours

### What to add

| Model | Tool | Advantage over mflux |
|-------|------|---------------------|
| SDXL | Draw Things CLI | 25% faster, 50% less memory via on-demand weight loading |
| Stable Diffusion 3 | DiffusionKit | Native Swift, Core ML + Neural Engine, newest SD |
| Flux Dev | Already in mflux | Higher quality than Z-Image-Turbo, same binary |

### Implementation

**Draw Things** is the biggest win — it's the most optimized image gen tool on Apple Silicon. The CLI (`draw-things-cli`) follows the same pattern as mflux. Detection via `shutil.which("draw-things-cli")`, same subprocess wrapper, same queue integration.

**DiffusionKit** is interesting for the future — it's a Swift package that uses Core ML + Neural Engine, potentially the fastest option on Apple Silicon. But it's a library, not a CLI, so the wrapper would need to be a small Swift binary.

**Flux Dev via mflux** already works — just pass `--model dev` to `mflux-generate`. We already detect `mflux-generate` and report `flux-dev` as available. The only change needed is documenting the quality/speed tradeoff.

---

## Priority 4: OCR / Document AI

**Endpoint**: `POST /api/ocr`
**Why fourth**: Valuable for batch document processing but fewer agents need it daily compared to transcription or TTS.
**Estimated effort**: 5 hours

### The model choice

| Model | Type | Accuracy | Speed | Apple Silicon |
|-------|------|----------|-------|--------------|
| PaddleOCR | Traditional + Deep | High | Fast | Good |
| Docling | Document AI | Excellent | Medium | Good |
| Florence-2 (MLX) | Vision-Language | High | Medium | Native |
| Ollama + LLaVA | LLM-based | Good | Slow | Already routed |

**Recommendation: PaddleOCR for speed, Docling for structured output.**

PaddleOCR handles 80+ languages and is fast enough for batch processing. Docling converts PDFs/DOCX into structured data (tables, headers, sections) which is more useful for RAG pipelines.

For simple "read this screenshot" tasks, Ollama + LLaVA already works through Herd. OCR integration is for high-volume, high-accuracy document processing.

### API design

```
POST /api/ocr
Content-Type: multipart/form-data

Fields:
  file: <image or PDF>
  model: "paddleocr"          // or "docling"
  language: "en"
  output_format: "json"       // or "text", "markdown"

Response:
{
  "text": "Extracted text...",
  "pages": [...],
  "tables": [...],
  "confidence": 0.95,
  "processing_time_ms": 2500
}
```

### Why routing helps specifically for OCR

Document processing is **embarrassingly parallel**. Processing 500 invoices:
- 1 node: 500 documents × 5s each = 42 minutes
- 5 nodes via Herd: 100 documents each × 5s = 8.3 minutes

This is the workload that benefits most from fleet distribution. The router could even split a multi-page PDF across nodes (page 1-10 to node A, 11-20 to node B).

---

## Priority 5: Video Processing

**Endpoint**: `POST /api/analyze-video`
**Why fifth**: Heaviest workload but fewest agents need it today. Multi-stage pipeline makes integration complex.
**Estimated effort**: 10 hours

### Architecture

Video processing is fundamentally different — it's a pipeline, not a single model call:

```
Video file
  → FFmpeg: extract audio track → POST /api/transcribe (route to best node)
  → FFmpeg: extract key frames → POST /api/ocr per frame (distribute across nodes)
  → Aggregate transcription + frame analysis → LLM summarization
  → Return combined result
```

Each sub-task routes independently. The router acts as an orchestrator, not just a proxy.

### When it becomes priority 1

If agents start processing video content regularly (YouTube analysis, security camera feeds, video content creation), this jumps to the top. But today, most agent video work is "extract the audio and transcribe it" — which Priority 1 (transcription) already handles.

---

## Priority 6: Code Execution (Sandboxed)

**Endpoint**: `POST /api/execute`
**Why last**: Architecturally different from GPU workloads. Important but requires solving isolation/security problems that don't apply to other modalities.
**Estimated effort**: 6 hours

### How it differs

Every other workload on this list is GPU-bound and follows the same pattern: detect tool → wrap subprocess → score by memory/CPU → route. Code execution is:

- **CPU-bound**, not GPU-bound
- **Security-sensitive** — needs sandboxing (Docker, macOS sandbox)
- **Isolation-oriented** — should run on dedicated nodes, not inference nodes
- **Stateful** — code may read/write files, access networks

### When it matters

When agents need to run generated code (data analysis, file processing, API testing) and you want to isolate that from your inference fleet. The routing decision isn't "who has capacity?" — it's "who is designated for code execution?"

---

## The pattern for each integration

Every new modality follows the same 4-component pattern established by image generation:

```
1. models/node.py        → Add detection metrics to HeartbeatPayload + NodeState
2. node/collector.py     → Detect tool availability (shutil.which, process check)
3. node/<tool>_server.py → FastAPI wrapper that invokes the tool as subprocess
4. server/routes/<tool>_compat.py → Router endpoint with scoring + queue integration
```

Plus:
- Config toggle in `models/config.py` (disabled by default)
- Settings dashboard toggle
- Health check for activity monitoring
- Tests (~10-15 per modality)

### Estimated total for all 6 priorities

| Priority | Modality | Effort | Cumulative |
|----------|----------|--------|-----------|
| 1 | Speech-to-Text | 5h | 5h |
| 2 | Text-to-Speech | 4h | 9h |
| 3 | More image models | 3h | 12h |
| 4 | OCR / Document AI | 5h | 17h |
| 5 | Video processing | 10h | 27h |
| 6 | Code execution | 6h | 33h |

33 hours total to make Herd a universal local AI router. Priorities 1-3 (12 hours) cover 80% of agent needs.

---

## Economic summary

Annual savings vs cloud APIs for a fleet of 8 agents:

| Modality | Cloud cost/year | Local cost/year | Savings |
|----------|----------------|----------------|---------|
| LLM inference | $2,400-6,000 | $48 | $2,352-5,952 |
| Image generation | $600-2,400 | $0 | $600-2,400 |
| Transcription (STT) | $144-240 | $0 | $144-240 |
| Text-to-speech | $360-1,080 | $0 | $360-1,080 |
| OCR | $240-960 | $0 | $240-960 |
| **Total** | **$3,744-10,680** | **$48** | **$3,696-10,632** |

The Mac Studio ($10,000) pays for itself in 12-32 months on LLM inference alone. Add all modalities and payback drops to 11-16 months, then saves $3,700-10,600 every year after.

---

## Decision framework: when to add a new modality

Add a new modality when:

1. **Agents are hitting cloud APIs for it** — you're paying per-request for something that could be free
2. **A local model exists within 5% quality of cloud** — the gap is small enough that agents don't notice
3. **The workload blocks nodes** — long-running jobs need routing to avoid starving other tasks
4. **Privacy matters** — the data shouldn't leave the network (meeting transcripts, business documents, customer content)

Don't add a modality when:

1. **Cloud is 10x+ better** — some tasks still need frontier models (complex video understanding)
2. **Usage is rare** — one request per week doesn't justify integration effort
3. **It's already covered** — Ollama embeddings and LLaVA vision already route through Herd
