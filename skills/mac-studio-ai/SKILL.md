---
name: mac-studio-ai
description: Mac Studio AI — run LLMs, image generation, speech-to-text, and embeddings on your Mac Studio. M2 Ultra (192GB), M3 Ultra (512GB), M4 Max (128GB), and M4 Ultra (256GB) configurations make the Mac Studio the most powerful local AI device available. Load 120B+ parameter models entirely in unified memory. Route requests across multiple Mac Studios with zero configuration.
version: 1.0.0
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"desktop","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","pip"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["darwin"]}}
---

# Mac Studio AI — The Most Powerful Local AI Machine

The Mac Studio is the best hardware for local AI inference. M4 Ultra with 256GB of unified memory runs 120B+ parameter models that would need multiple NVIDIA A100s. M3 Ultra with 512GB loads frontier models that don't fit on any single GPU. No PCIe bottleneck, no CPU-GPU transfer — everything in one memory pool.

This skill turns one Mac Studio into a powerhouse and multiple Mac Studios into a fleet.

## Mac Studio configurations for AI

| Config | Chip | Unified Memory | GPU Cores | LLM Sweet Spot | Image Gen |
|--------|------|---------------|-----------|----------------|-----------|
| Mac Studio M4 Max | M4 Max | 128GB | 40 | 70B models | Fast |
| Mac Studio M4 Ultra | M4 Ultra | 256GB | 80 | 120B+ models | Very fast |
| Mac Studio M3 Ultra | M3 Ultra | 192-512GB | 76 | 120B-236B models | Very fast |
| Mac Studio M2 Ultra | M2 Ultra | 192GB | 76 | 70B-120B models | Fast |

A Mac Studio M3 Ultra with 512GB runs models like `deepseek-v3:236b` (quantized) entirely in memory — something that requires 4-8 NVIDIA A100s in a data center.

## Setup

```bash
pip install ollama-herd    # PyPI: https://pypi.org/project/ollama-herd/
herd                       # start the router on your Mac Studio
herd-node                  # run on additional Mac Studios or other devices
```

Devices discover each other automatically on your local network. No IP configuration needed.

### Add image generation

```bash
uv tool install mflux           # Flux models (~5s at 512px on M4 Ultra)
uv tool install diffusionkit    # Stable Diffusion 3/3.5
```

## Use your Mac Studio

### LLM inference — run the biggest models

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:11435/v1", api_key="not-needed")

# 120B model — runs smoothly on Mac Studio M4 Ultra (256GB)
response = client.chat.completions.create(
    model="gpt-oss:120b",
    messages=[{"role": "user", "content": "Explain the transformer architecture in detail"}],
    stream=True,
)
for chunk in response:
    print(chunk.choices[0].delta.content or "", end="")
```

### Image generation

```bash
# Flux via mflux — ~5s on Mac Studio M4 Ultra
curl -o image.png http://localhost:11435/api/generate-image \
  -H "Content-Type: application/json" \
  -d '{"model": "z-image-turbo", "prompt": "a Mac Studio on a minimalist desk", "width": 1024, "height": 1024}'

# Stable Diffusion 3 — ~9s on Mac Studio
curl -o sd3.png http://localhost:11435/api/generate-image \
  -H "Content-Type: application/json" \
  -d '{"model": "sd3-medium", "prompt": "cinematic landscape", "width": 1024, "height": 1024, "steps": 20}'
```

### Speech-to-text

```bash
curl http://localhost:11435/api/transcribe -F "file=@recording.wav" -F "model=qwen3-asr"
```

### Embeddings

```bash
curl http://localhost:11435/api/embed \
  -d '{"model": "nomic-embed-text", "input": "Mac Studio unified memory architecture"}'
```

## Recommended models for Mac Studio

| Mac Studio Config | Recommended models |
|-------------------|-------------------|
| M4 Max (128GB) | `llama3.3:70b`, `qwen3:72b`, `deepseek-r1:70b`, `codestral` |
| M4 Ultra (256GB) | `gpt-oss:120b`, `qwen3:110b`, `deepseek-r1:70b` + `codestral` simultaneously |
| M3 Ultra (512GB) | `deepseek-v3:236b` (quantized), multiple 70B models loaded at once |

The router's model recommender analyzes your Mac Studio's specs: `GET /dashboard/api/recommendations`.

## Multiple Mac Studios

```
Mac Studio #1 (M4 Ultra, 256GB)  ─┐
Mac Studio #2 (M4 Max, 128GB)    ├──→  Router (:11435)  ←──  Your apps
Mac Mini (32GB)                   ─┘
```

The router scores each device on 7 signals and routes every request to the best one. Big models go to the Mac Studio with the most memory; small models can go to the Mac Mini.

## Monitor your Mac Studio fleet

Dashboard at `http://localhost:11435/dashboard` — see loaded models, queue depths, thermal state, memory usage per device.

```bash
curl -s http://localhost:11435/fleet/status | python3 -m json.tool
curl -s http://localhost:11435/dashboard/api/health | python3 -m json.tool
```

## Full documentation

- [Agent Setup Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/agent-setup-guide.md)
- [Image Generation Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/image-generation.md)
- [API Reference](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/api-reference.md)

## Contribute

Ollama Herd is open source (MIT). Built by Mac Studio owners for Mac Studio owners:
- [Star on GitHub](https://github.com/geeks-accelerator/ollama-herd) — help others discover local AI
- [Open an issue](https://github.com/geeks-accelerator/ollama-herd/issues) — share your Mac Studio setup
- **PRs welcome** — `CLAUDE.md` gives AI agents full context. 412 tests, async Python.

## Guardrails

- **No automatic downloads** — model pulls require explicit user confirmation (some models are 70-230GB).
- **Model deletion requires explicit user confirmation.**
- **All requests stay local** — no data leaves your network.
- Never delete or modify files in `~/.fleet-manager/`.
