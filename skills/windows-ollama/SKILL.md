---
name: windows-ollama
description: Windows Ollama — run Ollama on Windows with fleet routing across multiple Windows PCs. Windows Ollama setup for Llama, Qwen, DeepSeek, Phi, Mistral. Route Ollama inference across Windows machines with NVIDIA RTX GPUs. Windows Ollama load balancing, health monitoring, and real-time dashboard. Windows Ollama本地推理。Windows Ollama enrutador IA.
version: 1.0.0
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"windows","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","pip","nvidia-smi"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["windows"]}}
---

# Windows Ollama — Fleet Routing for Ollama on Windows

Run Ollama on Windows with multi-machine load balancing. Windows Ollama Herd turns multiple Windows PCs running Ollama into one smart endpoint. Your gaming desktop, your work laptop, your old tower — all serving AI requests through one Windows Ollama URL.

## Windows Ollama setup

### Step 1: Install Ollama on Windows

Download Ollama from [ollama.ai](https://ollama.ai) and install. Ollama on Windows runs natively with NVIDIA GPU support.

### Step 2: Install Windows Ollama Herd

```powershell
pip install ollama-herd
```

### Step 3: Start the Windows Ollama router

On one Windows PC (your router):
```powershell
herd          # starts Windows Ollama router on port 11435
herd-node     # registers this Windows PC
```

On every other Windows PC:
```powershell
herd-node     # auto-discovers the Windows Ollama router
```

> **mDNS issues on Windows?** Corporate networks often block mDNS. Use explicit connection: `herd-node --router-url http://router-ip:11435`

### Step 4: Verify Windows Ollama fleet

```powershell
curl http://localhost:11435/fleet/status
```

You should see all your Windows Ollama nodes listed.

## Use Windows Ollama

### OpenAI SDK (Python)

```python
from openai import OpenAI

# Your Windows Ollama fleet
client = OpenAI(base_url="http://localhost:11435/v1", api_key="not-needed")

response = client.chat.completions.create(
    model="llama3.3:70b",
    messages=[{"role": "user", "content": "Write a PowerShell script to monitor GPU usage"}],
    stream=True,
)
for chunk in response:
    print(chunk.choices[0].delta.content or "", end="")
```

### curl (Ollama format)

```powershell
curl http://localhost:11435/api/chat -d '{
  "model": "qwen3.5:32b",
  "messages": [{"role": "user", "content": "Explain Windows GPU drivers"}],
  "stream": false
}'
```

### curl (OpenAI format)

```powershell
curl http://localhost:11435/v1/chat/completions `
  -H "Content-Type: application/json" `
  -d '{"model": "phi4", "messages": [{"role": "user", "content": "Hello from Windows"}]}'
```

## Windows Ollama environment setup

Keep models loaded in GPU memory on Windows:

```powershell
# Windows environment variables (PowerShell)
[System.Environment]::SetEnvironmentVariable("OLLAMA_KEEP_ALIVE", "-1", "User")
[System.Environment]::SetEnvironmentVariable("OLLAMA_MAX_LOADED_MODELS", "-1", "User")
[System.Environment]::SetEnvironmentVariable("OLLAMA_NUM_PARALLEL", "2", "User")
# Restart Ollama from the Windows system tray
```

Verify Windows Ollama settings:

```powershell
[System.Environment]::GetEnvironmentVariable("OLLAMA_KEEP_ALIVE", "User")
```

## Windows Ollama model recommendations

| Windows PC | GPU | Best Windows Ollama models |
|------------|-----|---------------------------|
| Gaming desktop (RTX 4090) | 24GB vRAM | `llama3.3:70b`, `qwen3.5:32b`, `deepseek-r1:32b` |
| Gaming desktop (RTX 4080) | 16GB vRAM | `qwen3.5:14b`, `phi4`, `codestral` |
| Work laptop (RTX 4060) | 8GB vRAM | `phi4-mini`, `gemma3:4b`, `llama3.2:3b` |
| Office desktop (no GPU) | CPU only | `phi4-mini`, `gemma3:1b` — slower but works |

> Windows Ollama works with or without an NVIDIA GPU. CPU inference is slower but functional.

## Windows Ollama troubleshooting

### Windows Firewall

Allow Ollama Herd through Windows Firewall:

```powershell
netsh advfirewall firewall add rule name="Ollama Herd" dir=in action=allow protocol=tcp localport=11435
```

### Check Windows Ollama is listening

```powershell
netstat -ano | findstr :11435    # Windows Ollama router
netstat -ano | findstr :11434    # Ollama itself
```

### Windows Ollama logs

```powershell
# Check Windows Ollama fleet health
curl http://localhost:11435/dashboard/api/health | python3 -m json.tool
```

## Monitor Windows Ollama

```powershell
# Windows Ollama fleet status
curl -s http://localhost:11435/fleet/status | python3 -m json.tool

# Models on Windows Ollama nodes
curl -s http://localhost:11435/api/ps | python3 -m json.tool

# Windows Ollama health checks
curl -s http://localhost:11435/dashboard/api/health | python3 -m json.tool
```

Dashboard at `http://localhost:11435/dashboard` — live Windows Ollama monitoring.

## Also available on Windows Ollama

### Image generation (Ollama native — works on Windows)
```powershell
curl http://localhost:11435/api/generate-image `
  -d '{"model": "z-image-turbo", "prompt": "Windows desktop wallpaper", "width": 1024, "height": 1024}'
```

### Embeddings
```powershell
curl http://localhost:11435/api/embed `
  -d '{"model": "nomic-embed-text", "input": "Windows Ollama local inference"}'
```

## Full documentation

- [Agent Setup Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/agent-setup-guide.md)
- [API Reference](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/api-reference.md)

## Contribute

Ollama Herd is open source (MIT). Windows Ollama users welcome:
- [Star on GitHub](https://github.com/geeks-accelerator/ollama-herd)
- [Open an issue](https://github.com/geeks-accelerator/ollama-herd/issues)

## Guardrails

- **Windows Ollama model downloads require explicit user confirmation.**
- **Windows Ollama model deletion requires explicit user confirmation.**
- Never delete or modify files in `~/.fleet-manager/`.
- No models are downloaded automatically — all pulls are user-initiated or require opt-in.
