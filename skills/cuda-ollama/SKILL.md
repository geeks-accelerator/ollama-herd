---
name: cuda-ollama
description: CUDA Ollama — route Ollama LLM inference across NVIDIA GPUs with automatic CUDA load balancing. CUDA Ollama cluster for RTX 4090, RTX 4080, A100, L40S, H100. NVIDIA CUDA Ollama fleet routing with 7-signal scoring, vRAM-aware fallback, and auto-retry. Run Llama, Qwen, DeepSeek, Phi, Mistral on NVIDIA CUDA GPUs. CUDA Ollama本地推理路由。CUDA Ollama enrutador IA NVIDIA.
version: 1.0.0
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"gpu","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","pip","nvidia-smi"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["linux","windows"]}}
---

# CUDA Ollama — Route LLMs Across NVIDIA GPUs

Turn your NVIDIA GPUs into a unified CUDA Ollama inference cluster. Ollama already uses CUDA for GPU acceleration — Ollama Herd routes requests across multiple CUDA-enabled machines automatically. One CUDA Ollama endpoint, many NVIDIA GPUs.

## Why CUDA Ollama fleet routing

You have NVIDIA GPUs across multiple machines — a workstation with an RTX 4090, a server with dual A100s, maybe an old machine with an RTX 3080. Each runs Ollama with CUDA. But without routing, you're manually picking which CUDA GPU handles each request.

CUDA Ollama Herd fixes this: one endpoint routes every request to the best available NVIDIA GPU based on 7 signals including vRAM fit, thermal state, and queue depth.

## NVIDIA CUDA GPU recommendations

| NVIDIA GPU | vRAM | Best CUDA Ollama models | Notes |
|------------|------|------------------------|-------|
| RTX 4090 | 24GB | `llama3.3:70b` (Q4), `qwen3.5:32b`, `deepseek-r1:32b` | Consumer CUDA king |
| RTX 4080 | 16GB | `qwen3.5:14b`, `phi4`, `codestral` | Great CUDA mid-range |
| RTX 4070 | 12GB | `llama3.2:3b`, `phi4-mini`, `gemma3:4b` | Budget CUDA option |
| RTX 3090 | 24GB | Same as RTX 4090 | Older CUDA, still excellent |
| A100 | 40/80GB | `llama3.3:70b` (full), `deepseek-v3` | Data center CUDA |
| H100 | 80GB | `deepseek-v3`, `qwen3.5:72b` | Frontier CUDA performance |
| L40S | 48GB | `llama3.3:70b`, `qwen3.5:32b` | Inference-optimized CUDA |

> **Cross-platform:** Any NVIDIA CUDA GPU works. These are example configurations — the fleet router runs on Linux and Windows.

## Quick start

```bash
pip install ollama-herd    # PyPI: https://pypi.org/project/ollama-herd/
```

### On your CUDA Ollama router machine:

```bash
herd    # start the CUDA Ollama router (port 11435)
```

### On every NVIDIA CUDA machine:

```bash
herd-node    # auto-discovers the CUDA Ollama router via mDNS
```

Verify CUDA is available on each NVIDIA node:

```bash
nvidia-smi    # confirm NVIDIA CUDA driver is loaded
ollama ps     # confirm Ollama is using CUDA GPU
```

> No mDNS? Connect CUDA nodes directly: `herd-node --router-url http://router-ip:11435`

## Use the CUDA Ollama cluster

### OpenAI SDK (drop-in replacement)

```python
from openai import OpenAI

# Point at your CUDA Ollama fleet
cuda_client = OpenAI(base_url="http://localhost:11435/v1", api_key="not-needed")

# Request routes to the best NVIDIA CUDA GPU automatically
response = cuda_client.chat.completions.create(
    model="llama3.3:70b",
    messages=[{"role": "user", "content": "Explain CUDA parallel computing"}],
    stream=True,
)
for chunk in response:
    print(chunk.choices[0].delta.content or "", end="")
```

### curl (Ollama format)

```bash
# Routes to best available NVIDIA CUDA GPU
curl http://localhost:11435/api/chat -d '{
  "model": "qwen3.5:32b",
  "messages": [{"role": "user", "content": "Optimize this CUDA kernel"}],
  "stream": false
}'
```

### curl (OpenAI format)

```bash
curl http://localhost:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "deepseek-r1:32b", "messages": [{"role": "user", "content": "Hello"}]}'
```

## CUDA Ollama fleet features

- **7-signal CUDA scoring** — thermal state, vRAM fit, queue depth, latency history, role affinity, availability trend, context fit
- **vRAM-aware CUDA fallback** — if a CUDA GPU is full, routes to the next best NVIDIA GPU
- **CUDA auto-retry** — transparent failover between NVIDIA CUDA nodes
- **Context protection** — prevents expensive CUDA model reloads from `num_ctx` changes
- **Thinking model support** — auto-inflates `num_predict` 4x for reasoning models on CUDA
- **Request tagging** — track per-project usage across your CUDA Ollama cluster

## Monitor your CUDA Ollama cluster

```bash
# NVIDIA CUDA fleet status
curl -s http://localhost:11435/fleet/status | python3 -m json.tool

# CUDA GPU health — 15 automated checks
curl -s http://localhost:11435/dashboard/api/health | python3 -m json.tool

# Which CUDA models are loaded
curl -s http://localhost:11435/api/ps | python3 -m json.tool
```

Web dashboard at `http://localhost:11435/dashboard` — live view of all NVIDIA CUDA nodes, queues, and models.

## Optimize Ollama for NVIDIA CUDA

```bash
# Linux (systemd)
sudo systemctl edit ollama
# Add under [Service]:
#   Environment="OLLAMA_KEEP_ALIVE=-1"
#   Environment="OLLAMA_MAX_LOADED_MODELS=-1"
#   Environment="OLLAMA_NUM_PARALLEL=2"
sudo systemctl restart ollama

# Windows (PowerShell)
[System.Environment]::SetEnvironmentVariable("OLLAMA_KEEP_ALIVE", "-1", "User")
[System.Environment]::SetEnvironmentVariable("OLLAMA_MAX_LOADED_MODELS", "-1", "User")
```

## Also available on this CUDA Ollama fleet

### Image generation
```bash
curl http://localhost:11435/api/generate-image \
  -d '{"model": "z-image-turbo", "prompt": "NVIDIA GPU rendering abstract art", "width": 1024, "height": 1024}'
```

### Embeddings
```bash
curl http://localhost:11435/api/embed \
  -d '{"model": "nomic-embed-text", "input": "NVIDIA CUDA GPU inference routing"}'
```

## Full documentation

- [Agent Setup Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/agent-setup-guide.md)
- [API Reference](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/api-reference.md)
- [Configuration Reference](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/configuration-reference.md)

## Contribute

Ollama Herd is open source (MIT). NVIDIA CUDA users, PRs welcome:
- [Star on GitHub](https://github.com/geeks-accelerator/ollama-herd) — help CUDA Ollama users find local inference
- [Open an issue](https://github.com/geeks-accelerator/ollama-herd/issues)

## Guardrails

- **CUDA Ollama model downloads require explicit user confirmation** — models range from 1GB to 400GB+.
- **CUDA Ollama model deletion requires explicit user confirmation.**
- Never delete or modify files in `~/.fleet-manager/`.
- No models are downloaded automatically — all pulls are user-initiated or require opt-in via `auto_pull`.
