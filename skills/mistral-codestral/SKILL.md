---
name: mistral-codestral
description: Mistral and Codestral — run Mistral Large, Mistral-Nemo, Codestral, and Mistral-Small across your local fleet. Code generation and general reasoning from Mistral AI routed to the best device. Codestral trained on 80+ languages. OpenAI-compatible, zero cloud costs.
version: 1.0.0
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"sparkles","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","pip"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["darwin","linux"]}}
---

# Mistral & Codestral — Code Generation and Reasoning on Your Fleet

Mistral AI's models excel at code generation (Codestral) and multilingual reasoning (Mistral Large). Route them across your devices — the fleet picks the optimal machine for every request.

## Supported Mistral models

| Model | Parameters | Ollama name | Best for |
|-------|-----------|-------------|----------|
| **Codestral** | 22B | `codestral` | Code generation — trained on 80+ programming languages |
| **Mistral Large** | 123B | `mistral-large` | Frontier reasoning, multilingual, function calling |
| **Mistral-Nemo** | 12B | `mistral-nemo` | Efficient general-purpose, great quality/size ratio |
| **Mistral-Small** | 22B | `mistral-small` | Fast reasoning, lower resource usage than Large |
| **Mistral 7B** | 7B | `mistral:7b` | Lightweight, runs on any hardware |

## Quick start

```bash
pip install ollama-herd    # PyPI: https://pypi.org/project/ollama-herd/
herd                       # start the router (port 11435)
herd-node                  # run on each device — finds the router automatically
```

No models are downloaded during installation. All model pulls are user-initiated and require confirmation.

## Code generation with Codestral

Codestral is Mistral's dedicated coding model — trained on 80+ languages with fill-in-the-middle support.

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:11435/v1", api_key="not-needed")

response = client.chat.completions.create(
    model="codestral",
    messages=[{"role": "user", "content": "Write a Redis-backed rate limiter in Go"}],
)
print(response.choices[0].message.content)
```

### Codestral via curl

```bash
curl http://localhost:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "codestral", "messages": [{"role": "user", "content": "Implement a B-tree in Rust"}]}'
```

## General reasoning with Mistral Large

```bash
curl http://localhost:11435/api/chat -d '{
  "model": "mistral-large",
  "messages": [{"role": "user", "content": "Compare microservices vs monolith for a 5-person startup"}],
  "stream": false
}'
```

## Hardware fit (choose what matches your RAM)

| Model | Min RAM | Recommended Mac |
|-------|---------|----------------|
| `mistral:7b` | 8GB | Any Mac — even 8GB MacBook Air |
| `mistral-nemo` | 10GB | Mac Mini (16GB) |
| `codestral` | 16GB | Mac Mini (24GB) or MacBook Pro |
| `mistral-small` | 16GB | Mac Mini (24GB) or MacBook Pro |
| `mistral-large` | 80GB | Mac Studio M4 Max (128GB) |

## Fleet features

The router handles all the complexity:

```bash
# What's loaded right now
curl -s http://localhost:11435/api/ps | python3 -m json.tool

# Fleet overview — nodes, queues, loaded models
curl -s http://localhost:11435/fleet/status | python3 -m json.tool

# Per-model performance stats
curl -s http://localhost:11435/dashboard/api/models | python3 -m json.tool
```

Web dashboard at `http://localhost:11435/dashboard` for visual monitoring.

## Also available on this fleet

### Other LLMs
Llama 3.3, Qwen 3.5, DeepSeek-V3, DeepSeek-R1, Phi 4, Gemma 3 — same endpoint, same routing.

### Image generation
```bash
curl http://localhost:11435/api/generate-image \
  -d '{"model": "z-image-turbo", "prompt": "abstract code visualization", "width": 512, "height": 512}'
```

### Speech-to-text
```bash
curl http://localhost:11435/api/transcribe -F "file=@standup.wav" -F "model=qwen3-asr"
```

### Embeddings
```bash
curl http://localhost:11435/api/embed \
  -d '{"model": "nomic-embed-text", "input": "Mistral AI open source models"}'
```

## Full documentation

- [Agent Setup Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/agent-setup-guide.md) — all 4 model types
- [API Reference](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/api-reference.md) — complete endpoint docs

## Guardrails

- **Model downloads require explicit user confirmation** — Mistral models range from 4GB (7B) to 70GB+ (123B). Always confirm before pulling.
- **Model deletion requires explicit user confirmation.**
- Never delete or modify files in `~/.fleet-manager/`.
- If a model is too large for available memory, suggest a smaller variant (e.g., `mistral-nemo` instead of `mistral-large`).
- No models are downloaded automatically — all pulls are user-initiated or require opt-in.
