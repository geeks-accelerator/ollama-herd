---
name: qwen-qwen3.5
description: Qwen 3.5 by Alibaba — run Qwen 3.5 (the latest and most capable Qwen model) across your local device fleet. Qwen 3.5 rivals GPT-4o and Claude 3.5 on reasoning benchmarks. Plus Qwen3-Coder for code generation and Qwen3-ASR for speech-to-text. Fleet-routed to the best available machine via Ollama Herd. Zero cloud costs.
version: 1.0.0
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"sparkles","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","pip"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["darwin","linux"]}}
---

# Qwen 3.5 — Alibaba's Latest LLM on Your Local Fleet

Qwen 3.5 is the newest and most capable model in the Qwen family. It rivals GPT-4o and Claude 3.5 Sonnet on reasoning, coding, and multilingual benchmarks — and you can run it locally on your own hardware for free.

## Supported Qwen models

| Model | Parameters | Ollama name | Best for |
|-------|-----------|-------------|----------|
| **Qwen 3.5** | 72B | `qwen3.5` | Frontier reasoning — rivals GPT-4o |
| **Qwen 3.5** | 32B | `qwen3.5:32b` | Strong quality at lower resource cost |
| **Qwen 3.5** | 14B | `qwen3.5:14b` | Good balance for mid-range hardware |
| **Qwen 3.5** | 7B | `qwen3.5:7b` | Fast on low-RAM devices |
| **Qwen3-Coder** | 32B | `qwen3-coder:32b` | Code generation — 80+ languages |
| **Qwen2.5-Coder** | 7B, 32B | `qwen2.5-coder:32b` | Proven code model |
| **Qwen3-ASR** | — | `qwen3-asr` | Speech-to-text transcription |

## Quick start

```bash
pip install ollama-herd    # PyPI: https://pypi.org/project/ollama-herd/
herd                       # start the router (port 11435)
herd-node                  # run on each device — finds the router automatically
```

No models are downloaded during installation. Models are pulled on demand. All pulls require user confirmation.

## Use Qwen 3.5 through the fleet

### OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:11435/v1", api_key="not-needed")

# Qwen 3.5 for complex reasoning
response = client.chat.completions.create(
    model="qwen3.5",
    messages=[{"role": "user", "content": "Compare microservices vs monolith architectures"}],
    stream=True,
)
for chunk in response:
    print(chunk.choices[0].delta.content or "", end="")
```

### Qwen3-Coder for code

```python
response = client.chat.completions.create(
    model="qwen3-coder:32b",
    messages=[{"role": "user", "content": "Write a thread-safe connection pool in Go"}],
)
print(response.choices[0].message.content)
```

### Ollama API

```bash
# Qwen 3.5 chat
curl http://localhost:11435/api/chat -d '{
  "model": "qwen3.5",
  "messages": [{"role": "user", "content": "Explain attention mechanisms"}],
  "stream": false
}'
```

### Qwen3-ASR speech-to-text

```bash
curl http://localhost:11435/api/transcribe \
  -F "file=@meeting.wav" \
  -F "model=qwen3-asr"
```

## Hardware recommendations

| Device | RAM | Best Qwen model |
|--------|-----|----------------|
| Mac Mini (16GB) | 16GB | `qwen3.5:7b` |
| Mac Mini (32GB) | 32GB | `qwen3.5:14b` or `qwen2.5-coder:32b` |
| MacBook Pro (64GB) | 64GB | `qwen3.5:32b` or `qwen3-coder:32b` |
| Mac Studio (128GB) | 128GB | `qwen3.5` (72B) — full quality |
| Mac Studio (256GB) | 256GB | `qwen3.5` + `qwen3-coder:32b` simultaneously |

## Why Qwen 3.5 locally

- **GPT-4o quality** — Qwen 3.5 72B matches GPT-4o on MMLU, HumanEval, and MT-Bench
- **Zero cost** — no per-token charges after hardware
- **Privacy** — all data stays on your network
- **No rate limits** — Qwen's cloud API throttles during peak hours. Your hardware doesn't.
- **Fleet routing** — multiple machines share the load

## Also available on this fleet

### Other LLMs
Llama 3.3, DeepSeek-V3, DeepSeek-R1, Phi 4, Mistral, Gemma 3, Codestral — same endpoint.

### Image generation
```bash
curl -o image.png http://localhost:11435/api/generate-image \
  -d '{"model": "z-image-turbo", "prompt": "an AI assistant helping with code", "width": 1024, "height": 1024}'
```

### Embeddings
```bash
curl http://localhost:11435/api/embed \
  -d '{"model": "nomic-embed-text", "input": "Qwen 3.5 large language model"}'
```

## Monitor

```bash
curl -s http://localhost:11435/fleet/status | python3 -m json.tool
curl -s http://localhost:11435/dashboard/api/health | python3 -m json.tool
```

Dashboard at `http://localhost:11435/dashboard`.

## Full documentation

- [Agent Setup Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/agent-setup-guide.md)
- [API Reference](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/api-reference.md)

## Contribute

Ollama Herd is open source (MIT):
- [Star on GitHub](https://github.com/geeks-accelerator/ollama-herd) — help others run Qwen locally
- [Open an issue](https://github.com/geeks-accelerator/ollama-herd/issues) — share your Qwen setup, report bugs
- **PRs welcome** — `CLAUDE.md` gives AI agents full context. 412 tests, async Python.

## Guardrails

- **Model downloads require explicit user confirmation** — Qwen models range from 4GB (7B) to 42GB (72B).
- **Model deletion requires explicit user confirmation.**
- Never delete or modify files in `~/.fleet-manager/`.
- No models are downloaded automatically — all pulls are user-initiated or require opt-in.
