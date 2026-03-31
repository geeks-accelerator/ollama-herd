---
name: self-hosted-ai
description: Self-hosted AI — run your own LLM inference, image generation, speech-to-text, and embeddings. No cloud APIs, no SaaS subscriptions, no data leaving your network. Self-hosted alternative to OpenAI, DALL-E, Whisper API, and cloud embedding services. Route across Mac Studio, Mac Mini, MacBook Pro, and Linux machines.
version: 1.0.0
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"server","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","pip"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["darwin","linux"]}}
---

# Self-Hosted AI — Own Your Entire AI Stack

Stop paying per token. Stop sending data to cloud APIs. Run LLMs, image generation, speech-to-text, and embeddings on your own hardware. One router makes all your devices act like one system.

## What you're replacing

| Cloud service | Self-hosted replacement | How |
|--------------|----------------------|-----|
| **OpenAI API** | Llama 3.3, Qwen 3.5, DeepSeek-R1 via Ollama | Same OpenAI SDK, swap the base URL |
| **DALL-E / Midjourney** | Stable Diffusion 3, Flux via mflux/DiffusionKit | `POST /api/generate-image` |
| **Whisper API** | Qwen3-ASR via MLX | `POST /api/transcribe` |
| **OpenAI Embeddings** | nomic-embed-text, mxbai-embed via Ollama | `POST /api/embed` |

Same APIs. Same quality. Zero per-request costs. All data stays on your machines.

## Setup

```bash
pip install ollama-herd    # PyPI: https://pypi.org/project/ollama-herd/
herd                       # start the router
herd-node                  # run on each machine — auto-discovers the router
```

No Docker. No Kubernetes. No config files. Devices find each other automatically on your local network.

## Self-hosted LLM inference

Drop-in replacement for the OpenAI SDK:

```python
from openai import OpenAI

# Before: client = OpenAI(api_key="sk-...")
# After:
client = OpenAI(base_url="http://localhost:11435/v1", api_key="not-needed")

response = client.chat.completions.create(
    model="llama3.3:70b",  # or qwen3:32b, deepseek-r1:70b, etc.
    messages=[{"role": "user", "content": "Analyze this contract for risks"}],
    stream=True,
)
for chunk in response:
    print(chunk.choices[0].delta.content or "", end="")
```

### Ollama API

```bash
curl http://localhost:11435/api/chat -d '{
  "model": "deepseek-r1:70b",
  "messages": [{"role": "user", "content": "Explain this code: ..."}],
  "stream": false
}'
```

## Self-hosted image generation

Replace DALL-E and Midjourney:

```bash
# Install image backends on any node
uv tool install mflux           # Flux models (~7s)
uv tool install diffusionkit    # Stable Diffusion 3/3.5

# Generate
curl -o image.png http://localhost:11435/api/generate-image \
  -H "Content-Type: application/json" \
  -d '{"model": "z-image-turbo", "prompt": "product mockup on white background", "width": 1024, "height": 1024}'
```

## Self-hosted speech-to-text

Replace Whisper API:

```bash
curl http://localhost:11435/api/transcribe \
  -F "file=@meeting-recording.wav" \
  -F "model=qwen3-asr"
```

## Self-hosted embeddings

Replace OpenAI's embedding API:

```bash
curl http://localhost:11435/api/embed \
  -d '{"model": "nomic-embed-text", "input": "your document text here"}'
```

## Cost comparison

| Service | Cloud cost | Self-hosted cost |
|---------|-----------|-----------------|
| GPT-4o (1M tokens/month) | ~$15-30/month | $0 (hardware you already own) |
| DALL-E (1000 images/month) | ~$40/month | $0 |
| Whisper API (10 hours audio/month) | ~$6/month | $0 |
| OpenAI embeddings (1M tokens/month) | ~$0.10/month | $0 |
| **Total** | **~$60+/month** | **$0/month** |

After hardware investment, every request is free forever. No rate limits, no usage caps, no surprise bills.

## Self-hosted advantages

- **Data sovereignty** — prompts, images, audio, and documents never leave your network
- **No rate limits** — your hardware, your throughput
- **No downtime dependency** — cloud API outages don't affect you
- **No vendor lock-in** — switch models instantly, no migration
- **Compliance-friendly** — HIPAA, GDPR, SOC2 — no third-party data processors
- **Predictable costs** — hardware depreciates, but never surprises you with a bill

## Fleet routing

The router scores each device on 7 signals and picks the best one for every request. Multiple machines share the load automatically.

```bash
# Fleet overview
curl -s http://localhost:11435/fleet/status | python3 -m json.tool

# Health checks
curl -s http://localhost:11435/dashboard/api/health | python3 -m json.tool

# Model recommendations for your hardware
curl -s http://localhost:11435/dashboard/api/recommendations | python3 -m json.tool
```

Dashboard at `http://localhost:11435/dashboard` for visual monitoring.

## Full documentation

- [Agent Setup Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/agent-setup-guide.md) — all 4 model types
- [Image Generation Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/image-generation.md) — 3 image backends
- [API Reference](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/api-reference.md)
- [Configuration](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/configuration-reference.md)

## Contribute

Ollama Herd is open source (MIT). Self-hosted AI for everyone:
- [Star on GitHub](https://github.com/geeks-accelerator/ollama-herd) — help others escape cloud API lock-in
- [Open an issue](https://github.com/geeks-accelerator/ollama-herd/issues) — share your self-hosted setup
- **PRs welcome** from humans and AI agents. `CLAUDE.md` gives full context. 412 tests.

## Guardrails

- **No automatic downloads** — all model pulls require explicit user confirmation.
- **Model deletion requires explicit user confirmation.**
- **All requests stay local** — no data leaves your network. No telemetry, no analytics, no cloud callbacks.
- Never delete or modify files in `~/.fleet-manager/`.
