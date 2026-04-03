---
name: wsl2-local-ai
description: WSL2 Local AI — run LLMs on Windows via WSL2 with NVIDIA GPU passthrough. WSL2 AI development with Ollama, CUDA, and Docker. WSL2 Ollama fleet routing for Windows developers. Build AI apps on WSL2 with full Linux performance and Windows convenience. WSL2本地AI开发。WSL2 IA local para desarrolladores Windows.
version: 1.0.0
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"penguin","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","pip","nvidia-smi","wsl"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["windows"]}}
---

# WSL2 Local AI — Windows Developer LLM Stack

Develop AI apps on Windows with full Linux performance. WSL2 gives you native Linux inside Windows with NVIDIA GPU passthrough — your RTX GPU runs CUDA in WSL2 at near-native speed. Ollama Herd routes AI requests across WSL2 instances and native Windows machines.

## Why WSL2 for local AI

- **Full Linux + Windows GPU** — WSL2 passes your NVIDIA GPU directly to Linux. CUDA works in WSL2.
- **Docker integration** — Docker Desktop on Windows uses WSL2 backend. Containerize your AI workflows.
- **Best of both** — VS Code on Windows, Ollama in WSL2, GPU shared between them.
- **Development workflow** — write code on Windows, run inference in WSL2, same filesystem.

## WSL2 AI setup

### Step 1: Enable WSL2 with GPU support

```powershell
# PowerShell (admin)
wsl --install -d Ubuntu
wsl --set-default-version 2
```

Verify WSL2 NVIDIA GPU access:

```bash
# Inside WSL2
nvidia-smi    # should show your RTX GPU
```

### Step 2: Install Ollama in WSL2

```bash
# Inside WSL2
curl -fsSL https://ollama.ai/install.sh | sh
ollama serve &
```

### Step 3: Install WSL2 Ollama Herd

```bash
# Inside WSL2
pip install ollama-herd
herd          # start WSL2 AI router on port 11435
herd-node     # register WSL2 as a node
```

### Step 4: Access from Windows

Your WSL2 AI endpoint is accessible from Windows at `http://localhost:11435` — WSL2 forwards ports automatically.

```powershell
# From Windows PowerShell
curl http://localhost:11435/api/tags    # see WSL2 AI models
```

## Use WSL2 AI

### Python (from Windows or WSL2)

```python
from openai import OpenAI

# Same URL works from Windows and WSL2
client = OpenAI(base_url="http://localhost:11435/v1", api_key="not-needed")

# WSL2 handles the inference via NVIDIA GPU
response = client.chat.completions.create(
    model="qwen3.5:32b",
    messages=[{"role": "user", "content": "Write a Docker Compose file for a Python API"}],
    stream=True,
)
for chunk in response:
    print(chunk.choices[0].delta.content or "", end="")
```

### VS Code + WSL2 AI

```json
// .vscode/settings.json — Continue.dev configuration
{
  "continue.models": [{
    "title": "WSL2 Local",
    "provider": "openai",
    "model": "codestral",
    "apiBase": "http://localhost:11435/v1",
    "apiKey": "not-needed"
  }]
}
```

### curl from WSL2

```bash
# WSL2 inference
curl http://localhost:11435/api/chat -d '{
  "model": "codestral",
  "messages": [{"role": "user", "content": "Refactor this Python function"}],
  "stream": false
}'
```

## WSL2 + Docker AI workflow

Run Ollama in Docker on WSL2 for containerized AI:

```bash
# WSL2 Docker + Ollama
docker run -d --gpus all -p 11434:11434 ollama/ollama

# Herd routes between Docker Ollama and native Ollama
pip install ollama-herd
herd &
herd-node
```

## WSL2 AI hardware guide

| Windows PC | GPU | WSL2 AI models |
|------------|-----|---------------|
| RTX 4090 desktop | 24GB shared with WSL2 | `llama3.3:70b`, `qwen3.5:32b` |
| RTX 4080 desktop | 16GB shared with WSL2 | `phi4`, `codestral`, `qwen3.5:14b` |
| RTX 4060 laptop | 8GB shared with WSL2 | `phi4-mini`, `gemma3:4b` |

> WSL2 shares GPU memory with Windows. Close GPU-heavy Windows apps for more WSL2 AI vRAM.

## WSL2 AI environment

```bash
# WSL2 Ollama optimization
export OLLAMA_KEEP_ALIVE=-1
export OLLAMA_MAX_LOADED_MODELS=-1

# Add to ~/.bashrc for persistence in WSL2
echo 'export OLLAMA_KEEP_ALIVE=-1' >> ~/.bashrc
echo 'export OLLAMA_MAX_LOADED_MODELS=-1' >> ~/.bashrc
```

## Monitor WSL2 AI

```bash
# WSL2 fleet status
curl -s http://localhost:11435/fleet/status | python3 -m json.tool

# WSL2 health checks
curl -s http://localhost:11435/dashboard/api/health | python3 -m json.tool
```

Dashboard at `http://localhost:11435/dashboard` — accessible from both Windows browser and WSL2.

## Also available on WSL2 AI

### Image generation
```bash
curl http://localhost:11435/api/generate-image \
  -d '{"model": "z-image-turbo", "prompt": "developer workspace", "width": 1024, "height": 1024}'
```

### Embeddings
```bash
curl http://localhost:11435/api/embed \
  -d '{"model": "nomic-embed-text", "input": "WSL2 Windows development AI"}'
```

## Full documentation

- [Agent Setup Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/agent-setup-guide.md)
- [API Reference](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/api-reference.md)

## Contribute

Ollama Herd is open source (MIT). WSL2 developers welcome:
- [Star on GitHub](https://github.com/geeks-accelerator/ollama-herd)
- [Open an issue](https://github.com/geeks-accelerator/ollama-herd/issues)

## Guardrails

- **WSL2 AI model downloads require explicit user confirmation.**
- **WSL2 AI model deletion requires explicit user confirmation.**
- Never delete or modify files in `~/.fleet-manager/`.
- No models are downloaded automatically — all pulls are user-initiated or require opt-in.
