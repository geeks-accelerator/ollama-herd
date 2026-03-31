---
name: private-ai
description: Private AI — run LLMs, image generation, speech-to-text, and embeddings entirely on your own hardware. No data leaves your network. No cloud APIs, no telemetry, no third-party access. Air-gapped compatible. On-premise local AI for teams that need privacy, compliance, and data sovereignty. HIPAA-friendly, GDPR-ready.
version: 1.0.0
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"lock","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","pip"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["darwin","linux"]}}
---

# Private AI — Your Data Never Leaves Your Network

Every prompt, every response, every image, every transcription stays on your machines. No cloud APIs. No telemetry. No third-party access. Route AI workloads across your own devices with zero external dependencies.

## What makes this private

- **No external network calls** — the router and nodes communicate only on your local network
- **No telemetry** — zero usage data, analytics, or metrics sent anywhere
- **No API keys** — no accounts, no tokens, no cloud provider relationships
- **No model phone-home** — Ollama models run fully offline after download
- **Local-only state** — all data stored in `~/.fleet-manager/` on your machines (SQLite + JSONL logs)
- **Air-gap compatible** — pre-download models, then disconnect. The fleet runs without internet.

## Setup

```bash
pip install ollama-herd    # PyPI: https://pypi.org/project/ollama-herd/
herd                       # start the router (port 11435)
herd-node                  # run on each device — finds the router automatically
```

No models are downloaded during installation. All model downloads require explicit user confirmation. Once downloaded, models run entirely offline.

## Private LLM inference

Send sensitive prompts — legal documents, medical notes, financial data, proprietary code — without them ever leaving your network.

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:11435/v1", api_key="not-needed")

# Sensitive document analysis stays local
response = client.chat.completions.create(
    model="llama3.3:70b",
    messages=[{"role": "user", "content": "Review this contract clause for risks: ..."}],
)
print(response.choices[0].message.content)
```

### Any model, fully local

```bash
# DeepSeek-R1 for reasoning — no data sent to DeepSeek servers
curl http://localhost:11435/api/chat -d '{
  "model": "deepseek-r1:70b",
  "messages": [{"role": "user", "content": "Analyze this financial report: ..."}],
  "stream": false
}'
```

## Private image generation

Generate images from sensitive prompts without uploading them to DALL-E, Midjourney, or any cloud service.

```bash
curl http://localhost:11435/api/generate-image \
  -d '{"model": "z-image-turbo", "prompt": "internal product mockup, confidential design", "width": 1024, "height": 1024}'
```

## Private transcription

Transcribe meetings, legal depositions, medical dictation, and confidential recordings without cloud STT services.

```bash
curl http://localhost:11435/api/transcribe \
  -F "file=@board-meeting.wav" \
  -F "model=qwen3-asr"
```

## Private embeddings

Build knowledge bases from proprietary documents without sending content to OpenAI's embedding API.

```bash
curl http://localhost:11435/api/embed \
  -d '{"model": "nomic-embed-text", "input": "Q4 revenue projections for internal review"}'
```

## Air-gapped deployment

For fully disconnected environments:

1. Download models on a connected machine: `ollama pull llama3.3:70b`
2. Transfer the model files to the air-gapped network (USB, sneakernet)
3. Start the fleet — it runs without internet

The router discovers nodes on the local network. No DNS, no external lookups, no cloud callbacks.

## Compliance considerations

| Requirement | How Ollama Herd helps |
|-------------|----------------------|
| **Data residency** | All processing on your hardware, your jurisdiction |
| **No third-party subprocessors** | No cloud APIs involved in inference |
| **Audit trail** | SQLite trace log records every request (model, node, latency, tokens) |
| **Access control** | Fleet runs on your network — standard network security applies |
| **Data minimization** | Traces store routing metadata, never prompt content |

## Monitor your private fleet

```bash
# Fleet status — all nodes, loaded models, queue depths
curl -s http://localhost:11435/fleet/status | python3 -m json.tool

# Recent traces — routing decisions without prompt content
curl -s "http://localhost:11435/dashboard/api/traces?limit=10" | python3 -m json.tool

# Health checks — 11 automated diagnostics
curl -s http://localhost:11435/dashboard/api/health | python3 -m json.tool
```

Web dashboard at `http://localhost:11435/dashboard` — accessible only on your local network.

## Full documentation

- [Agent Setup Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/agent-setup-guide.md) — all 4 model types
- [Configuration Reference](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/configuration-reference.md) — environment variables
- [API Reference](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/api-reference.md) — complete endpoint docs

## Guardrails

- **No automatic downloads** — all model pulls require explicit user confirmation.
- **Model deletion requires explicit user confirmation.**
- **No external network access** — the router and nodes communicate only on your local network.
- **Read-only local state** — `~/.fleet-manager/latency.db` and `~/.fleet-manager/logs/herd.jsonl` are the only local files. Never delete or modify without user confirmation.
- **Traces never store prompt content** — only routing metadata (model name, node, latency, token counts).
