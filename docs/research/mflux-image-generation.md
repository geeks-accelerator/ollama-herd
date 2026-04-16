# mflux Image Generation Guide

## What mflux Is

mflux is an MLX-native implementation of Flux image generation models, built specifically for Apple Silicon. Unlike Ollama (which wraps LLMs), mflux runs diffusion models directly through Apple's MLX framework, talking to the unified memory and GPU without translation layers.

It's a strong option for any workflow that generates images locally on Apple Silicon — bot systems, creative tools, content pipelines.

## Why mflux Instead of Ollama or a Cloud API

**Performance on Apple Silicon.** MLX was designed for the unified memory architecture on M-series chips. mflux takes advantage of this — the model sits in the same memory pool as the GPU, so there's no copying between CPU and GPU memory. On a Mac Studio M3 Ultra (512GB), generation takes ~20 seconds per 1024x1024 image.

**No server process.** mflux runs as a CLI subprocess — invoke it, get an image, done. No daemon to manage, no port to configure, no health checks. Call `subprocess.run()` with the prompt and output path, wait for the file, and move on.

**Cost.** Zero. All compute is local. No API costs regardless of volume.

**Privacy.** Image prompts stay on-device. The LLM generates the prompt (via Claude/Ollama), mflux generates the image locally. Nothing leaves the machine unless you upload it.

## Current Setup

### Model

**Z-Image-Turbo** (`Tongyi-MAI/Z-Image-Turbo`) — a fast, ungated Flux variant optimized for few-step generation. ~3GB when quantized to 8-bit.

| Setting | Value | Why |
|---|---|---|
| Quantize | 8-bit | Cuts memory footprint ~3x with minimal quality loss |
| Steps | 4 | Turbo model is optimized for 4-step generation |
| Resolution | 1024x1024 | Good balance of quality and speed |
| Timeout | 180s | Safety net — normal generation is ~20s |

### Installation

```bash
# Install mflux via uv (recommended)
uv tool install mflux

# Verify
mflux-generate-z-image-turbo --help
```

The first run downloads the model weights (~3GB). Subsequent runs load from cache.

### Typical Pipeline

A typical image generation pipeline looks like this:

```
LLM call: generate_image_prompt()     ← LLM creates the prompt
    ↓
image_gen: generate_image(prompt)     ← mflux subprocess generates PNG
    ↓
upload or save locally                ← Use the image however you need
```

The LLM call and the image generation are completely separate systems — Claude/Ollama handles the creative direction (what to generate), mflux handles the pixels.

## Why Not Route Through Ollama Herd

Ollama Herd sits in front of Ollama instances as a smart inference router. But mflux bypasses it entirely. Here's why:

### 1. Different protocol

Ollama Herd speaks the Ollama API — `POST /api/chat`, `POST /api/generate`, streaming completions. mflux is a CLI tool that takes a text prompt and writes a PNG file to disk. There's no HTTP API to route to.

### 2. Single machine, nothing to route

Herd's core value is multi-node routing: when you have multiple Ollama instances across machines, it picks the best one based on thermal state, memory fit, queue depth, and latency history. With mflux on a single Mac Studio, there's one machine, one model, one process. The routing decision is trivial — there's only one option.

### 3. Different resource profile

LLM inference is memory-bound and benefits from request queuing (Ollama's `OLLAMA_NUM_PARALLEL=16`). Image generation is compute-bound and serializes naturally — you generate one image at a time. The scheduling problems are different.

## What an Image Generation Router Would Look Like

If you were building a Herd-like router for image generation across multiple machines, it would need to solve different problems than LLM routing:

### Architecture

```
                    ┌──────────────┐
   POST /generate   │  Image Herd  │
   {prompt, size}  →│   (Router)   │
                    └──────┬───────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
        ┌──────────┐ ┌──────────┐ ┌──────────┐
        │ Node A   │ │ Node B   │ │ Node C   │
        │ Mac Studio│ │ Mac Studio│ │ Mac Mini │
        │ mflux    │ │ mflux    │ │ mflux    │
        └──────────┘ └──────────┘ └──────────┘
```

### What It Would Route On

| Signal | Why | Difference from LLM routing |
|---|---|---|
| **GPU utilization** | Image generation is compute-bound, not memory-bound | LLM routing cares more about memory fit |
| **Thermal state** | Sustained image generation heats up Apple Silicon fast | Same concern, but image gen causes thermal spikes vs LLM's sustained warmth |
| **Queue depth** | Only 1 image at a time per node (no parallel batching) | LLMs can run 16 parallel requests in Ollama |
| **Model loaded** | Different nodes might have different models (SDXL, Flux, etc.) | Similar to LLM model routing |
| **Resolution capability** | Larger images need more memory — not every node can do 2048x2048 | Analogous to LLM context window limits |

### API Design

A minimal image routing API would look like:

```
POST /api/generate
{
  "prompt": "a neon-lit Tokyo alley at midnight, cyberpunk aesthetic",
  "width": 1024,
  "height": 1024,
  "model": "z-image-turbo",
  "steps": 4,
  "seed": null
}

Response (streaming or poll):
{
  "id": "img_abc123",
  "status": "completed",
  "image_url": "http://node-a:8080/images/img_abc123.png",
  "node": "node-a",
  "generation_time_ms": 18500,
  "model": "z-image-turbo"
}
```

### Key Differences from Ollama Herd

| Aspect | Ollama Herd (LLM) | Image Herd (hypothetical) |
|---|---|---|
| **Concurrency** | 16 parallel requests per node | 1 at a time per node (sequential) |
| **Latency** | Streaming tokens, first-token matters | Batch output — all or nothing, total time matters |
| **Memory model** | Model stays loaded, requests share it | Model loads per-request or stays resident |
| **Output** | Text stream | Binary file (PNG/JPEG) |
| **Node discovery** | mDNS (Ollama broadcast) | Would need custom broadcast or registration |
| **Failure mode** | Retry on another node mid-stream | Retry from scratch on another node |
| **Scheduling** | Route to least-loaded node | Route to coolest node (thermal is the bottleneck) |

### When It Would Matter

An image router becomes valuable when:

- **Multiple machines** are generating images and you want to distribute load
- **Mixed hardware** (M3 Ultra vs M4 Max vs M2) where generation speed varies and you want to route to the fastest available node
- **Multiple models** where different nodes specialize (one runs Flux Turbo for speed, another runs SDXL for quality)
- **Thermal management** matters — sustained generation on a single machine throttles after ~30 minutes, rotating across nodes keeps everything cool

For a single-machine setup (1 Mac Studio, 1 model), the overhead of a router adds latency without benefit. But when scaling to multiple machines or higher volume, distributing image generation across a cluster would be the natural next step — and an image-aware Herd would be how you'd do it.
