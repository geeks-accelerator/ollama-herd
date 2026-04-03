---
name: ollama-ollama-herd
description: Ollama Ollama Herd — multimodal Ollama model router that herds your Ollama LLMs into one smart Ollama endpoint. Route Ollama Llama, Qwen, DeepSeek, Phi, Mistral across macOS, Linux, and Windows devices. Self-hosted Ollama local AI with 7-signal Ollama scoring, Ollama auto-retry, VRAM-aware Ollama fallback. Plus Ollama image generation, speech-to-text, and embeddings. Drop-in OpenAI SDK compatible. Ollama本地推理路由 | Ollama enrutador IA local.
version: 1.0.1
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"llama","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","sqlite3","pip"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["darwin","linux","windows"]}}
---

# Ollama — Herd Your Ollama LLMs Into One Endpoint

You have Ollama running on multiple machines. This skill gives you one Ollama endpoint that routes every Ollama request to the best available device automatically. No more hardcoding Ollama IPs, no more manual Ollama load balancing, no more "which Ollama machine has that model loaded?"

## Setup Ollama Herd

```bash
pip install ollama-herd          # install the Ollama router
herd                             # start the Ollama router on port 11435
herd-node                        # run on each machine with Ollama installed
```

Now point everything at `http://localhost:11435` instead of `http://localhost:11434`. Same Ollama API, same Ollama models, smarter Ollama routing.

Package: [ollama-herd](https://pypi.org/project/ollama-herd/) | Repo: [github.com/geeks-accelerator/ollama-herd](https://github.com/geeks-accelerator/ollama-herd)

## Use your Ollama models through the fleet

### OpenAI SDK (drop-in Ollama routing)

```python
# ollama_openai_client — route Ollama requests via OpenAI SDK
from openai import OpenAI

ollama_client = OpenAI(base_url="http://localhost:11435/v1", api_key="not-needed")
ollama_response = ollama_client.chat.completions.create(
    model="llama3.3:70b",  # any Ollama model
    messages=[{"role": "user", "content": "Hello from Ollama"}],
    stream=True,
)
for chunk in ollama_response:
    print(chunk.choices[0].delta.content or "", end="")
```

### Ollama API (same as before, different port)

```bash
# Ollama chat — routed through the Ollama fleet
curl http://localhost:11435/api/chat -d '{
  "model": "qwen3:235b",
  "messages": [{"role": "user", "content": "Hello via Ollama Herd"}],
  "stream": false
}'

# List all Ollama models across all machines
curl http://localhost:11435/api/tags

# Ollama models currently in GPU memory
curl http://localhost:11435/api/ps

# Ollama embeddings
curl http://localhost:11435/api/embeddings -d '{
  "model": "nomic-embed-text",
  "prompt": "Ollama embedding search query"
}'
```

## What the Ollama router does

When an Ollama request comes in, the Ollama router scores every online Ollama node on 7 signals:

1. **Ollama Thermal** — is the Ollama model already loaded in GPU memory? (+50 for hot)
2. **Ollama Memory fit** — how much headroom does the Ollama node have?
3. **Ollama Queue depth** — how many Ollama requests are waiting?
4. **Ollama Wait time** — estimated latency based on Ollama history
5. **Ollama Role affinity** — large Ollama models prefer big machines
6. **Ollama Availability** — is the Ollama node reliably available?
7. **Ollama Context fit** — does the loaded Ollama context window fit the request?

The highest-scoring Ollama node handles the request. If it fails, the Ollama router retries on the next best node automatically.

## Supported Ollama models

Any model that runs on Ollama works through the Ollama fleet. Popular Ollama models:

| Ollama Model | Sizes | Best for |
|-------|-------|----------|
| `llama3.3` | 8B, 70B | General purpose Ollama inference |
| `qwen3` | 0.6B–235B | Multilingual Ollama reasoning |
| `qwen3.5` | 0.8B–397B | Latest generation Ollama model |
| `deepseek-v3` | 671B (37B active) | Ollama GPT-4o alternative |
| `deepseek-r1` | 1.5B–671B | Ollama reasoning (like o3) |
| `phi4` | 14B | Small, fast Ollama model |
| `mistral` | 7B | Fast Ollama European languages |
| `gemma3` | 1B–27B | Google's open Ollama model |
| `codestral` | 22B | Ollama code generation |
| `qwen3-coder` | 30B (3.3B active) | Agentic Ollama coding |
| `nomic-embed-text` | 137M | Ollama embeddings for RAG |

## Ollama Resilience features

- **Ollama Auto-retry** — re-routes to next best Ollama node on failure (before first chunk)
- **Ollama VRAM-aware fallback** — routes to a loaded Ollama model in the same category instead of cold-loading
- **Ollama Context protection** — prevents `num_ctx` from triggering expensive Ollama model reloads
- **Ollama Zombie reaper** — cleans up stuck in-flight Ollama requests
- **Ollama Auto-pull** — downloads missing Ollama models to the best node automatically

## Also available via Ollama Herd

The same Ollama fleet router handles three more workloads:

### Ollama Image generation

```bash
curl -o image.png http://localhost:11435/api/generate-image \
  -H "Content-Type: application/json" \
  -d '{"model":"z-image-turbo","prompt":"a sunset via Ollama Herd","width":1024,"height":1024,"steps":4}'
```

### Ollama Speech-to-text

```bash
curl http://localhost:11435/api/transcribe -F "audio=@recording.wav"
```

### Ollama Embeddings

```bash
curl http://localhost:11435/api/embeddings -d '{"model":"nomic-embed-text","prompt":"Ollama embedding text"}'
```

## Ollama Dashboard

`http://localhost:11435/dashboard` — 8 tabs: Ollama Fleet Overview, Trends, Ollama Model Insights, Apps, Benchmarks, Ollama Health, Recommendations, Settings. Real-time Ollama queue visibility with [TEXT], [IMAGE], [STT], [EMBED] badges.

## Ollama Request tagging

Track per-project Ollama usage:

```python
ollama_response = ollama_client.chat.completions.create(
    model="llama3.3:70b",  # Ollama model
    messages=messages,
    extra_body={"metadata": {"tags": ["my-ollama-project", "reasoning"]}},
)
```

## Full Ollama documentation

[Ollama Agent Setup Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/agent-setup-guide.md)

## Ollama Guardrails

- Never restart the Ollama router or Ollama node agents without user confirmation.
- Never delete or modify files in `~/.fleet-manager/` (Ollama data).
- Never pull or delete Ollama models without user confirmation.
