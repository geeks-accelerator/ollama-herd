---
name: homelab-ai
description: Home lab AI — turn your spare Macs into a local AI cluster. LLM inference, image generation, speech-to-text, and embeddings across Mac Studio, Mac Mini, MacBook Pro, and Mac Pro. Zero-config mDNS discovery, real-time dashboard, 7-signal scoring. No cloud, no Docker, no Kubernetes. The home lab AI setup that just works.
version: 1.0.0
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"house","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","pip"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["darwin","linux"]}}
---

# Home Lab AI — Your Spare Macs Are a Cluster

You have Macs sitting around. A Mac Mini in the closet. A MacBook Pro on the desk. Maybe a Mac Studio doing light work. Together, they have more compute than most cloud instances — you just need software that treats them as one system.

Ollama Herd turns them into a local AI cluster. One endpoint, zero config, four model types.

## What you get

```
Mac Mini (32GB)    ─┐
MacBook Pro (64GB)  ├──→  Router (:11435)  ←──  Your apps / agents / scripts
Mac Studio (256GB) ─┘
```

- **LLM inference** — Llama, Qwen, DeepSeek, Phi, Mistral, Gemma
- **Image generation** — Stable Diffusion 3, Flux, z-image-turbo
- **Speech-to-text** — Qwen3-ASR transcription
- **Embeddings** — nomic-embed-text, mxbai-embed for RAG

All routed to the best available device automatically.

## Setup (5 minutes)

### On every Mac:

```bash
pip install ollama-herd
```

### Pick one Mac as the router:

```bash
herd
```

### On every other Mac:

```bash
herd-node
```

That's it. Devices discover each other automatically on your local network. No IP addresses, no config files, no Docker, no Kubernetes.

### Optional: add image generation

```bash
uv tool install mflux           # Flux models (fastest)
uv tool install diffusionkit    # Stable Diffusion 3/3.5
```

## Use your home lab

### Chat with an LLM

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:11435/v1", api_key="not-needed")
response = client.chat.completions.create(
    model="llama3.3:70b",
    messages=[{"role": "user", "content": "How do I set up a home NAS?"}],
    stream=True,
)
for chunk in response:
    print(chunk.choices[0].delta.content or "", end="")
```

### Generate an image

```bash
curl -o homelab.png http://localhost:11435/api/generate-image \
  -H "Content-Type: application/json" \
  -d '{"model": "z-image-turbo", "prompt": "a cozy home lab with servers and RGB lighting", "width": 1024, "height": 1024}'
```

### Transcribe a meeting

```bash
curl http://localhost:11435/api/transcribe -F "file=@standup.wav" -F "model=qwen3-asr"
```

### Build a knowledge base

```bash
curl http://localhost:11435/api/embed \
  -d '{"model": "nomic-embed-text", "input": "home lab networking best practices"}'
```

## What runs where

The router scores each device on 7 signals and picks the best one:

| Signal | What it measures |
|--------|-----------------|
| Thermal state | Is the model already loaded (hot) or needs cold-loading? |
| Memory fit | Does the device have enough RAM for this model? |
| Queue depth | Is the device already busy with other requests? |
| Wait time | How long has the request been waiting? |
| Role affinity | Big models prefer big machines, small models prefer small ones |
| Availability trend | Is this device reliably available at this time of day? |
| Context fit | Does the loaded context window fit the request? |

You don't manage any of this. The router handles it.

## The home lab dashboard

Open `http://localhost:11435/dashboard` in your browser:

- **Fleet Overview** — see every device, loaded models, queue depths, health status
- **Trends** — requests per hour, latency, token throughput over 24h-7d
- **Health** — 11 automated checks with recommendations
- **Recommendations** — optimal model mix per device based on your hardware

## Recommended models by device

| Device | RAM | Start with |
|--------|-----|-----------|
| MacBook Air (8GB) | 8GB | `phi4-mini`, `gemma3:1b` |
| Mac Mini (16GB) | 16GB | `phi4`, `gemma3:4b`, `nomic-embed-text` |
| Mac Mini (32GB) | 32GB | `qwen3:14b`, `deepseek-r1:14b` |
| MacBook Pro (64GB) | 64GB | `qwen3:32b`, `codestral`, `z-image-turbo` |
| Mac Studio (128GB) | 128GB | `llama3.3:70b`, `qwen3:72b` |
| Mac Studio (256GB) | 256GB | `gpt-oss:120b`, `sd3.5-large` |

The router's model recommender suggests the optimal mix: `GET /dashboard/api/recommendations`.

## Works with everything

The fleet exposes an OpenAI-compatible API. Any tool that works with OpenAI works with your home lab:

| Tool | How to connect |
|------|---------------|
| **Open WebUI** | Set Ollama URL to `http://router-ip:11435` |
| **Aider** | `aider --openai-api-base http://router-ip:11435/v1` |
| **Continue.dev** | Base URL: `http://router-ip:11435/v1` |
| **LangChain** | `ChatOpenAI(base_url="http://router-ip:11435/v1")` |
| **CrewAI** | Set `OPENAI_API_BASE=http://router-ip:11435/v1` |
| **Any OpenAI SDK** | Base URL: `http://router-ip:11435/v1`, API key: any string |

## Full documentation

- [Agent Setup Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/agent-setup-guide.md) — all 4 model types
- [Image Generation Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/image-generation.md) — 3 image backends
- [Configuration Reference](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/configuration-reference.md) — 30+ env vars
- [Troubleshooting](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/troubleshooting.md) — common issues

## Contribute

Ollama Herd is open source (MIT) and built by home lab enthusiasts for home lab enthusiasts:
- [Star on GitHub](https://github.com/geeks-accelerator/ollama-herd) — help other home labbers find us
- [Open an issue](https://github.com/geeks-accelerator/ollama-herd/issues) — share your setup, report bugs, request features
- **PRs welcome** — from humans and AI agents. `CLAUDE.md` gives full context.
- Built by twin brothers in Alaska who run their own Mac fleet. Come say hi.

## Guardrails

- **No automatic downloads** — model pulls require explicit user confirmation. Some models are 70GB+.
- **Model deletion requires explicit user confirmation.**
- **All requests stay local** — no data leaves your home network.
- Never delete or modify files in `~/.fleet-manager/`.
- No cloud dependencies — your home lab works offline after initial model downloads.
