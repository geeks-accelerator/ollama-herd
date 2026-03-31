---
name: phi-phi4
description: Phi 4 by Microsoft — small but powerful LLMs that run on minimal hardware. Phi-4 (14B), Phi-4-mini (3.8B), and Phi-3.5 across your device fleet. Perfect for Mac Mini, MacBook Air, and other low-RAM devices. State-of-the-art reasoning in a tiny footprint. Zero cloud costs.
version: 1.0.0
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"zap","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","pip"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["darwin","linux"]}}
---

# Phi 4 — Microsoft's Small Models, Big Results

Phi models prove you don't need 70B parameters for great results. Phi-4 matches much larger models on reasoning benchmarks while running on hardware as modest as an 8GB MacBook Air. Route them across your fleet for even better throughput.

## Supported Phi models

| Model | Parameters | Ollama name | RAM needed | Best for |
|-------|-----------|-------------|-----------|----------|
| **Phi-4** | 14B | `phi4` | 10GB | Reasoning, math, code — punches way above its weight |
| **Phi-4-mini** | 3.8B | `phi4-mini` | 4GB | Ultra-fast on any device, even 8GB Macs |
| **Phi-3.5-mini** | 3.8B | `phi3.5` | 4GB | Proven lightweight model |
| **Phi-3-medium** | 14B | `phi3:14b` | 10GB | Balanced quality and speed |

## Quick start

```bash
pip install ollama-herd    # PyPI: https://pypi.org/project/ollama-herd/
herd                       # start the router (port 11435)
herd-node                  # run on each device — finds the router automatically
```

No models are downloaded during installation. All pulls require user confirmation.

## Why Phi for small devices

A Mac Mini with 16GB RAM can run Phi-4 (14B) with room to spare. A MacBook Air with 8GB runs Phi-4-mini comfortably. These models start in seconds and respond fast — ideal for devices that can't load a 70B model.

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:11435/v1", api_key="not-needed")

# Phi-4 for reasoning
response = client.chat.completions.create(
    model="phi4",
    messages=[{"role": "user", "content": "Solve: if 3x + 7 = 22, what is x?"}],
)
print(response.choices[0].message.content)
```

### Phi-4-mini — fastest response times

```bash
curl http://localhost:11435/api/chat -d '{
  "model": "phi4-mini",
  "messages": [{"role": "user", "content": "Summarize this in 3 bullet points: ..."}],
  "stream": false
}'
```

### OpenAI-compatible API

```bash
curl http://localhost:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "phi4", "messages": [{"role": "user", "content": "Write a unit test for a login function"}]}'
```

## Ideal hardware pairings

| Your device | RAM | Best Phi model | Why |
|-------------|-----|---------------|-----|
| MacBook Air (8GB) | 8GB | `phi4-mini` | Fits with room for other apps |
| Mac Mini (16GB) | 16GB | `phi4` | Full Phi-4 with headroom |
| Mac Mini (24GB) | 24GB | `phi4` | Can run Phi-4 + an embedding model simultaneously |
| MacBook Pro (36GB) | 36GB | `phi4` + `phi4-mini` | Both loaded, router picks based on task |

## Monitor your fleet

```bash
# What's loaded and where
curl -s http://localhost:11435/api/ps | python3 -m json.tool

# Fleet health overview
curl -s http://localhost:11435/dashboard/api/health | python3 -m json.tool

# Model recommendations based on your hardware
curl -s http://localhost:11435/dashboard/api/recommendations | python3 -m json.tool
```

Web dashboard at `http://localhost:11435/dashboard` — live view of nodes, queues, and performance.

## Also available on this fleet

### Larger LLMs (when you need more power)
Llama 3.3 (70B), Qwen 3.5, DeepSeek-R1, Mistral Large — route to a bigger machine in the fleet.

### Image generation
```bash
curl http://localhost:11435/api/generate-image \
  -d '{"model": "z-image-turbo", "prompt": "minimalist circuit board art", "width": 512, "height": 512}'
```

### Speech-to-text
```bash
curl http://localhost:11435/api/transcribe -F "file=@meeting.wav" -F "model=qwen3-asr"
```

### Embeddings
```bash
curl http://localhost:11435/api/embed \
  -d '{"model": "nomic-embed-text", "input": "Microsoft Phi small language model"}'
```

## Full documentation

- [Agent Setup Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/agent-setup-guide.md) — all 4 model types
- [API Reference](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/api-reference.md) — complete endpoint docs

## Guardrails

- **Model downloads require explicit user confirmation** — Phi models are small (2-8GB) but still require confirmation.
- **Model deletion requires explicit user confirmation.**
- Never delete or modify files in `~/.fleet-manager/`.
- No models are downloaded automatically — all pulls are user-initiated or require opt-in.
