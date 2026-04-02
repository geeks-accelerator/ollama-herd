---
name: mlx-apple-silicon-mlx
description: MLX-powered local AI — run LLMs, Stable Diffusion, speech-to-text, and embeddings natively on Apple Silicon via MLX. Ollama uses MLX for LLM inference, mflux uses MLX for Flux image generation, DiffusionKit uses MLX for Stable Diffusion 3, and Qwen3-ASR uses MLX for transcription. One fleet router coordinates all four across Mac Studio, Mac Mini, MacBook Pro.
version: 1.0.0
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"bolt","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","pip"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["darwin"]}}
---

# MLX Local AI — Apple's ML Framework Powers Your Entire Fleet

Everything in this fleet runs on Apple's [MLX framework](https://github.com/ml-explore/mlx). LLM inference, image generation, speech-to-text, embeddings — all MLX-native, all optimized for Apple Silicon's unified memory architecture.

## The MLX stack

| Capability | Tool | MLX usage |
|-----------|------|-----------|
| **LLM inference** | Ollama | MLX backend for model loading and inference on Apple Silicon |
| **Image gen (Flux)** | mflux | Pure MLX implementation of Flux diffusion models |
| **Image gen (SD3)** | DiffusionKit | MLX-native Stable Diffusion 3 and 3.5 |
| **Speech-to-text** | Qwen3-ASR | MLX-accelerated audio transcription |
| **Embeddings** | Ollama | MLX backend for embedding model inference |

One router. One framework. Four modalities. All local.

## Setup

```bash
pip install ollama-herd    # PyPI: https://pypi.org/project/ollama-herd/
herd                       # start the router (port 11435)
herd-node                  # run on each device — finds the router automatically

# Install image generation backends
uv tool install mflux           # Flux models (~7s at 512px)
uv tool install diffusionkit    # Stable Diffusion 3/3.5
```

All tools leverage MLX for Metal-accelerated inference on Apple Silicon's GPU cores.

## LLM inference via MLX

Ollama runs models using MLX on Apple Silicon. Unified memory means the entire model stays in one address space — no PCIe bottleneck.

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:11435/v1", api_key="not-needed")
response = client.chat.completions.create(
    model="llama3.3:70b",
    messages=[{"role": "user", "content": "Explain MLX unified memory"}],
    stream=True,
)
for chunk in response:
    print(chunk.choices[0].delta.content or "", end="")
```

## Image generation via MLX

Both mflux and DiffusionKit are pure MLX implementations — no PyTorch, no CUDA.

```bash
# Flux via mflux (fastest)
curl -o flux.png http://localhost:11435/api/generate-image \
  -H "Content-Type: application/json" \
  -d '{"model": "z-image-turbo", "prompt": "a neural network visualization", "width": 1024, "height": 1024}'

# Stable Diffusion 3 via DiffusionKit
curl -o sd3.png http://localhost:11435/api/generate-image \
  -H "Content-Type: application/json" \
  -d '{"model": "sd3-medium", "prompt": "a circuit board landscape", "width": 1024, "height": 1024, "steps": 20}'
```

## Speech-to-text via MLX

Qwen3-ASR transcribes audio using MLX acceleration.

```bash
curl http://localhost:11435/api/transcribe \
  -F "file=@meeting.wav" \
  -F "model=qwen3-asr"
```

## Embeddings via MLX

Ollama embedding models run on the MLX backend.

```bash
curl http://localhost:11435/api/embed \
  -d '{"model": "nomic-embed-text", "input": "Apple MLX framework for machine learning"}'
```

## Why MLX matters for local AI

- **Unified memory** — model weights, activations, and KV cache share one memory pool. No CPU-GPU transfer overhead.
- **Metal acceleration** — MLX compiles to Metal shaders that run on Apple Silicon GPU cores (up to 80 on M3/M4 Ultra).
- **Lazy evaluation** — MLX only computes what's needed, reducing memory pressure.
- **Dynamic shapes** — no recompilation when input sizes change (unlike some CUDA frameworks).
- **Apple-maintained** — MLX is developed by Apple's ML research team, optimized for every chip generation.

## Fleet performance on Apple Silicon

| Chip | GPU Cores | Memory | LLM Sweet Spot | Image Gen |
|------|-----------|--------|----------------|-----------|
| M1 | 8 | 8-16GB | 3-7B models | Slow |
| M2 Pro | 19 | 32GB | 14B models | Capable |
| M3 Max | 40 | 128GB | 70B models | Fast |
| M4 Ultra | 80 | 256GB | 120B+ models | Very fast |

## Monitor your MLX fleet

```bash
# Fleet overview
curl -s http://localhost:11435/fleet/status | python3 -m json.tool

# Model recommendations based on your hardware
curl -s http://localhost:11435/dashboard/api/recommendations | python3 -m json.tool

# Health checks
curl -s http://localhost:11435/dashboard/api/health | python3 -m json.tool
```

Dashboard at `http://localhost:11435/dashboard` — see every node, every model, every queue in real time.

## Full documentation

- [Agent Setup Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/agent-setup-guide.md) — all 4 model types
- [Image Generation Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/image-generation.md) — 3 backends
- [API Reference](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/api-reference.md)

## Contribute

Ollama Herd is open source (MIT) and built on the MLX ecosystem. We welcome contributions:
- [Star on GitHub](https://github.com/geeks-accelerator/ollama-herd) — helps others discover the project
- [Open an issue](https://github.com/geeks-accelerator/ollama-herd/issues) — bug reports, feature requests, questions
- **AI agents welcome** — `CLAUDE.md` provides full architectural context. Fork, branch, PR.
- 444 tests, async Python, runs in under 40 seconds. Hard to break things.

## Guardrails

- **No automatic downloads** — all model pulls require explicit user confirmation.
- **Model deletion requires explicit user confirmation.**
- **All requests stay local** — no data leaves your network.
- Never delete or modify files in `~/.fleet-manager/`.
