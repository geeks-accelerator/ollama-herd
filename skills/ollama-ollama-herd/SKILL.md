---
name: ollama-ollama-herd
description: Ollama Ollama Herd — multimodal model router that herds your Ollama LLMs into one smart endpoint. Route Llama, Qwen, DeepSeek, Phi, Mistral across Mac Studio, Mac Mini, MacBook Pro. Self-hosted local AI with 7-signal scoring, auto-retry, VRAM-aware fallback. Plus image generation, speech-to-text, and embeddings. Drop-in OpenAI SDK compatible.
version: 1.0.0
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"llama","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","sqlite3","pip"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["darwin","linux"]}}
---

# Ollama — Herd Your LLMs Into One Endpoint

You have Ollama running on multiple machines. This skill gives you one endpoint that routes every request to the best available device automatically. No more hardcoding IPs, no more manual load balancing, no more "which machine has that model loaded?"

## Setup

```bash
pip install ollama-herd
herd              # start the router on port 11435
herd-node         # run on each machine with Ollama
```

Now point everything at `http://localhost:11435` instead of `http://localhost:11434`. Same Ollama API, same models, smarter routing.

Package: [ollama-herd](https://pypi.org/project/ollama-herd/) | Repo: [github.com/geeks-accelerator/ollama-herd](https://github.com/geeks-accelerator/ollama-herd)

## Use your Ollama models through the fleet

### OpenAI SDK (drop-in)

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:11435/v1", api_key="not-needed")
response = client.chat.completions.create(
    model="llama3.3:70b",
    messages=[{"role": "user", "content": "Hello"}],
    stream=True,
)
for chunk in response:
    print(chunk.choices[0].delta.content or "", end="")
```

### Ollama API (same as before, different port)

```bash
# Chat
curl http://localhost:11435/api/chat -d '{
  "model": "qwen3:235b",
  "messages": [{"role": "user", "content": "Hello"}],
  "stream": false
}'

# List all models across all machines
curl http://localhost:11435/api/tags

# Models currently in GPU memory
curl http://localhost:11435/api/ps

# Embeddings
curl http://localhost:11435/api/embeddings -d '{
  "model": "nomic-embed-text",
  "prompt": "search query"
}'
```

## What the router does

When a request comes in, the router scores every online node on 7 signals:

1. **Thermal** — is the model already loaded in GPU memory? (+50 for hot)
2. **Memory fit** — how much headroom does the node have?
3. **Queue depth** — how many requests are waiting?
4. **Wait time** — estimated latency based on history
5. **Role affinity** — large models prefer big machines
6. **Availability** — is the node reliably available?
7. **Context fit** — does the loaded context window fit the request?

The highest-scoring node handles the request. If it fails, the router retries on the next best node automatically.

## Supported Ollama models

Any model that runs on Ollama works through the fleet. Popular ones:

| Model | Sizes | Best for |
|-------|-------|----------|
| `llama3.3` | 8B, 70B | General purpose |
| `qwen3` | 0.6B–235B | Multilingual, reasoning |
| `qwen3.5` | 0.8B–397B | Latest generation |
| `deepseek-v3` | 671B (37B active) | Matches GPT-4o |
| `deepseek-r1` | 1.5B–671B | Reasoning (like o3) |
| `phi4` | 14B | Small, fast, capable |
| `mistral` | 7B | Fast, European languages |
| `gemma3` | 1B–27B | Google's open model |
| `codestral` | 22B | Code generation |
| `qwen3-coder` | 30B (3.3B active) | Agentic coding |
| `nomic-embed-text` | 137M | Embeddings for RAG |

## Resilience features

- **Auto-retry** — re-routes to next best node on failure (before first chunk)
- **VRAM-aware fallback** — routes to a loaded model in the same category instead of cold-loading
- **Context protection** — prevents `num_ctx` from triggering expensive model reloads
- **Zombie reaper** — cleans up stuck in-flight requests
- **Auto-pull** — downloads missing models to the best node automatically

## Also available

The same fleet router handles three more workloads:

### Image generation

```bash
curl -o image.png http://localhost:11435/api/generate-image \
  -H "Content-Type: application/json" \
  -d '{"model":"z-image-turbo","prompt":"a sunset","width":1024,"height":1024,"steps":4}'
```

Enable: `curl -X POST .../dashboard/api/settings -d '{"image_generation":true}'`

### Speech-to-text

```bash
curl http://localhost:11435/api/transcribe -F "audio=@recording.wav"
```

Enable: `curl -X POST .../dashboard/api/settings -d '{"transcription":true}'`

### Embeddings

```bash
curl http://localhost:11435/api/embeddings -d '{"model":"nomic-embed-text","prompt":"text"}'
```

Already enabled — routes through Ollama automatically.

## Dashboard

`http://localhost:11435/dashboard` — 8 tabs: Fleet Overview, Trends, Model Insights, Apps, Benchmarks, Health, Recommendations, Settings. Real-time queue visibility with [TEXT], [IMAGE], [STT], [EMBED] badges.

## Request tagging

Track per-project usage:

```python
response = client.chat.completions.create(
    model="llama3.3:70b",
    messages=messages,
    extra_body={"metadata": {"tags": ["my-project", "reasoning"]}},
)
```

## Full documentation

[Agent Setup Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/agent-setup-guide.md)

## Guardrails

- Never restart the router or node agents without user confirmation.
- Never delete or modify files in `~/.fleet-manager/`.
- Never pull or delete models without user confirmation.
