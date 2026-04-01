---
name: apple-silicon-ai
description: Apple Silicon AI — run LLMs, image generation, speech-to-text, and embeddings on Mac Studio, Mac Mini, MacBook Pro, and Mac Pro. Turn your Apple Silicon devices into a local AI fleet. M1, M2, M3, M4 Max and Ultra chips with unified memory make these machines ideal for local inference. No cloud APIs, no GPU rentals — your Macs are the cluster. 苹果芯片AI本地推理集群。IA Apple Silicon para inferencia local.
version: 1.0.2
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"apple","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","pip"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["darwin"]}}
---

# Apple Silicon AI — Your Macs Are the Cluster

Turn your Mac Studio, Mac Mini, MacBook Pro, or Mac Pro into a local Apple Silicon AI fleet. One endpoint routes LLM inference, image generation, speech-to-text, and embeddings across every Apple Silicon device on your network.

No cloud APIs. No GPU rentals. No Docker. Your Apple Silicon M1/M2/M3/M4 chips with unified memory are already better inference hardware than most cloud instances — you just need software that treats them as an Apple Silicon fleet.

## Why Apple Silicon for AI

Apple Silicon unified memory keeps the entire model in one address space — no PCIe bottleneck, no CPU-GPU transfer overhead. A Mac Studio with M4 Ultra and 256GB runs 120B parameter models that would need multiple NVIDIA A100s. That is the Apple Silicon advantage.

| Apple Silicon Chip | Unified Memory | LLM Sweet Spot | Apple Silicon Image Gen | Notes |
|------|---------------|----------------|-----------|-------|
| M1 (8GB) | 8GB | 7B models | Slow | Entry-level Apple Silicon |
| M1 Pro/Max (32-64GB) | 32-64GB | 14B-32B | Capable | Apple Silicon MacBook Pro |
| M2 Ultra (192GB) | 192GB | 70B-120B | Fast | Apple Silicon Mac Studio/Pro |
| M3 Max (128GB) | 128GB | 70B | Fast | Latest Apple Silicon MacBook Pro |
| M4 Max (128GB) | 128GB | 70B | Fast | Apple Silicon Mac Studio, newest gen |
| M4 Ultra (256GB) | 256GB | 120B+ | Very fast | Apple Silicon Mac Studio/Pro, largest models |

## Apple Silicon Fleet Setup

### 1. Install on every Apple Silicon Mac

```bash
pip install ollama-herd    # Apple Silicon optimized inference router
```

### 2. Start the Apple Silicon router (pick one Mac)

```bash
herd    # starts Apple Silicon router on port 11435
```

### 3. Start the Apple Silicon node agent on every Mac

```bash
herd-node    # Apple Silicon node auto-discovers the router
```

That's it. Apple Silicon nodes discover the router automatically on your local network. No IP addresses to configure, no config files. For explicit connection, use `herd-node --router-url http://<router-ip>:11435`.

### How Apple Silicon routing works

```
MacBook Pro (M3 Max, 64GB)  ─┐
Mac Mini (M4, 32GB)          ├──→  Apple Silicon Router (:11435)  ←──  Your apps
Mac Studio (M4 Ultra, 256GB) ─┘
```

The Apple Silicon router scores each device on 7 signals and routes every request to the best available Mac — thermal state, memory fit, queue depth, and more.

## Apple Silicon LLM Inference

Run Llama, Qwen, DeepSeek, Phi, Mistral, Gemma, and any Ollama model across your Apple Silicon fleet.

### OpenAI-compatible API (Apple Silicon backend)

```bash
curl http://localhost:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama3.3:70b",
    "messages": [{"role": "user", "content": "Explain Apple Silicon unified memory architecture"}]
  }'
```

### Ollama-compatible API

```bash
curl http://localhost:11435/api/chat \
  -d '{"model": "qwen3:32b", "messages": [{"role": "user", "content": "Compare Apple Silicon M4 vs M3 for AI inference"}]}'
```

### Apple Silicon Python Client

```python
from openai import OpenAI
# Apple Silicon inference client
apple_silicon_client = OpenAI(base_url="http://localhost:11435/v1", api_key="unused")
apple_silicon_response = apple_silicon_client.chat.completions.create(
    model="deepseek-r1:70b",
    messages=[{"role": "user", "content": "Optimize this function for Apple Silicon"}]
)
```

## Apple Silicon Image Generation (mflux)

Generate images using MLX-native Flux models. Runs natively on Apple Silicon — no CUDA, no cloud.

```bash
curl http://localhost:11435/api/generate-image \
  -d '{"prompt": "Apple Silicon Mac Studio rendering AI art, photorealistic", "model": "z-image-turbo", "width": 512, "height": 512}'
```

Apple Silicon image generation performance:
- **Mac Studio M4 Ultra**: ~5s at 512px, ~14s at 1024px
- **MacBook Pro M3 Max**: ~7s at 512px, ~18s at 1024px
- **Mac Mini M4**: ~12s at 512px, ~30s at 1024px

## Apple Silicon Speech-to-Text (Qwen ASR)

Transcribe audio locally on Apple Silicon using Qwen3-ASR via MLX. Meetings, voice notes, podcasts — no cloud, no Whisper API costs.

```bash
curl http://localhost:11435/api/transcribe \
  -F "file=@apple_silicon_meeting.wav" \
  -F "model=qwen3-asr"
```

Supports WAV, MP3, M4A, FLAC. ~2s for a 30-second clip on Apple Silicon M4 Ultra.

## Apple Silicon Embeddings

Embed documents across your Apple Silicon fleet using Ollama embedding models (nomic-embed-text, mxbai-embed-large, snowflake-arctic-embed).

```bash
curl http://localhost:11435/api/embed \
  -d '{"model": "nomic-embed-text", "input": "Apple Silicon unified memory architecture for AI inference"}'
```

Batch thousands of documents across Apple Silicon nodes instead of bottlenecking on one Mac.

## Apple Silicon Fleet Monitoring

### Dashboard

Open `http://localhost:11435/dashboard` — see every Apple Silicon Mac in your fleet: models loaded, queue depth, thermal state, memory usage, and health status.

### Apple Silicon Fleet Status API

```bash
curl http://localhost:11435/fleet/status
```

Returns every Apple Silicon node with hardware specs, loaded models, image/STT capabilities, and health metrics.

### Apple Silicon Health Checks

```bash
curl http://localhost:11435/dashboard/api/health
```

11 automated checks: offline Apple Silicon nodes, memory pressure, thermal throttling, VRAM fallbacks, error rates, and more.

## Recommended Models by Apple Silicon Hardware

| Your Apple Silicon Mac | RAM | Recommended models |
|----------|-----|-------------------|
| Mac Mini (16GB) | 16GB | llama3.2:3b, phi4-mini, nomic-embed-text |
| Mac Mini (32GB) | 32GB | qwen3:14b, deepseek-r1:14b, llama3.3:8b |
| MacBook Pro (36-64GB) | 36-64GB | qwen3:32b, deepseek-r1:32b, codestral |
| Mac Studio (128GB) | 128GB | llama3.3:70b, qwen3:72b, deepseek-r1:70b |
| Mac Studio/Pro (192-256GB) | 192-256GB | qwen3:110b, deepseek-v3:236b (quantized) |

The Apple Silicon router's model recommender analyzes your fleet hardware and suggests the optimal model mix: `GET /dashboard/api/model-recommendations`.

## Full documentation

- [Agent Setup Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/agent-setup-guide.md) — complete Apple Silicon setup for all 4 model types
- [Configuration Reference](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/configuration-reference.md) — all 30+ environment variables
- [API Reference](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/api-reference.md) — all endpoints with request/response schemas
- [Troubleshooting](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/troubleshooting.md) — common Apple Silicon issues and fixes

## Guardrails

- **No automatic downloads**: Apple Silicon model pulls are always user-initiated and require explicit confirmation. Downloads range from 2GB to 70GB+ depending on model size.
- **Model deletion requires confirmation**: Never remove models from Apple Silicon nodes without explicit user approval.
- **All Apple Silicon requests stay local**: No data leaves your local network — all inference happens on your Apple Silicon Macs.
- **No API keys**: No accounts, no tokens, no cloud dependencies for your Apple Silicon fleet.
- **No external network access**: The Apple Silicon router and nodes communicate only on your local network. No telemetry, no cloud callbacks.
- **Read-only local state**: The only local files created are `~/.fleet-manager/latency.db` (Apple Silicon routing metrics) and `~/.fleet-manager/logs/herd.jsonl` (structured logs). Never delete or modify these files without user confirmation.
