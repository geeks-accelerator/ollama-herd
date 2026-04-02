---
name: gemma-gemma3
description: Gemma 3 by Google — run Gemma 3 (4B, 12B, 27B) across your local device fleet. Google's most capable open model with 128K context, strong coding, and multilingual support. Fleet-routed to the best available machine via Ollama Herd. Zero cloud costs on Mac Studio, Mac Mini, MacBook Pro.
version: 1.0.0
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"gem","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","pip"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["darwin","linux"]}}
---

# Gemma 3 — Run Google's Open Models Across Your Fleet

Gemma 3 is Google's most capable open-source LLM family. 128K context window, strong coding performance, multilingual support across 140+ languages. The fleet router picks the best device for every request — no manual load balancing.

## Supported Gemma models

| Model | Parameters | Ollama name | Best for |
|-------|-----------|-------------|----------|
| **Gemma 3 27B** | 27B | `gemma3:27b` | Highest quality — rivals much larger models |
| **Gemma 3 12B** | 12B | `gemma3:12b` | Balanced quality and speed |
| **Gemma 3 4B** | 4B | `gemma3:4b` | Fast, runs on low-RAM devices |
| **Gemma 3 1B** | 1B | `gemma3:1b` | Ultra-light, instant responses |
| **CodeGemma 7B** | 7B | `codegemma` | Code-focused variant |

## Quick start

```bash
pip install ollama-herd    # PyPI: https://pypi.org/project/ollama-herd/
herd                       # start the router (port 11435)
herd-node                  # run on each device — finds the router automatically
```

No models are downloaded during installation. Models are pulled on demand when a request arrives, or manually via the dashboard. All pulls require user confirmation.

## Use Gemma through the fleet

### OpenAI SDK (drop-in replacement)

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:11435/v1", api_key="not-needed")

# Gemma 3 27B for complex reasoning
response = client.chat.completions.create(
    model="gemma3:27b",
    messages=[{"role": "user", "content": "Explain quantum entanglement to a 10-year-old"}],
    stream=True,
)
for chunk in response:
    print(chunk.choices[0].delta.content or "", end="")
```

### Code generation with CodeGemma

```python
response = client.chat.completions.create(
    model="codegemma",
    messages=[{"role": "user", "content": "Write a binary search tree in Rust with insert, delete, and search"}],
)
print(response.choices[0].message.content)
```

### curl (Ollama format)

```bash
# Gemma 3 27B
curl http://localhost:11435/api/chat -d '{
  "model": "gemma3:27b",
  "messages": [{"role": "user", "content": "Translate to Japanese: The weather is beautiful today"}],
  "stream": false
}'
```

### curl (OpenAI format)

```bash
curl http://localhost:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "gemma3:4b", "messages": [{"role": "user", "content": "Hello"}]}'
```

## Which Gemma for your hardware

| Device | RAM | Best Gemma model |
|--------|-----|-----------------|
| MacBook Air (8GB) | 8GB | `gemma3:1b` — instant responses |
| Mac Mini (16GB) | 16GB | `gemma3:4b` — strong for its size |
| Mac Mini (24GB) | 24GB | `gemma3:12b` — great balance |
| MacBook Pro (36GB) | 36GB | `gemma3:27b` — full power |
| Mac Studio (64GB+) | 64GB+ | `gemma3:27b` + `codegemma` simultaneously |

## Why Gemma locally

- **128K context** — process entire codebases and long documents
- **140+ languages** — multilingual without switching models
- **Google quality, zero cost** — no per-token charges after hardware
- **Privacy** — all data stays on your network
- **Fleet routing** — multiple machines share the load

## Check what's running

```bash
# Models loaded in memory
curl -s http://localhost:11435/api/ps | python3 -m json.tool

# Fleet health
curl -s http://localhost:11435/dashboard/api/health | python3 -m json.tool
```

Web dashboard at `http://localhost:11435/dashboard` — live monitoring.

## Also available on this fleet

### Other LLMs
Llama 3.3, Qwen 3.5, DeepSeek-V3, DeepSeek-R1, Phi 4, Mistral, Codestral — same endpoint.

### Image generation
```bash
curl -o image.png http://localhost:11435/api/generate-image \
  -d '{"model": "z-image-turbo", "prompt": "a gemstone catching light", "width": 1024, "height": 1024}'
```

### Speech-to-text
```bash
curl http://localhost:11435/api/transcribe -F "file=@meeting.wav" -F "model=qwen3-asr"
```

### Embeddings
```bash
curl http://localhost:11435/api/embed \
  -d '{"model": "nomic-embed-text", "input": "Google Gemma open source language model"}'
```

## Full documentation

- [Agent Setup Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/agent-setup-guide.md)
- [API Reference](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/api-reference.md)

## Contribute

Ollama Herd is open source (MIT). Stars, issues, and PRs welcome — from humans and AI agents alike:
- [GitHub](https://github.com/geeks-accelerator/ollama-herd) — 444 tests, fully async, `CLAUDE.md` makes AI agents productive instantly
- Found a bug? [Open an issue](https://github.com/geeks-accelerator/ollama-herd/issues)
- Want to add a feature? Fork, branch, PR — the test suite runs in under 40 seconds

## Guardrails

- **Model downloads require explicit user confirmation** — Gemma models range from 1GB (1B) to 16GB (27B).
- **Model deletion requires explicit user confirmation.**
- Never delete or modify files in `~/.fleet-manager/`.
- No models are downloaded automatically — all pulls are user-initiated or require opt-in via `auto_pull`.
