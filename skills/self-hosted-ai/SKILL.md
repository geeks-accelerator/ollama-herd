---
name: self-hosted-ai
description: Self-hosted AI — run your own LLM inference, image generation, speech-to-text, and embeddings. No cloud APIs, no SaaS subscriptions, no data leaving your network. Self-hosted alternative to OpenAI, DALL-E, Whisper API, and cloud embedding services. Route across macOS, Linux, and Windows machines. 自托管AI本地推理平台。IA autoalojada sin dependencias en la nube.
version: 1.0.2
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"server","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","pip"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["darwin","linux","windows"]}}
---

# Self-Hosted AI — Own Your Entire AI Stack

Stop paying per token. Stop sending data to cloud APIs. Run self-hosted LLMs, self-hosted image generation, self-hosted speech-to-text, and self-hosted embeddings on your own hardware. One self-hosted router makes all your devices act like one system.

## What self-hosted AI replaces

| Cloud service | Self-hosted replacement | How |
|--------------|----------------------|-----|
| **OpenAI API** | Self-hosted Llama 3.3, Qwen 3.5, DeepSeek-R1 via Ollama | Same OpenAI SDK, swap the base URL |
| **DALL-E / Midjourney** | Self-hosted Stable Diffusion 3, Flux via mflux/DiffusionKit | `POST /api/generate-image` |
| **Whisper API** | Self-hosted Qwen3-ASR via MLX | `POST /api/transcribe` |
| **OpenAI Embeddings** | Self-hosted nomic-embed-text, mxbai-embed via Ollama | `POST /api/embed` |

Same APIs. Same quality. Zero per-request costs. All data stays on your self-hosted machines.

## Self-Hosted Setup

```bash
pip install ollama-herd    # Self-hosted AI router from PyPI
herd                       # start the self-hosted router
herd-node                  # run on each self-hosted machine — auto-discovers the router
```

No Docker. No Kubernetes. No config files. Self-hosted devices find each other automatically on your local network.

## Self-Hosted LLM Inference

Drop-in self-hosted replacement for the OpenAI SDK:

```python
from openai import OpenAI

# Self-hosted inference client — replaces OpenAI cloud
self_hosted_client = OpenAI(base_url="http://localhost:11435/v1", api_key="not-needed")

self_hosted_response = self_hosted_client.chat.completions.create(
    model="llama3.3:70b",  # self-hosted model, no cloud dependency
    messages=[{"role": "user", "content": "Analyze this contract for risks"}],
    stream=True,
)
for chunk in self_hosted_response:
    print(chunk.choices[0].delta.content or "", end="")
```

### Self-hosted Ollama API

```bash
curl http://localhost:11435/api/chat -d '{
  "model": "deepseek-r1:70b",
  "messages": [{"role": "user", "content": "Explain self-hosted AI advantages over cloud APIs"}],
  "stream": false
}'
```

## Self-Hosted Image Generation

Self-hosted replacement for DALL-E and Midjourney:

```bash
# Install self-hosted image backends on any node
uv tool install mflux           # Self-hosted Flux models (~7s)
uv tool install diffusionkit    # Self-hosted Stable Diffusion 3/3.5

# Generate on your self-hosted fleet
curl -o self_hosted_output.png http://localhost:11435/api/generate-image \
  -H "Content-Type: application/json" \
  -d '{"model": "z-image-turbo", "prompt": "self-hosted AI generating product mockup", "width": 1024, "height": 1024}'
```

## Self-Hosted Speech-to-Text

Self-hosted replacement for Whisper API:

```bash
curl http://localhost:11435/api/transcribe \
  -F "file=@self_hosted_meeting.wav" \
  -F "model=qwen3-asr"
```

All self-hosted transcription stays on your network. No audio data sent to cloud services.

## Self-Hosted Embeddings

Self-hosted replacement for OpenAI's embedding API:

```bash
curl http://localhost:11435/api/embed \
  -d '{"model": "nomic-embed-text", "input": "self-hosted document embedding for private RAG pipelines"}'
```

## Self-Hosted Cost Comparison

| Service | Cloud cost | Self-hosted cost |
|---------|-----------|-----------------|
| GPT-4o (1M tokens/month) | ~$15-30/month | $0 (self-hosted hardware you own) |
| DALL-E (1000 images/month) | ~$40/month | $0 (self-hosted image gen) |
| Whisper API (10 hours audio/month) | ~$6/month | $0 (self-hosted transcription) |
| OpenAI embeddings (1M tokens/month) | ~$0.10/month | $0 (self-hosted embeddings) |
| **Total** | **~$60+/month** | **$0/month self-hosted** |

After hardware investment, every self-hosted request is free forever. No rate limits, no usage caps, no surprise bills.

## Self-Hosted Advantages

- **Self-hosted data sovereignty** — prompts, images, audio, and documents never leave your network
- **Self-hosted throughput** — your hardware, no rate limits
- **Self-hosted uptime** — cloud API outages don't affect your self-hosted fleet
- **Self-hosted flexibility** — switch models instantly, no vendor lock-in
- **Self-hosted compliance** — HIPAA, GDPR, SOC2 — no third-party data processors
- **Self-hosted predictability** — hardware depreciates, but never surprises you with a bill

## Self-Hosted Fleet Routing

The self-hosted router scores each device on 7 signals and picks the best one for every request. Multiple self-hosted machines share the load automatically.

```bash
# Self-hosted fleet overview
curl -s http://localhost:11435/fleet/status | python3 -m json.tool

# Self-hosted health checks
curl -s http://localhost:11435/dashboard/api/health | python3 -m json.tool

# Self-hosted model recommendations for your hardware
curl -s http://localhost:11435/dashboard/api/recommendations | python3 -m json.tool
```

Self-hosted dashboard at `http://localhost:11435/dashboard` for visual monitoring of your entire self-hosted fleet.

## Full self-hosted documentation

- [Agent Setup Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/agent-setup-guide.md) — all 4 self-hosted model types
- [Image Generation Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/image-generation.md) — 3 self-hosted image backends
- [API Reference](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/api-reference.md)
- [Configuration](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/configuration-reference.md)

## Contribute

Ollama Herd is open source (MIT). Self-hosted AI for everyone:
- [Star on GitHub](https://github.com/geeks-accelerator/ollama-herd) — help others discover self-hosted AI
- [Open an issue](https://github.com/geeks-accelerator/ollama-herd/issues) — share your self-hosted setup
- **PRs welcome** from humans and AI agents. `CLAUDE.md` gives full self-hosted context. 929 tests.

## Self-Hosted Guardrails

- **No automatic downloads** — all self-hosted model pulls require explicit user confirmation.
- **Self-hosted model deletion requires explicit user confirmation.**
- **All self-hosted requests stay local** — no data leaves your network. No telemetry, no analytics, no cloud callbacks.
- Never delete or modify self-hosted files in `~/.fleet-manager/`.
- Your self-hosted fleet has zero cloud dependencies — works fully offline after initial model downloads.
