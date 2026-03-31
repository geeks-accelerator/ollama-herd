---
name: mac-mini-ai
description: Mac Mini AI — run LLMs, image generation, speech-to-text, and embeddings on your Mac Mini. M4 (16-32GB) and M4 Pro (24-64GB) configurations make the Mac Mini the most affordable entry point for local AI. Stack multiple Mac Minis into a fleet for the cost of one cloud GPU. Route requests across all your Mac Minis automatically.
version: 1.0.0
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"computer","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","pip"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["darwin"]}}
---

# Mac Mini AI — The $599 AI Node

The Mac Mini is the most cost-effective hardware for local AI. Starting at $599 with 16GB of unified memory, it runs 7B-14B models comfortably. Stack three Mac Minis for the cost of one month of cloud GPU rental — and they run forever with zero ongoing costs.

This skill turns one Mac Mini into an AI server and multiple Mac Minis into a fleet.

## Mac Mini configurations for AI

| Config | Chip | Unified Memory | Price | LLM Sweet Spot |
|--------|------|---------------|-------|----------------|
| Mac Mini M4 (16GB) | M4 | 16GB | $599 | 3B-7B models (`phi4-mini`, `llama3.2:3b`) |
| Mac Mini M4 (24GB) | M4 | 24GB | $799 | 7B-14B models (`phi4`, `gemma3:12b`) |
| Mac Mini M4 (32GB) | M4 | 32GB | $999 | 14B-22B models (`qwen3:14b`, `codestral`) |
| Mac Mini M4 Pro (48GB) | M4 Pro | 48GB | $1,399 | 22B-32B models (`qwen3:32b`) |
| Mac Mini M4 Pro (64GB) | M4 Pro | 64GB | $1,799 | 32B-70B models (`llama3.3:70b` quantized) |

## The Mac Mini fleet strategy

Three Mac Minis (32GB each) for $3,000 give you:
- 96GB total unified memory across the fleet
- Each runs a different model simultaneously
- The router picks the best device for every request
- $0/month after purchase — no cloud API costs

```
Mac Mini #1 (32GB) — llama3.3:70b (quantized)  ─┐
Mac Mini #2 (32GB) — codestral + phi4            ├──→  Router  ←──  Your apps
Mac Mini #3 (32GB) — qwen3:14b + embeddings     ─┘
```

## Setup

```bash
pip install ollama-herd    # PyPI: https://pypi.org/project/ollama-herd/
```

**On one Mac Mini (the router):**
```bash
herd
```

**On every other Mac Mini:**
```bash
herd-node
```

Devices discover each other automatically. No IP configuration, no Docker, no Kubernetes.

## Use your Mac Mini

### Chat with an LLM

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:11435/v1", api_key="not-needed")
response = client.chat.completions.create(
    model="phi4",
    messages=[{"role": "user", "content": "Write a Python web scraper"}],
    stream=True,
)
for chunk in response:
    print(chunk.choices[0].delta.content or "", end="")
```

### Ollama API

```bash
curl http://localhost:11435/api/chat -d '{
  "model": "gemma3:12b",
  "messages": [{"role": "user", "content": "Explain recursion simply"}],
  "stream": false
}'
```

### Image generation (optional)

```bash
uv tool install mflux    # Install on any Mac Mini
curl -o art.png http://localhost:11435/api/generate-image \
  -H "Content-Type: application/json" \
  -d '{"model": "z-image-turbo", "prompt": "a stack of Mac Minis glowing", "width": 512, "height": 512}'
```

### Speech-to-text

```bash
curl http://localhost:11435/api/transcribe -F "file=@meeting.wav" -F "model=qwen3-asr"
```

### Embeddings for RAG

```bash
curl http://localhost:11435/api/embed \
  -d '{"model": "nomic-embed-text", "input": "Mac Mini home server local AI"}'
```

## Best models for Mac Mini

| RAM | Best models | Why |
|-----|------------|-----|
| 16GB | `phi4-mini` (3.8B), `gemma3:4b`, `nomic-embed-text` | Small but capable, leaves room for OS |
| 24GB | `phi4` (14B), `gemma3:12b`, `codestral` | Sweet spot for single-model use |
| 32GB | `qwen3:14b`, `deepseek-r1:14b`, `codestral` + `phi4-mini` | Two models simultaneously |
| 48GB | `qwen3:32b`, `deepseek-r1:32b` | Larger models, great quality |
| 64GB | `llama3.3:70b` (quantized) | Near-frontier quality on a Mac Mini |

## Monitor your Mac Mini fleet

Dashboard at `http://localhost:11435/dashboard` — see every Mac Mini's status, loaded models, and queue depths.

```bash
# Fleet overview
curl -s http://localhost:11435/fleet/status | python3 -m json.tool

# Model recommendations for your hardware
curl -s http://localhost:11435/dashboard/api/recommendations | python3 -m json.tool
```

## Works with any OpenAI-compatible tool

| Tool | Connection |
|------|-----------|
| **Open WebUI** | Ollama URL: `http://mac-mini-ip:11435` |
| **Aider** | `aider --openai-api-base http://mac-mini-ip:11435/v1` |
| **Continue.dev** | Base URL: `http://mac-mini-ip:11435/v1` |
| **LangChain** | `ChatOpenAI(base_url="http://mac-mini-ip:11435/v1")` |

## Full documentation

- [Agent Setup Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/agent-setup-guide.md)
- [API Reference](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/api-reference.md)
- [Configuration](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/configuration-reference.md)

## Contribute

Ollama Herd is open source (MIT). Built for the Mac Mini fleet community:
- [Star on GitHub](https://github.com/geeks-accelerator/ollama-herd) — help other Mac Mini owners find us
- [Open an issue](https://github.com/geeks-accelerator/ollama-herd/issues) — share your Mac Mini fleet setup
- **PRs welcome** from humans and AI agents. `CLAUDE.md` gives full context.
- Running a Mac Mini cluster? We'd love to hear about it.

## Guardrails

- **No automatic downloads** — model pulls require explicit user confirmation.
- **Model deletion requires explicit user confirmation.**
- **All requests stay local** — no data leaves your network.
- Never delete or modify files in `~/.fleet-manager/`.
