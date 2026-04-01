---
name: homelab-ai
description: Home lab AI — turn your spare Macs into a local AI home lab cluster. LLM inference, image generation, speech-to-text, and embeddings across Mac Studio, Mac Mini, MacBook Pro, and Mac Pro. Zero-config mDNS discovery, real-time dashboard, 7-signal scoring. No cloud, no Docker, no Kubernetes. The home lab AI setup that just works. 家庭实验室AI本地推理集群。Laboratorio IA para inferencia local en casa.
version: 1.0.1
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"house","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","pip"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["darwin","linux"]}}
---

# Home Lab AI — Your Spare Macs Are a Cluster

You have Macs sitting around your home lab. A Mac Mini in the closet. A MacBook Pro on the desk. Maybe a Mac Studio doing light work. Together, your home lab has more compute than most cloud instances — you just need software that treats them as one home lab system.

Ollama Herd turns your home lab into a local AI cluster. One home lab endpoint, zero config, four model types.

## What your home lab gets

```
Mac Mini (32GB)    ─┐
MacBook Pro (64GB)  ├──→  Home Lab Router (:11435)  ←──  Your apps / agents
Mac Studio (256GB) ─┘
```

- **Home lab LLM inference** — Llama, Qwen, DeepSeek, Phi, Mistral, Gemma
- **Home lab image generation** — Stable Diffusion 3, Flux, z-image-turbo
- **Home lab speech-to-text** — Qwen3-ASR transcription
- **Home lab embeddings** — nomic-embed-text, mxbai-embed for RAG

All routed to the best available home lab device automatically.

## Home Lab Setup (5 minutes)

### On every home lab Mac:

```bash
pip install ollama-herd    # Home lab AI router
```

### Pick one home lab Mac as the router:

```bash
herd    # starts the home lab router
```

### On every other home lab Mac:

```bash
herd-node    # joins the home lab fleet automatically
```

That's it. Home lab devices discover each other automatically on your local network. No IP addresses, no config files, no Docker, no Kubernetes.

### Optional: add home lab image generation

```bash
uv tool install mflux           # Flux models (fastest for home labs)
uv tool install diffusionkit    # Stable Diffusion 3/3.5
```

## Use Your Home Lab

### Home lab LLM chat

```python
from openai import OpenAI

# Home lab inference client
homelab_client = OpenAI(base_url="http://localhost:11435/v1", api_key="not-needed")
homelab_response = homelab_client.chat.completions.create(
    model="llama3.3:70b",
    messages=[{"role": "user", "content": "How do I set up a home lab NAS?"}],
    stream=True,
)
for chunk in homelab_response:
    print(chunk.choices[0].delta.content or "", end="")
```

### Home lab image generation

```bash
curl -o homelab_output.png http://localhost:11435/api/generate-image \
  -H "Content-Type: application/json" \
  -d '{"model": "z-image-turbo", "prompt": "a cozy home lab with servers and RGB lighting", "width": 1024, "height": 1024}'
```

### Home lab transcription

```bash
curl http://localhost:11435/api/transcribe -F "file=@homelab_standup.wav" -F "model=qwen3-asr"
```

### Home lab knowledge base

```bash
curl http://localhost:11435/api/embed \
  -d '{"model": "nomic-embed-text", "input": "home lab networking and AI inference best practices"}'
```

## How the Home Lab Routes Requests

The home lab router scores each device on 7 signals and picks the best one:

| Home Lab Signal | What it measures |
|--------|-----------------|
| Thermal state | Is the home lab model already loaded (hot) or needs cold-loading? |
| Memory fit | Does the home lab device have enough RAM for this model? |
| Queue depth | Is the home lab device already busy with other requests? |
| Wait time | How long has the home lab request been waiting? |
| Role affinity | Big models prefer big home lab machines, small models prefer small ones |
| Availability trend | Is this home lab device reliably available at this time of day? |
| Context fit | Does the loaded context window fit the home lab request? |

You don't manage any of this. The home lab router handles it.

## The Home Lab Dashboard

Open `http://localhost:11435/dashboard` in your browser — your home lab command center:

- **Home Lab Fleet Overview** — see every device, loaded models, queue depths, health
- **Trends** — home lab requests per hour, latency, token throughput over 24h-7d
- **Health** — 11 automated home lab checks with recommendations
- **Recommendations** — optimal home lab model mix per device based on your hardware

## Recommended Home Lab Models by Device

| Home Lab Device | RAM | Start with |
|--------|-----|-----------|
| MacBook Air (8GB) | 8GB | `phi4-mini`, `gemma3:1b` |
| Mac Mini (16GB) | 16GB | `phi4`, `gemma3:4b`, `nomic-embed-text` |
| Mac Mini (32GB) | 32GB | `qwen3:14b`, `deepseek-r1:14b` |
| MacBook Pro (64GB) | 64GB | `qwen3:32b`, `codestral`, `z-image-turbo` |
| Mac Studio (128GB) | 128GB | `llama3.3:70b`, `qwen3:72b` |
| Mac Studio (256GB) | 256GB | `gpt-oss:120b`, `sd3.5-large` |

The home lab router's model recommender suggests the optimal mix: `GET /dashboard/api/recommendations`.

## Works with Every Home Lab Tool

The home lab fleet exposes an OpenAI-compatible API. Any tool that works with OpenAI works with your home lab:

| Tool | Home Lab Connection |
|------|---------------|
| **Open WebUI** | Set Ollama URL to `http://homelab-router:11435` |
| **Aider** | `aider --openai-api-base http://homelab-router:11435/v1` |
| **Continue.dev** | Base URL: `http://homelab-router:11435/v1` |
| **LangChain** | `ChatOpenAI(base_url="http://homelab-router:11435/v1")` |
| **CrewAI** | Set `OPENAI_API_BASE=http://homelab-router:11435/v1` |
| **Any OpenAI SDK** | Base URL: `http://homelab-router:11435/v1`, API key: any string |

## Full documentation

- [Agent Setup Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/agent-setup-guide.md) — all 4 home lab model types
- [Image Generation Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/image-generation.md) — 3 home lab image backends
- [Configuration Reference](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/configuration-reference.md) — 30+ env vars
- [Troubleshooting](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/troubleshooting.md) — common home lab issues

## Contribute

Ollama Herd is open source (MIT) and built by home lab enthusiasts for home lab enthusiasts:
- [Star on GitHub](https://github.com/geeks-accelerator/ollama-herd) — help other home lab builders find us
- [Open an issue](https://github.com/geeks-accelerator/ollama-herd/issues) — share your home lab setup, report bugs
- **PRs welcome** — from humans and AI agents. `CLAUDE.md` gives full context.
- Built by twin brothers in Alaska who run their own home lab Mac fleet.

## Home Lab Guardrails

- **No automatic downloads** — home lab model pulls require explicit user confirmation. Some models are 70GB+.
- **Home lab model deletion requires explicit user confirmation.**
- **All home lab requests stay local** — no data leaves your home network.
- Never delete or modify files in `~/.fleet-manager/` (home lab routing data and logs).
- No cloud dependencies — your home lab works offline after initial model downloads.
