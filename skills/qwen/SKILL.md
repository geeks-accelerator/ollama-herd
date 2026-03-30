---
name: qwen
description: Qwen models on your local fleet — Qwen3.5, Qwen3, Qwen3-Coder, Qwen2.5-Coder, and Qwen ASR routed across multiple devices via Ollama Herd. LLM inference, code generation, and speech-to-text from Alibaba's Qwen family. Run locally on Apple Silicon with zero cloud costs.
version: 1.0.0
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"sparkles","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","pip"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["darwin","linux"]}}
---

# Qwen — Run Qwen Models Across Your Local Fleet

Run Qwen3.5, Qwen3, Qwen3-Coder, and Qwen ASR on your own hardware. The fleet router picks the best device for every request — chat, code generation, and speech-to-text from one endpoint.

## Supported Qwen models

### LLM (Chat & Reasoning)

| Model | Parameters | Ollama name | Best for |
|-------|-----------|-------------|----------|
| **Qwen3.5** | 0.8B–397B MoE | `qwen3.5` | Latest — multimodal, best reasoning |
| **Qwen3** | 0.6B–235B MoE | `qwen3` | Competitive with GPT-4o |
| **Qwen2.5** | 0.5B–72B | `qwen2.5` | Proven, stable, multilingual |

### Code Generation

| Model | Parameters | Ollama name | Best for |
|-------|-----------|-------------|----------|
| **Qwen3-Coder** | 30B MoE (3.3B active) | `qwen3-coder` | Agentic coding workflows |
| **Qwen2.5-Coder** | 0.5B–32B | `qwen2.5-coder` | Code — matches GPT-4o at 32B |

### Speech-to-Text

| Model | Parameters | Tool | Best for |
|-------|-----------|------|----------|
| **Qwen3-ASR** | 0.6B–1.7B | `mlx-qwen3-asr` | State-of-the-art local transcription |

## Setup

```bash
pip install ollama-herd
herd              # start the router (port 11435)
herd-node         # run on each machine

# Pull Qwen models
ollama pull qwen3.5:32b
ollama pull qwen3-coder
```

For speech-to-text:

```bash
uv tool install "mlx-qwen3-asr[serve]" --python 3.14
curl -X POST http://localhost:11435/dashboard/api/settings \
  -H "Content-Type: application/json" -d '{"transcription": true}'
```

Package: [ollama-herd](https://pypi.org/project/ollama-herd/) | Repo: [github.com/geeks-accelerator/ollama-herd](https://github.com/geeks-accelerator/ollama-herd)

## Use Qwen through the fleet

### OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:11435/v1", api_key="not-needed")

# Qwen3.5 for general chat
response = client.chat.completions.create(
    model="qwen3.5:32b",
    messages=[{"role": "user", "content": "Hello"}],
    stream=True,
)
for chunk in response:
    print(chunk.choices[0].delta.content or "", end="")
```

### Qwen3-Coder for code

```python
response = client.chat.completions.create(
    model="qwen3-coder",
    messages=[{"role": "user", "content": "Write a FastAPI CRUD app with SQLAlchemy"}],
)
print(response.choices[0].message.content)
```

### Qwen ASR for transcription

```bash
curl http://localhost:11435/api/transcribe -F "audio=@meeting.wav"
```

```python
import httpx

def transcribe(audio_path):
    with open(audio_path, "rb") as f:
        resp = httpx.post(
            "http://localhost:11435/api/transcribe",
            files={"audio": (audio_path, f)},
            timeout=300.0,
        )
    resp.raise_for_status()
    return resp.json()["text"]
```

### Ollama API

```bash
# Qwen3.5 chat
curl http://localhost:11435/api/chat -d '{
  "model": "qwen3.5:32b",
  "messages": [{"role": "user", "content": "Explain transformers"}],
  "stream": false
}'

# Qwen2.5-Coder
curl http://localhost:11435/api/chat -d '{
  "model": "qwen2.5-coder:32b",
  "messages": [{"role": "user", "content": "Optimize this SQL query: ..."}],
  "stream": false
}'
```

## Hardware recommendations

| Model | Min RAM | Recommended hardware |
|-------|---------|---------------------|
| `qwen3.5:0.8b` | 2GB | Any Mac |
| `qwen3.5:9b` | 8GB | Mac Mini M4 (16GB) |
| `qwen3.5:32b` | 24GB | Mac Mini M4 Pro (48GB) |
| `qwen3.5:122b-a10b` | 64GB | Mac Studio M4 Max (128GB) |
| `qwen3.5:397b-a17b` | 256GB+ | Mac Studio M3 Ultra (512GB) |
| `qwen3-coder` | 24GB | Mac Mini M4 Pro (48GB) |
| `qwen2.5-coder:32b` | 24GB | Mac Mini M4 Pro (48GB) |
| Qwen3-ASR (0.6B) | 1.2GB | Any Mac |
| Qwen3-ASR (1.7B) | 3.4GB | Any Mac (8GB+) |

## Why run Qwen locally

- **Zero cost** — no per-token charges for Qwen API
- **Privacy** — Chinese and English content stays on your devices
- **Full Qwen family** — chat, code, reasoning, and speech-to-text from one fleet
- **No rate limits** — Alibaba Cloud throttles API access. Local runs unlimited
- **Fleet routing** — multiple machines share the load. The router picks the fastest available

## The Qwen advantage on this fleet

Qwen models are uniquely suited for fleet routing:

- **MoE architecture** — Qwen3.5 (397B total, 17B active) and Qwen3-Coder (30B total, 3.3B active) use Mixture of Experts. Only a fraction of parameters activate per request, making them fast despite large total size.
- **Size variety** — from 0.6B to 397B, there's a Qwen model for every device in your fleet. Small Macs run the small models, big Macs run the big ones.
- **Code + Chat + STT** — Qwen covers three modalities. One vendor, one fleet, three capabilities.

## Also available on this fleet

### Other LLM models

Llama 3.3, DeepSeek-V3, DeepSeek-R1, Phi 4, Mistral, Gemma 3 — any Ollama model routes through the same endpoint.

### Image generation

```bash
curl -o image.png http://localhost:11435/api/generate-image \
  -H "Content-Type: application/json" \
  -d '{"model":"z-image-turbo","prompt":"a sunset","width":1024,"height":1024,"steps":4}'
```

### Embeddings

```bash
curl http://localhost:11435/api/embeddings -d '{"model":"nomic-embed-text","prompt":"query"}'
```

## Dashboard

`http://localhost:11435/dashboard` — monitor Qwen requests alongside all other models. Per-model latency, token throughput, error rates, health checks.

## Full documentation

[Agent Setup Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/agent-setup-guide.md)

## Guardrails

- Never pull or delete Qwen models without user confirmation.
- Never delete or modify files in `~/.fleet-manager/`.
- If a Qwen model is too large for available memory, suggest a smaller variant or MoE version.
