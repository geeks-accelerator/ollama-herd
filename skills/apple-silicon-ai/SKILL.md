---
name: apple-silicon-ai
description: Apple Silicon AI — run LLMs, image generation, speech-to-text, and embeddings on Mac Studio, Mac Mini, MacBook Pro, and Mac Pro. Turn your Apple Silicon devices into a local AI fleet. M1, M2, M3, M4 Max and Ultra chips with unified memory make these machines ideal for local inference. No cloud APIs, no GPU rentals — your Macs are the cluster.
version: 1.0.1
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"apple","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","pip"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["darwin"]}}
---

# Apple Silicon AI — Your Macs Are the Cluster

Turn your Mac Studio, Mac Mini, MacBook Pro, or Mac Pro into a local AI fleet. One endpoint routes LLM inference, image generation, speech-to-text, and embeddings across every Apple Silicon device on your network.

No cloud APIs. No GPU rentals. No Docker. Your M1/M2/M3/M4 chips with unified memory are already better inference hardware than most cloud instances — you just need software that treats them as a fleet.

## Why Apple Silicon for AI

| Chip | Unified Memory | LLM Sweet Spot | Image Gen | Notes |
|------|---------------|----------------|-----------|-------|
| M1 (8GB) | 8GB | 7B models | Slow | Entry level, good for small models |
| M1 Pro/Max (32-64GB) | 32-64GB | 14B-32B | Capable | MacBook Pro, solid mid-range |
| M2 Ultra (192GB) | 192GB | 70B-120B | Fast | Mac Studio/Pro, runs large models in memory |
| M3 Max (128GB) | 128GB | 70B | Fast | Latest MacBook Pro, great single-node |
| M4 Max (128GB) | 128GB | 70B | Fast | Mac Studio, newest generation |
| M4 Ultra (256GB) | 256GB | 120B+ | Very fast | Mac Studio/Pro, largest models fully in memory |

Unified memory means the entire model stays in one address space — no PCIe bottleneck, no CPU-GPU transfer overhead. A Mac Studio with M4 Ultra and 256GB runs 120B parameter models that would need multiple NVIDIA A100s.

## Setup

### 1. Install on every Mac

```bash
pip install ollama-herd    # PyPI: https://pypi.org/project/ollama-herd/
```

### 2. Start the router (pick one Mac)

```bash
herd    # starts on port 11435
```

### 3. Start the node agent on every Mac

```bash
herd-node    # automatically finds the router on your local network
```

That's it. Nodes discover the router automatically on your local network. No IP addresses to configure, no config files, no manual registration. For explicit connection, use `herd-node --router-url http://<router-ip>:11435`.

### How it works

```
MacBook Pro (M3 Max, 64GB)  ─┐
Mac Mini (M4, 32GB)          ├──→  Router (:11435)  ←──  Your apps / agents
Mac Studio (M4 Ultra, 256GB) ─┘
```

The router scores each device on 7 signals and routes every request to the best available Mac — thermal state, memory fit, queue depth, and more.

## LLM inference

Run Llama, Qwen, DeepSeek, Phi, Mistral, Gemma, and any Ollama model across your Mac fleet.

### OpenAI-compatible API

```bash
curl http://localhost:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama3.3:70b",
    "messages": [{"role": "user", "content": "Explain unified memory"}]
  }'
```

### Ollama-compatible API

```bash
curl http://localhost:11435/api/chat \
  -d '{"model": "qwen3:32b", "messages": [{"role": "user", "content": "Hello"}]}'
```

### Works with any OpenAI SDK

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:11435/v1", api_key="unused")
response = client.chat.completions.create(
    model="deepseek-r1:70b",
    messages=[{"role": "user", "content": "Optimize this function"}]
)
```

## Image generation (mflux on Apple Silicon)

Generate images using MLX-native Flux models. Runs natively on Apple Silicon — no CUDA, no cloud.

```bash
curl http://localhost:11435/api/generate-image \
  -d '{"prompt": "a Mac Studio on a desk, photorealistic", "model": "z-image-turbo", "width": 512, "height": 512}'
```

Performance on Apple Silicon:
- **Mac Studio M4 Ultra**: ~5s at 512px, ~14s at 1024px
- **MacBook Pro M3 Max**: ~7s at 512px, ~18s at 1024px
- **Mac Mini M4**: ~12s at 512px, ~30s at 1024px

## Speech-to-text (Qwen ASR on Apple Silicon)

Transcribe audio locally using Qwen3-ASR via MLX. Meetings, voice notes, podcasts — no cloud, no Whisper API costs.

```bash
curl http://localhost:11435/api/transcribe \
  -F "file=@meeting.wav" \
  -F "model=qwen3-asr"
```

Supports WAV, MP3, M4A, FLAC. ~2s for a 30-second clip on M4 Ultra.

## Embeddings

Embed documents across your fleet using Ollama embedding models (nomic-embed-text, mxbai-embed-large, snowflake-arctic-embed).

```bash
curl http://localhost:11435/api/embed \
  -d '{"model": "nomic-embed-text", "input": "Apple Silicon unified memory architecture"}'
```

Batch thousands of documents across nodes instead of bottlenecking on one Mac.

## Fleet monitoring

### Dashboard

Open `http://localhost:11435/dashboard` in your browser. See every Mac in your fleet: models loaded, queue depth, thermal state, memory usage, and health status.

### Fleet status API

```bash
curl http://localhost:11435/fleet/status
```

Returns every node with hardware specs, loaded models, image/STT capabilities, and health metrics.

### Health checks

```bash
curl http://localhost:11435/dashboard/api/health
```

11 automated checks: offline nodes, memory pressure, thermal throttling, VRAM fallbacks, error rates, and more.

## Recommended model placement by hardware

| Your Mac | RAM | Recommended models |
|----------|-----|-------------------|
| Mac Mini (16GB) | 16GB | llama3.2:3b, phi4-mini, nomic-embed-text |
| Mac Mini (32GB) | 32GB | qwen3:14b, deepseek-r1:14b, llama3.3:8b |
| MacBook Pro (36-64GB) | 36-64GB | qwen3:32b, deepseek-r1:32b, codestral |
| Mac Studio (128GB) | 128GB | llama3.3:70b, qwen3:72b, deepseek-r1:70b |
| Mac Studio/Pro (192-256GB) | 192-256GB | qwen3:110b, deepseek-v3:236b (quantized) |

The router's model recommender analyzes your fleet hardware and suggests the optimal model mix: `GET /dashboard/api/model-recommendations`.

## Full documentation

- [Agent Setup Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/agent-setup-guide.md) — complete setup for all 4 model types
- [Configuration Reference](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/configuration-reference.md) — all 30+ environment variables
- [API Reference](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/api-reference.md) — all endpoints with request/response schemas
- [Troubleshooting](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/troubleshooting.md) — common issues and fixes

## Guardrails

- **No automatic downloads**: No models are downloaded during installation. Model pulls are always user-initiated and require explicit confirmation. Downloads range from 2GB to 70GB+ depending on model size.
- **Model deletion requires confirmation**: Never remove models without explicit user approval.
- **All requests stay local**: No data leaves your local network — all inference happens on your Macs.
- **No API keys**: No accounts, no tokens, no cloud dependencies.
- **No external network access**: The router and nodes communicate only on your local network. No telemetry, no cloud callbacks.
- **Read-only local state**: The only local files created are `~/.fleet-manager/latency.db` (routing metrics) and `~/.fleet-manager/logs/herd.jsonl` (structured logs). Never delete or modify these files without user confirmation.
