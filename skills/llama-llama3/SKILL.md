---
name: llama-llama3
description: Llama 3 by Meta — run Llama 3.3, Llama 3.2, and Llama 3.1 across your local device fleet. The most popular open-source LLM family routed to the best available machine. 8B for fast responses, 70B for quality, 405B for frontier performance. OpenAI-compatible API, zero cloud costs on Mac Studio, Mac Mini, MacBook Pro.
version: 1.0.0
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"llama","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","pip"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["darwin","linux"]}}
---

# Llama 3 — Run Meta's LLMs Across Your Local Fleet

The Llama family is the most widely deployed open-source LLM. This skill routes Llama requests across your devices — the fleet picks the best machine for every request automatically.

## Supported Llama models

| Model | Parameters | Ollama name | Best for |
|-------|-----------|-------------|----------|
| **Llama 3.3** | 70B | `llama3.3:70b` | Best overall — matches GPT-4o on most benchmarks |
| **Llama 3.2** | 1B, 3B | `llama3.2:3b` | Fast responses on low-RAM devices |
| **Llama 3.1** | 8B, 70B, 405B | `llama3.1:70b` | Proven workhorse, massive community |
| **Llama 3** | 8B, 70B | `llama3:70b` | Original release, still widely used |

## Quick start

```bash
pip install ollama-herd    # PyPI: https://pypi.org/project/ollama-herd/
herd                       # start the router (port 11435)
herd-node                  # run on each device — finds the router automatically
```

No models are downloaded during installation. Models are pulled on demand when a request arrives, or manually via the dashboard. All pulls require user confirmation.

## Use Llama through the fleet

### OpenAI SDK (drop-in replacement)

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:11435/v1", api_key="not-needed")

response = client.chat.completions.create(
    model="llama3.3:70b",
    messages=[{"role": "user", "content": "Explain transformer architecture"}],
    stream=True,
)
for chunk in response:
    print(chunk.choices[0].delta.content or "", end="")
```

### curl (Ollama format)

```bash
curl http://localhost:11435/api/chat -d '{
  "model": "llama3.3:70b",
  "messages": [{"role": "user", "content": "Write a Python quicksort"}],
  "stream": false
}'
```

### curl (OpenAI format)

```bash
curl http://localhost:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "llama3.2:3b", "messages": [{"role": "user", "content": "Hello"}]}'
```

## Which Llama model for your hardware

Pick the model that fits your available memory — smaller models work great for most tasks:

| Model | Min RAM | Recommended Mac |
|-------|---------|----------------|
| `llama3.2:1b` | 2GB | Any Mac — even 8GB |
| `llama3.2:3b` | 4GB | Mac Mini (16GB) |
| `llama3:8b` | 8GB | Mac Mini (16GB) |
| `llama3.3:70b` | 48GB | Mac Studio M4 Max (128GB) |
| `llama3.1:405b` | 256GB+ | Mac Studio M4 Ultra (256GB) or distributed |

The fleet router sends requests to the machine where the model is loaded. No manual routing needed.

## Why run Llama locally

- **Free after hardware** — Meta's license allows commercial use with no per-token cost
- **Privacy** — prompts and responses never leave your network
- **No rate limits** — your hardware, your throughput
- **Fleet routing** — multiple machines share the load automatically

## See what's running

```bash
# Models loaded in memory right now
curl -s http://localhost:11435/api/ps | python3 -m json.tool

# All models available across the fleet
curl -s http://localhost:11435/api/tags | python3 -m json.tool
```

## Monitor Llama performance

```bash
# Recent request traces — see latency, tokens, which node handled each request
curl -s "http://localhost:11435/dashboard/api/traces?limit=10" | python3 -m json.tool

# Fleet health — 11 automated checks
curl -s http://localhost:11435/dashboard/api/health | python3 -m json.tool
```

Web dashboard at `http://localhost:11435/dashboard` — live view of all nodes, queues, and models.

## Also available on this fleet

### Other LLM models

Qwen 3.5, DeepSeek-V3, DeepSeek-R1, Phi 4, Mistral, Gemma 3, Codestral — any Ollama model routes through the same endpoint.

### Image generation

```bash
curl http://localhost:11435/api/generate-image \
  -d '{"model": "z-image-turbo", "prompt": "a llama in the mountains", "width": 512, "height": 512}'
```

### Speech-to-text

```bash
curl http://localhost:11435/api/transcribe -F "file=@recording.wav" -F "model=qwen3-asr"
```

### Embeddings

```bash
curl http://localhost:11435/api/embed \
  -d '{"model": "nomic-embed-text", "input": "Meta Llama open source language model"}'
```

## Full documentation

- [Agent Setup Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/agent-setup-guide.md) — all 4 model types
- [API Reference](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/api-reference.md) — complete endpoint docs

## Guardrails

- **Model downloads require explicit user confirmation** — Llama models range from 1GB (1B) to 230GB+ (405B). Always confirm before pulling.
- **Model deletion requires explicit user confirmation.**
- Never delete or modify files in `~/.fleet-manager/`.
- If a model is too large for available memory, suggest a smaller variant.
- No models are downloaded automatically — all pulls are user-initiated or require opt-in via the `auto_pull` setting.
