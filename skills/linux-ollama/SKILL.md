---
name: linux-ollama
description: Linux Ollama — run Ollama on Linux with fleet routing across multiple Linux machines. Linux Ollama setup for Llama, Qwen, DeepSeek, Phi, Mistral. Route Ollama inference across Linux servers, desktops, and edge devices. Linux Ollama load balancing with systemd integration. Linux Ollama本地推理。Linux Ollama enrutador IA.
version: 1.0.0
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"penguin","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","pip","nvidia-smi","systemctl"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["linux"]}}
---

# Linux Ollama — Fleet Routing for Ollama on Linux

Run Ollama on Linux with multi-machine load balancing. Linux Ollama Herd turns multiple Linux machines into one smart Ollama endpoint. Your server rack, your desktop, your edge device — all serving AI through one Linux Ollama URL.

## Linux Ollama setup

### Step 1: Install Ollama on Linux

```bash
curl -fsSL https://ollama.ai/install.sh | sh
```

### Step 2: Install Linux Ollama Herd

```bash
pip install ollama-herd
```

### Step 3: Start the Linux Ollama router

On one Linux machine (your router):
```bash
herd          # starts Linux Ollama router on port 11435
herd-node     # registers this Linux machine
```

On every other Linux machine:
```bash
herd-node     # auto-discovers the Linux Ollama router via mDNS
```

> No mDNS? Connect Linux nodes directly: `herd-node --router-url http://router-ip:11435`

## Linux Ollama systemd integration

Run Linux Ollama Herd as a systemd service for automatic startup:

```bash
# /etc/systemd/system/ollama-herd.service
[Unit]
Description=Linux Ollama Herd Router
After=network.target ollama.service

[Service]
Type=simple
ExecStart=/usr/local/bin/herd
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable ollama-herd
sudo systemctl start ollama-herd
```

Node agent as a Linux systemd service:

```bash
# /etc/systemd/system/ollama-herd-node.service
[Unit]
Description=Linux Ollama Herd Node Agent
After=network.target ollama.service

[Service]
Type=simple
ExecStart=/usr/local/bin/herd-node
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

## Use Linux Ollama

### OpenAI SDK

```python
from openai import OpenAI

# Your Linux Ollama fleet
client = OpenAI(base_url="http://localhost:11435/v1", api_key="not-needed")

response = client.chat.completions.create(
    model="llama3.3:70b",
    messages=[{"role": "user", "content": "Write a systemd service file for a Python API"}],
    stream=True,
)
for chunk in response:
    print(chunk.choices[0].delta.content or "", end="")
```

### curl (Ollama format)

```bash
# Linux Ollama inference
curl http://localhost:11435/api/chat -d '{
  "model": "qwen3.5:32b",
  "messages": [{"role": "user", "content": "Explain Linux process scheduling"}],
  "stream": false
}'
```

## Linux Ollama environment setup

```bash
# Optimize Linux Ollama performance via systemd
sudo systemctl edit ollama
# Add under [Service]:
#   Environment="OLLAMA_KEEP_ALIVE=-1"
#   Environment="OLLAMA_MAX_LOADED_MODELS=-1"
#   Environment="OLLAMA_NUM_PARALLEL=2"
sudo systemctl restart ollama
```

Or via shell profile:

```bash
echo 'export OLLAMA_KEEP_ALIVE=-1' >> ~/.bashrc
echo 'export OLLAMA_MAX_LOADED_MODELS=-1' >> ~/.bashrc
source ~/.bashrc
```

## Linux Ollama GPU support

| Linux GPU | vRAM | Best Linux Ollama models |
|-----------|------|-------------------------|
| NVIDIA RTX 4090 | 24GB | `llama3.3:70b`, `qwen3.5:32b` |
| NVIDIA A100 | 40/80GB | `deepseek-v3`, `qwen3.5:72b` |
| NVIDIA L40S | 48GB | `llama3.3:70b` (full precision) |
| AMD ROCm (experimental) | varies | Ollama ROCm support on Linux |
| CPU only | system RAM | `phi4-mini`, `gemma3:1b` — slower but works |

> Linux Ollama supports NVIDIA CUDA, experimental AMD ROCm, and CPU-only inference.

## Linux Ollama firewall

```bash
# UFW (Ubuntu/Debian)
sudo ufw allow 11435/tcp

# firewalld (RHEL/Fedora)
sudo firewall-cmd --add-port=11435/tcp --permanent
sudo firewall-cmd --reload

# iptables
sudo iptables -A INPUT -p tcp --dport 11435 -j ACCEPT
```

## Monitor Linux Ollama

```bash
# Linux Ollama fleet status
curl -s http://localhost:11435/fleet/status | python3 -m json.tool

# Linux Ollama health — 16 automated checks
curl -s http://localhost:11435/dashboard/api/health | python3 -m json.tool

# Models on Linux Ollama nodes
curl -s http://localhost:11435/api/ps | python3 -m json.tool
```

Dashboard at `http://localhost:11435/dashboard` — live Linux Ollama monitoring.

## Linux Ollama logs

```bash
# JSONL structured logs
tail -f ~/.fleet-manager/logs/herd.jsonl.$(date +%Y-%m-%d) | python3 -m json.tool

# Check for Linux Ollama errors
grep '"level":"ERROR"' ~/.fleet-manager/logs/herd.jsonl.$(date +%Y-%m-%d)
```

## Also available on Linux Ollama

### Image generation
```bash
curl http://localhost:11435/api/generate-image \
  -d '{"model": "z-image-turbo", "prompt": "Linux penguin in cyberspace", "width": 1024, "height": 1024}'
```

### Embeddings
```bash
curl http://localhost:11435/api/embed \
  -d '{"model": "nomic-embed-text", "input": "Linux Ollama local inference"}'
```

## Full documentation

- [Agent Setup Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/agent-setup-guide.md)
- [API Reference](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/api-reference.md)

## Contribute

Ollama Herd is open source (MIT). Linux Ollama users welcome:
- [Star on GitHub](https://github.com/geeks-accelerator/ollama-herd)
- [Open an issue](https://github.com/geeks-accelerator/ollama-herd/issues)

## Guardrails

- **Linux Ollama model downloads require explicit user confirmation.**
- **Linux Ollama model deletion requires explicit user confirmation.**
- Never delete or modify files in `~/.fleet-manager/`.
- No models are downloaded automatically — all pulls are user-initiated or require opt-in.
