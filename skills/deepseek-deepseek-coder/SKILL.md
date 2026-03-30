---
name: deepseek-deepseek-coder
description: DeepSeek DeepSeek-Coder — run DeepSeek-V3, DeepSeek-R1, DeepSeek-Coder across your local fleet. 7-signal scoring routes every request to the best device. Run DeepSeek locally on Apple Silicon with zero cloud costs via Ollama Herd.
version: 1.0.0
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"brain","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","pip"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["darwin","linux"]}}
---

# DeepSeek — Run DeepSeek Models Across Your Local Fleet

Run DeepSeek-V3, DeepSeek-R1, and DeepSeek-Coder on your own hardware. The fleet router picks the best device for every request — no cloud API needed, zero per-token costs, all data stays on your machines.

## Supported DeepSeek models

| Model | Parameters | Ollama name | Best for |
|-------|-----------|-------------|----------|
| **DeepSeek-V3** | 671B MoE (37B active) | `deepseek-v3` | General — matches GPT-4o on most benchmarks |
| **DeepSeek-V3.1** | 671B MoE | `deepseek-v3.1` | Hybrid thinking/non-thinking modes |
| **DeepSeek-V3.2** | 671B MoE | `deepseek-v3.2` | Improved reasoning + agent performance |
| **DeepSeek-R1** | 1.5B–671B | `deepseek-r1` | Reasoning — approaches O3 and Gemini 2.5 Pro |
| **DeepSeek-Coder** | 1.3B–33B | `deepseek-coder` | Code generation (87% code, 13% NL training) |
| **DeepSeek-Coder-V2** | 236B MoE (21B active) | `deepseek-coder-v2` | Code — matches GPT-4 Turbo on code tasks |

## Setup

```bash
pip install ollama-herd
herd              # start the router (port 11435)
herd-node         # run on each machine

# Pull a DeepSeek model
ollama pull deepseek-r1:70b
```

Package: [ollama-herd](https://pypi.org/project/ollama-herd/) | Repo: [github.com/geeks-accelerator/ollama-herd](https://github.com/geeks-accelerator/ollama-herd)

## Use DeepSeek through the fleet

### OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:11435/v1", api_key="not-needed")

# DeepSeek-R1 for reasoning
response = client.chat.completions.create(
    model="deepseek-r1:70b",
    messages=[{"role": "user", "content": "Prove that there are infinitely many primes"}],
    stream=True,
)
for chunk in response:
    print(chunk.choices[0].delta.content or "", end="")
```

### DeepSeek-Coder for code

```python
response = client.chat.completions.create(
    model="deepseek-coder-v2:16b",
    messages=[{"role": "user", "content": "Write a Redis cache decorator in Python"}],
)
print(response.choices[0].message.content)
```

### Ollama API

```bash
# DeepSeek-V3 general chat
curl http://localhost:11435/api/chat -d '{
  "model": "deepseek-v3",
  "messages": [{"role": "user", "content": "Explain quantum computing"}],
  "stream": false
}'

# DeepSeek-R1 reasoning
curl http://localhost:11435/api/chat -d '{
  "model": "deepseek-r1:70b",
  "messages": [{"role": "user", "content": "Solve this step by step: ..."}],
  "stream": false
}'
```

## Hardware recommendations

DeepSeek models are large. Here's what fits where:

| Model | Min RAM | Recommended hardware |
|-------|---------|---------------------|
| `deepseek-r1:1.5b` | 4GB | Any Mac |
| `deepseek-r1:7b` | 8GB | Mac Mini M4 (16GB) |
| `deepseek-r1:14b` | 12GB | Mac Mini M4 (24GB) |
| `deepseek-r1:32b` | 24GB | Mac Mini M4 Pro (48GB) |
| `deepseek-r1:70b` | 48GB | Mac Studio M4 Max (128GB) |
| `deepseek-coder-v2:16b` | 12GB | Mac Mini M4 (24GB) |
| `deepseek-v3` | 256GB+ | Mac Studio M3 Ultra (512GB) |

The fleet router automatically sends requests to the machine where the model is loaded — no manual routing needed.

## Why run DeepSeek locally

- **Zero cost** — DeepSeek API charges per token. Local is free after hardware.
- **Privacy** — code and business data never leave your network.
- **No rate limits** — DeepSeek API throttles during peak hours. Local has no throttle.
- **Availability** — DeepSeek API has had outages. Your hardware doesn't depend on their servers.
- **Fleet routing** — multiple machines share the load. One busy? Request goes to the next.

## Fleet features

- **7-signal scoring** — picks the optimal node for every request
- **Auto-retry** — fails over to next best node transparently
- **VRAM-aware fallback** — routes to a loaded model in the same category instead of cold-loading
- **Context protection** — prevents expensive model reloads from `num_ctx` changes
- **Request tagging** — track per-project DeepSeek usage

## Also available on this fleet

### Other LLM models

Llama 3.3, Qwen 3.5, Phi 4, Mistral, Gemma 3 — any Ollama model routes through the same endpoint.

### Image generation

```bash
curl -o image.png http://localhost:11435/api/generate-image \
  -H "Content-Type: application/json" \
  -d '{"model":"z-image-turbo","prompt":"a sunset","width":1024,"height":1024,"steps":4}'
```

### Speech-to-text

```bash
curl http://localhost:11435/api/transcribe -F "audio=@recording.wav"
```

### Embeddings

```bash
curl http://localhost:11435/api/embeddings -d '{"model":"nomic-embed-text","prompt":"query"}'
```

## Dashboard

`http://localhost:11435/dashboard` — monitor DeepSeek requests alongside all other models. Per-model latency, token throughput, health checks.

## Full documentation

[Agent Setup Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/agent-setup-guide.md)

## Guardrails

- Never pull or delete DeepSeek models without user confirmation — downloads are 4-400+ GB.
- Never delete or modify files in `~/.fleet-manager/`.
- If a DeepSeek model is too large for available memory, suggest a smaller variant.
