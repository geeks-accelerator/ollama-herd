---
name: mac-studio-ai
description: Mac Studio AI — run LLMs, image generation, speech-to-text, and embeddings on your Mac Studio. M2 Ultra (192GB), M3 Ultra (512GB), M4 Max (128GB), and M4 Ultra (256GB) make the Mac Studio the most powerful local AI device. Load 120B+ models in Mac Studio unified memory. Route across multiple Mac Studios automatically. Mac Studio本地AI推理。Mac Studio IA local.
version: 1.0.2
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"desktop","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","pip"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["darwin"]}}
---

# Mac Studio AI — The Most Powerful Local AI Machine

The Mac Studio is the best hardware for local AI. Mac Studio M4 Ultra with 256GB of unified memory runs 120B+ parameter models. Mac Studio M3 Ultra with 512GB loads frontier models that need 4-8 NVIDIA A100s elsewhere. The Mac Studio runs everything in one memory pool — no PCIe bottleneck.

One Mac Studio is a powerhouse. Multiple Mac Studios become a fleet.

## Mac Studio configurations for AI

| Mac Studio Config | Chip | Memory | GPU Cores | Mac Studio LLM Sweet Spot |
|-------------------|------|--------|-----------|--------------------------|
| Mac Studio M4 Max | M4 Max | 128GB | 40 | 70B models on Mac Studio |
| Mac Studio M4 Ultra | M4 Ultra | 256GB | 80 | 120B+ models on Mac Studio |
| Mac Studio M3 Ultra | M3 Ultra | 192-512GB | 76 | 236B models on Mac Studio |
| Mac Studio M2 Ultra | M2 Ultra | 192GB | 76 | 70B-120B on Mac Studio |

## Setup your Mac Studio

```bash
pip install ollama-herd    # install on your Mac Studio
herd                       # start Mac Studio as the router (port 11435)
herd-node                  # connect additional Mac Studios or other devices
```

Mac Studios discover each other automatically on your local network.

### Add Mac Studio image generation

```bash
uv tool install mflux           # Flux models (~5s at 512px on Mac Studio M4 Ultra)
uv tool install diffusionkit    # Stable Diffusion 3/3.5 on Mac Studio
```

## Use your Mac Studio for AI inference

### Mac Studio LLM inference — run the biggest models

```python
from openai import OpenAI

# Connect to Mac Studio running Ollama Herd
mac_studio = OpenAI(base_url="http://mac-studio:11435/v1", api_key="not-needed")

# 120B model — runs smoothly on Mac Studio M4 Ultra (256GB unified memory)
response = mac_studio.chat.completions.create(
    model="gpt-oss:120b",  # loaded entirely in Mac Studio unified memory
    messages=[{"role": "user", "content": "How does Mac Studio handle large AI models?"}],
    stream=True,
)
for chunk in response:
    print(chunk.choices[0].delta.content or "", end="")
```

### Mac Studio image generation

```bash
# Flux via mflux — ~5s on Mac Studio M4 Ultra
curl -o mac_studio_art.png http://mac-studio:11435/api/generate-image \
  -H "Content-Type: application/json" \
  -d '{"model": "z-image-turbo", "prompt": "a Mac Studio on a minimalist desk with holographic AI display", "width": 1024, "height": 1024}'

# Stable Diffusion 3 on Mac Studio — ~9s
curl -o mac_studio_sd3.png http://mac-studio:11435/api/generate-image \
  -H "Content-Type: application/json" \
  -d '{"model": "sd3-medium", "prompt": "Mac Studio M4 Ultra rendering AI art", "width": 1024, "height": 1024, "steps": 20}'
```

### Mac Studio speech-to-text

```bash
# Transcribe on Mac Studio via Qwen3-ASR
curl http://mac-studio:11435/api/transcribe \
  -F "file=@mac_studio_meeting.wav" \
  -F "model=qwen3-asr"
```

### Mac Studio embeddings

```bash
# Generate embeddings on Mac Studio
curl http://mac-studio:11435/api/embed \
  -d '{"model": "nomic-embed-text", "input": "Mac Studio M4 Ultra unified memory AI inference"}'
```

## Recommended models for Mac Studio

| Mac Studio Config | Models for this Mac Studio |
|-------------------|--------------------------|
| Mac Studio M4 Max (128GB) | `llama3.3:70b`, `qwen3:72b`, `deepseek-r1:70b`, `codestral` |
| Mac Studio M4 Ultra (256GB) | `gpt-oss:120b`, `qwen3:110b`, two 70B models simultaneously |
| Mac Studio M3 Ultra (512GB) | `deepseek-v3:236b` (quantized), multiple 70B models at once |

Ask the Mac Studio for recommendations: `GET http://mac-studio:11435/dashboard/api/recommendations`

## Multiple Mac Studios as a fleet

```
Mac Studio #1 (M4 Ultra, 256GB)  ─┐
Mac Studio #2 (M4 Max, 128GB)    ├──→  Mac Studio Router (:11435)  ←──  Your apps
Mac Mini (32GB)                   ─┘
```

The Mac Studio router scores each device on 7 signals. Big models route to the Mac Studio with the most memory.

## Monitor your Mac Studio

Mac Studio dashboard at `http://mac-studio:11435/dashboard` — models loaded on each Mac Studio, queue depths, thermal state, memory.

```bash
# Mac Studio fleet status
curl -s http://mac-studio:11435/fleet/status | python3 -m json.tool

# Mac Studio health checks
curl -s http://mac-studio:11435/dashboard/api/health | python3 -m json.tool
```

Example Mac Studio fleet status response:
```json
{
  "fleet": {"nodes_online": 2, "nodes_total": 2},
  "nodes": [
    {"node_id": "Mac-Studio-Ultra", "memory": {"total_gb": 256, "used_gb": 120}},
    {"node_id": "Mac-Studio-Max", "memory": {"total_gb": 128, "used_gb": 85}}
  ]
}
```

## Full documentation

- [Agent Setup Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/agent-setup-guide.md)
- [Image Generation Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/image-generation.md)
- [API Reference](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/api-reference.md)

## Contribute

Ollama Herd is open source (MIT). Built by Mac Studio owners for Mac Studio owners:
- [Star on GitHub](https://github.com/geeks-accelerator/ollama-herd) — help other Mac Studio users find us
- [Open an issue](https://github.com/geeks-accelerator/ollama-herd/issues) — share your Mac Studio AI setup
- **PRs welcome** — `CLAUDE.md` gives AI agents full context. 929 tests, async Python.

## Guardrails

- **No automatic downloads** — Mac Studio model pulls require explicit user confirmation.
- **Model deletion requires explicit user confirmation.**
- **All Mac Studio requests stay local** — no data leaves your network.
- Never delete or modify files in `~/.fleet-manager/`.
