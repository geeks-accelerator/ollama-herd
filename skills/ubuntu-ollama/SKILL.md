---
name: ubuntu-ollama
description: Ubuntu Ollama — run Ollama on Ubuntu with fleet routing across multiple Ubuntu machines. Ubuntu Ollama setup with apt, systemd, and NVIDIA CUDA. Route Ollama inference across Ubuntu servers and desktops. Ubuntu Ollama load balancing, auto-discovery, and health monitoring. Ubuntu Ollama本地推理。Ubuntu Ollama enrutador IA.
version: 1.0.0
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"penguin","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","pip","nvidia-smi","systemctl","apt"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["linux"]}}
---

# Ubuntu Ollama — Fleet Routing for Ollama on Ubuntu

Run Ollama on Ubuntu with multi-machine load balancing. Ubuntu Ollama Herd turns your Ubuntu servers and desktops into one smart Ollama endpoint. Install with apt + pip, manage with systemd, monitor with the web dashboard.

## Ubuntu Ollama setup

### Step 1: Install Ollama on Ubuntu

```bash
# Install Ollama on Ubuntu
curl -fsSL https://ollama.ai/install.sh | sh

# Verify Ollama is running on Ubuntu
ollama --version
systemctl status ollama
```

### Step 2: Install Ubuntu Ollama Herd

```bash
# Ubuntu prerequisites
sudo apt update && sudo apt install python3-pip curl -y

# Install Ubuntu Ollama fleet router
pip install ollama-herd
```

### Step 3: Start Ubuntu Ollama router

On one Ubuntu machine (the router):
```bash
herd          # start Ubuntu Ollama router on port 11435
herd-node     # register this Ubuntu Ollama node
```

On every other Ubuntu machine:
```bash
herd-node     # auto-discovers the Ubuntu Ollama router via mDNS
```

> No mDNS? Connect Ubuntu Ollama nodes directly: `herd-node --router-url http://router-ip:11435`

### Step 4: Verify Ubuntu Ollama fleet

```bash
curl -s http://localhost:11435/fleet/status | python3 -m json.tool
```

## Ubuntu Ollama systemd services

Run Ubuntu Ollama as systemd services for automatic startup:

```bash
# Ubuntu Ollama router service
sudo tee /etc/systemd/system/herd-router.service << 'EOF'
[Unit]
Description=Ubuntu Ollama Router
After=network.target ollama.service

[Service]
Type=simple
ExecStart=/usr/local/bin/herd
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# Ubuntu Ollama node service
sudo tee /etc/systemd/system/herd-node.service << 'EOF'
[Unit]
Description=Ubuntu Ollama Node
After=network.target ollama.service

[Service]
Type=simple
ExecStart=/usr/local/bin/herd-node
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable --now herd-router
sudo systemctl enable --now herd-node
```

## Use Ubuntu Ollama

### OpenAI SDK

```python
from openai import OpenAI

# Your Ubuntu Ollama fleet
client = OpenAI(base_url="http://localhost:11435/v1", api_key="not-needed")

response = client.chat.completions.create(
    model="llama3.3:70b",
    messages=[{"role": "user", "content": "Write an Ubuntu cron job for log rotation"}],
    stream=True,
)
for chunk in response:
    print(chunk.choices[0].delta.content or "", end="")
```

### curl (Ollama format)

```bash
# Ubuntu Ollama inference
curl http://localhost:11435/api/chat -d '{
  "model": "qwen3.5:32b",
  "messages": [{"role": "user", "content": "Explain Ubuntu apt package management"}],
  "stream": false
}'
```

### curl (OpenAI format)

```bash
curl http://localhost:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "phi4", "messages": [{"role": "user", "content": "Hello from Ubuntu Ollama"}]}'
```

## Ubuntu Ollama NVIDIA CUDA setup

```bash
# Install NVIDIA drivers on Ubuntu for Ollama CUDA
sudo apt install nvidia-driver-550 -y
sudo reboot

# Verify Ubuntu NVIDIA CUDA
nvidia-smi

# Ubuntu Ollama automatically uses CUDA when NVIDIA drivers are installed
ollama ps    # should show GPU acceleration
```

## Ubuntu Ollama environment

```bash
# Optimize Ollama on Ubuntu via systemd
sudo systemctl edit ollama
# Add under [Service]:
#   Environment="OLLAMA_KEEP_ALIVE=-1"
#   Environment="OLLAMA_MAX_LOADED_MODELS=-1"
#   Environment="OLLAMA_NUM_PARALLEL=2"
sudo systemctl restart ollama

# Verify Ubuntu Ollama settings
systemctl show ollama | grep Environment
```

## Ubuntu Ollama model recommendations

| Ubuntu Machine | GPU | Best Ubuntu Ollama models |
|---------------|-----|--------------------------|
| Ubuntu desktop (RTX 4090) | 24GB | `llama3.3:70b`, `qwen3.5:32b`, `deepseek-r1:32b` |
| Ubuntu desktop (RTX 4080) | 16GB | `phi4`, `codestral`, `qwen3.5:14b` |
| Ubuntu Server (A100) | 80GB | `deepseek-v3`, `qwen3.5:72b` |
| Ubuntu Server (no GPU) | CPU | `phi4-mini`, `gemma3:4b` |
| Ubuntu on Raspberry Pi 5 | CPU | `gemma3:1b`, `phi4-mini` |

## Ubuntu Ollama firewall

```bash
# Ubuntu UFW
sudo ufw allow 11435/tcp
sudo ufw reload
```

## Monitor Ubuntu Ollama

```bash
# Ubuntu Ollama fleet status
curl -s http://localhost:11435/fleet/status | python3 -m json.tool

# Ubuntu Ollama health — 15 automated checks
curl -s http://localhost:11435/dashboard/api/health | python3 -m json.tool

# Ubuntu Ollama models loaded
curl -s http://localhost:11435/api/ps | python3 -m json.tool

# Ubuntu Ollama logs
journalctl -u herd-router -f
tail -f ~/.fleet-manager/logs/herd.jsonl.$(date +%Y-%m-%d)
```

Dashboard at `http://localhost:11435/dashboard` — live Ubuntu Ollama monitoring.

## Also available on Ubuntu Ollama

### Image generation
```bash
curl http://localhost:11435/api/generate-image \
  -d '{"model": "z-image-turbo", "prompt": "Ubuntu penguin in space", "width": 1024, "height": 1024}'
```

### Embeddings
```bash
curl http://localhost:11435/api/embed \
  -d '{"model": "nomic-embed-text", "input": "Ubuntu Ollama local inference routing"}'
```

## Full documentation

- [Agent Setup Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/agent-setup-guide.md)
- [API Reference](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/api-reference.md)

## Contribute

Ollama Herd is open source (MIT). Ubuntu Ollama users welcome:
- [Star on GitHub](https://github.com/geeks-accelerator/ollama-herd)
- [Open an issue](https://github.com/geeks-accelerator/ollama-herd/issues)

## Guardrails

- **Ubuntu Ollama model downloads require explicit user confirmation.**
- **Ubuntu Ollama model deletion requires explicit user confirmation.**
- Never delete or modify files in `~/.fleet-manager/`.
- No models are downloaded automatically — all pulls are user-initiated or require opt-in.
