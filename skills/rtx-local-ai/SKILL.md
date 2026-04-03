---
name: rtx-local-ai
description: RTX Local AI — turn your gaming PC into a local AI server. RTX 4090, RTX 4080, RTX 4070, RTX 3090 run Llama, Qwen, DeepSeek, Phi, Mistral locally. Gaming PC AI inference with NVIDIA RTX GPUs via Ollama Herd. No cloud costs — your RTX GPU is the AI server. RTX本地AI推理。RTX IA local en tu PC gaming.
version: 1.0.0
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"joystick","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","pip","nvidia-smi"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["linux","windows"]}}
---

# RTX Local AI — Your Gaming PC Is an AI Server

Your RTX GPU already runs games at 4K. Now run LLMs at the same speed. An RTX 4090 with 24GB vRAM loads 70B parameter models. An RTX 4080 with 16GB runs 14B-34B models fast. Stack multiple RTX PCs into a fleet and route AI requests to the best available RTX GPU.

## RTX GPU model guide

| RTX GPU | vRAM | Best RTX models | RTX performance |
|---------|------|-----------------|-----------------|
| **RTX 4090** | 24GB | `llama3.3:70b` (Q4), `qwen3.5:32b`, `deepseek-r1:32b` | RTX king — 70B models at speed |
| **RTX 4080** | 16GB | `qwen3.5:14b`, `phi4`, `codestral`, `mistral-nemo` | RTX sweet spot for most tasks |
| **RTX 4070 Ti** | 12GB | `phi4`, `gemma3:12b`, `llama3.2:3b` | Budget RTX with solid performance |
| **RTX 4070** | 12GB | `phi4-mini`, `gemma3:4b`, `qwen3.5:7b` | Entry-level RTX for local AI |
| **RTX 3090** | 24GB | Same as RTX 4090 | Last-gen RTX, still great for AI |
| **RTX 3080** | 10GB | `phi4-mini`, `llama3.2:3b` | Older RTX, lightweight models |

> **Cross-platform:** RTX Local AI works on Windows and Linux. Most RTX gaming PCs run Windows — that's fine.

## Setup your RTX AI server

```bash
pip install ollama-herd    # PyPI: https://pypi.org/project/ollama-herd/
```

### Single RTX gaming PC

```bash
herd         # start the RTX router
herd-node    # register this RTX machine
```

### Multiple RTX PCs (RTX fleet)

On one RTX PC (the router):
```bash
herd
herd-node
```

On every other RTX PC:
```bash
herd-node    # auto-discovers the RTX router via mDNS
```

That's it. Every RTX PC in your fleet now shares AI workload.

## Use your RTX for AI

### OpenAI SDK

```python
from openai import OpenAI

# Your RTX GPU serves this
rtx_client = OpenAI(base_url="http://localhost:11435/v1", api_key="not-needed")

# RTX 4090 handles 70B models easily
response = rtx_client.chat.completions.create(
    model="llama3.3:70b",
    messages=[{"role": "user", "content": "Write a game engine ECS system in Rust"}],
    stream=True,
)
for chunk in response:
    print(chunk.choices[0].delta.content or "", end="")
```

### RTX-powered code generation

```python
# Your RTX runs Codestral for code
response = rtx_client.chat.completions.create(
    model="codestral",
    messages=[{"role": "user", "content": "Optimize this HLSL shader for RTX ray tracing"}],
)
print(response.choices[0].message.content)
```

### curl

```bash
# RTX inference
curl http://localhost:11435/api/chat -d '{
  "model": "qwen3.5:32b",
  "messages": [{"role": "user", "content": "Explain GPU memory architecture"}],
  "stream": false
}'
```

## RTX vs cloud — cost comparison

| Option | Monthly cost | RTX advantage |
|--------|-------------|---------------|
| RTX 4090 (one-time $1,599) | $0/month | Your RTX runs unlimited inference forever |
| Cloud A100 (AWS) | $3.06/hour (~$2,200/month) | RTX pays for itself in weeks |
| OpenAI GPT-4o API | ~$100-500/month at scale | RTX has zero per-token cost |
| RTX 4080 (one-time $1,199) | $0/month | Even budget RTX beats cloud |

## Monitor your RTX fleet

```bash
# RTX fleet overview
curl -s http://localhost:11435/fleet/status | python3 -m json.tool

# Check RTX GPU health
curl -s http://localhost:11435/dashboard/api/health | python3 -m json.tool

# Models loaded on RTX GPUs
curl -s http://localhost:11435/api/ps | python3 -m json.tool
```

Dashboard at `http://localhost:11435/dashboard` — live RTX performance monitoring.

## Optimize Ollama for RTX

Keep models loaded in your RTX vRAM permanently:

```powershell
# Windows (most RTX gaming PCs)
[System.Environment]::SetEnvironmentVariable("OLLAMA_KEEP_ALIVE", "-1", "User")
[System.Environment]::SetEnvironmentVariable("OLLAMA_MAX_LOADED_MODELS", "-1", "User")
# Restart Ollama from system tray
```

```bash
# Linux
sudo systemctl edit ollama
# Add: Environment="OLLAMA_KEEP_ALIVE=-1"
# Add: Environment="OLLAMA_MAX_LOADED_MODELS=-1"
sudo systemctl restart ollama
```

## Also available on your RTX fleet

### Image generation
```bash
curl http://localhost:11435/api/generate-image \
  -d '{"model": "z-image-turbo", "prompt": "RTX-powered cyberpunk cityscape", "width": 1024, "height": 1024}'
```

### Embeddings
```bash
curl http://localhost:11435/api/embed \
  -d '{"model": "nomic-embed-text", "input": "NVIDIA RTX local AI inference"}'
```

## Full documentation

- [Agent Setup Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/agent-setup-guide.md)
- [API Reference](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/api-reference.md)

## Contribute

Ollama Herd is open source (MIT). RTX gamers and AI builders welcome:
- [Star on GitHub](https://github.com/geeks-accelerator/ollama-herd)
- [Open an issue](https://github.com/geeks-accelerator/ollama-herd/issues)

## Guardrails

- **RTX model downloads require explicit user confirmation** — models range from 1GB to 400GB+.
- **RTX model deletion requires explicit user confirmation.**
- Never delete or modify files in `~/.fleet-manager/`.
- No models are downloaded automatically — all pulls are user-initiated or require opt-in.
