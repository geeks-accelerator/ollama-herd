---
name: windows-ai
description: Windows AI — run local AI on Windows with LLM inference, image generation, and embeddings. Windows AI server for Llama, Qwen, DeepSeek, Phi, Mistral. Turn Windows PCs into a Windows AI cluster. No cloud APIs, no subscriptions — Windows AI runs entirely on your hardware. Windows AI本地推理。Windows IA local sin dependencias en la nube.
version: 1.0.0
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"computer","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","pip","nvidia-smi"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["windows"]}}
---

# Windows AI — Local AI on Your Windows PCs

Run AI entirely on Windows. No cloud APIs, no subscriptions, no data leaving your network. Windows AI via Ollama Herd routes LLM requests across your Windows machines — your gaming PC, your work desktop, your laptop. One Windows AI endpoint serves them all.

## Why Windows AI locally

- **Zero cost** — no per-token charges. Your Windows PC runs unlimited AI inference.
- **Privacy** — prompts and responses never leave your Windows network.
- **No rate limits** — cloud APIs throttle. Your Windows AI hardware doesn't.
- **NVIDIA GPU support** — Windows AI uses your RTX GPU via CUDA for fast inference.
- **Fleet routing** — multiple Windows PCs share the AI workload automatically.

## Windows AI quick start

```powershell
# Install Windows AI router
pip install ollama-herd

# Start Windows AI on your main PC
herd          # Windows AI router on port 11435
herd-node     # register this Windows AI node

# On other Windows PCs
herd-node     # joins the Windows AI cluster automatically
```

> **Windows Firewall:** Allow port 11435 — `netsh advfirewall firewall add rule name="Windows AI" dir=in action=allow protocol=tcp localport=11435`

## Use Windows AI

### OpenAI SDK

```python
from openai import OpenAI

# Your Windows AI endpoint
client = OpenAI(base_url="http://localhost:11435/v1", api_key="not-needed")

# Windows AI routes to the best available GPU
response = client.chat.completions.create(
    model="qwen3.5:32b",
    messages=[{"role": "user", "content": "Explain local AI vs cloud AI for Windows users"}],
    stream=True,
)
for chunk in response:
    print(chunk.choices[0].delta.content or "", end="")
```

### Windows AI for coding

```python
# Windows AI code generation
response = client.chat.completions.create(
    model="codestral",
    messages=[{"role": "user", "content": "Write a C# Windows service that monitors GPU temperature"}],
)
print(response.choices[0].message.content)
```

### curl (PowerShell)

```powershell
# Windows AI chat
curl http://localhost:11435/api/chat -d '{
  "model": "llama3.3:70b",
  "messages": [{"role": "user", "content": "Hello from Windows AI"}],
  "stream": false
}'
```

## Windows AI hardware guide

| Windows PC | GPU | RAM | Best Windows AI models |
|------------|-----|-----|------------------------|
| Gaming desktop | RTX 4090 (24GB) | 32GB+ | `llama3.3:70b`, `qwen3.5:32b` — full quality Windows AI |
| Gaming desktop | RTX 4080 (16GB) | 16GB+ | `phi4`, `codestral`, `qwen3.5:14b` |
| Work laptop | RTX 4060 (8GB) | 16GB | `phi4-mini`, `gemma3:4b` — fast Windows AI |
| Office desktop | Intel/AMD (no GPU) | 16GB | `phi4-mini`, `gemma3:1b` — CPU Windows AI |

> Windows AI works with or without a GPU. NVIDIA GPUs dramatically accelerate inference.

## Windows AI environment setup

```powershell
# Optimize Windows AI performance
[System.Environment]::SetEnvironmentVariable("OLLAMA_KEEP_ALIVE", "-1", "User")
[System.Environment]::SetEnvironmentVariable("OLLAMA_MAX_LOADED_MODELS", "-1", "User")
# Restart Ollama from the Windows system tray
```

## Windows AI features

- **7-signal scoring** — picks the best Windows PC for every AI request
- **15 health checks** — monitors all Windows AI nodes in real-time
- **Auto-retry** — transparent failover between Windows AI machines
- **vRAM-aware routing** — knows which Windows GPU has room for the model
- **Request tagging** — track per-project Windows AI usage
- **Web dashboard** — `http://localhost:11435/dashboard`

## Windows AI integrations

Works with any OpenAI-compatible tool on Windows:

- **Continue.dev** (VS Code) — set endpoint to `http://localhost:11435/v1`
- **Cursor** — Windows AI as local backend
- **LangChain** — drop-in OpenAI replacement
- **CrewAI** — multi-agent workflows on Windows AI
- **Open WebUI** — chat interface for Windows AI

## Also available on Windows AI

### Image generation
```powershell
curl http://localhost:11435/api/generate-image `
  -d '{"model": "z-image-turbo", "prompt": "futuristic Windows desktop", "width": 1024, "height": 1024}'
```

### Embeddings
```powershell
curl http://localhost:11435/api/embed `
  -d '{"model": "nomic-embed-text", "input": "Windows AI local inference embeddings"}'
```

## Full documentation

- [Agent Setup Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/agent-setup-guide.md)
- [API Reference](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/api-reference.md)

## Contribute

Ollama Herd is open source (MIT). Windows AI enthusiasts welcome:
- [Star on GitHub](https://github.com/geeks-accelerator/ollama-herd)
- [Open an issue](https://github.com/geeks-accelerator/ollama-herd/issues)

## Guardrails

- **Windows AI model downloads require explicit user confirmation.**
- **Windows AI model deletion requires explicit user confirmation.**
- Never delete or modify files in `~/.fleet-manager/`.
- No models are downloaded automatically — all pulls are user-initiated or require opt-in.
