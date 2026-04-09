---
name: linux-ai-server
description: Linux AI Server — turn Linux servers into a local AI inference cluster. Headless Linux AI with systemd, NVIDIA CUDA, and zero GUI overhead. Linux AI server for Llama, Qwen, DeepSeek, Phi, Mistral. Run a Linux AI server cluster on Ubuntu, Debian, RHEL, Fedora. Linux AI服务器本地推理。Servidor Linux IA para inferencia local.
version: 1.0.0
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"server","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","pip","nvidia-smi","systemctl"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["linux"]}}
---

# Linux AI Server — Headless AI Inference Cluster

Turn your Linux servers into a distributed AI inference cluster. No GUI, no Docker, no Kubernetes — just Linux + pip install. Your rack-mounted servers, cloud VMs, and spare Linux boxes all serve AI through one endpoint.

## Why Linux AI server

- **Zero GUI overhead** — headless Linux AI uses all resources for inference, not desktops
- **systemd native** — Linux AI server starts on boot, restarts on failure, logs to journald
- **SSH management** — manage your Linux AI server cluster entirely over SSH
- **Any Linux distro** — Ubuntu, Debian, RHEL, Fedora, Arch, Alpine — if it runs Ollama, it joins the fleet
- **NVIDIA CUDA** — Linux AI server uses NVIDIA GPUs natively. No compatibility issues.
- **Fleet routing** — multiple Linux AI servers share the load. 7-signal scoring picks the best one.

## Linux AI server setup

### Quick install on each Linux server

```bash
# Install Ollama on Linux
curl -fsSL https://ollama.ai/install.sh | sh

# Install the Linux AI router
pip install ollama-herd
```

### Linux AI server router (pick one server)

```bash
herd          # start Linux AI server router on port 11435
herd-node     # register this Linux AI server
```

### Linux AI server nodes (all other servers)

```bash
herd-node     # auto-discovers the Linux AI server router
# Or explicit: herd-node --router-url http://router-ip:11435
```

### Linux AI server systemd services

```bash
# /etc/systemd/system/herd-router.service
[Unit]
Description=Linux AI Server Router
After=network.target ollama.service

[Service]
Type=simple
ExecStart=/usr/local/bin/herd
Restart=always
RestartSec=5
User=ollama

[Install]
WantedBy=multi-user.target
```

```bash
# /etc/systemd/system/herd-node.service
[Unit]
Description=Linux AI Server Node
After=network.target ollama.service

[Service]
Type=simple
ExecStart=/usr/local/bin/herd-node
Restart=always
RestartSec=5
User=ollama

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now herd-router    # on the Linux AI router
sudo systemctl enable --now herd-node      # on all Linux AI nodes
```

## Linux AI server hardware guide

| Linux AI Server | GPU | RAM | Best Linux AI models |
|-----------------|-----|-----|---------------------|
| Rack server (NVIDIA A100) | 80GB | 256GB | `deepseek-v3`, `qwen3.5:72b` — frontier |
| Rack server (NVIDIA L40S) | 48GB | 128GB | `llama3.3:70b`, `qwen3.5:32b` |
| Desktop server (RTX 4090) | 24GB | 64GB | `llama3.3:70b` (Q4), `deepseek-r1:32b` |
| Mini PC / NUC (no GPU) | CPU | 32GB | `phi4`, `gemma3:12b` — CPU inference |
| Cloud VM (no GPU) | CPU | 16GB | `phi4-mini`, `gemma3:4b` |
| Raspberry Pi 5 | CPU | 8GB | `gemma3:1b`, `phi4-mini` — edge AI |

> Linux AI server works with NVIDIA CUDA GPUs, AMD ROCm (experimental), and CPU-only inference.

## Use your Linux AI server

### OpenAI SDK

```python
from openai import OpenAI

# Your Linux AI server endpoint
client = OpenAI(base_url="http://linux-ai-server:11435/v1", api_key="not-needed")

response = client.chat.completions.create(
    model="llama3.3:70b",
    messages=[{"role": "user", "content": "Write a Terraform module for AWS ECS"}],
    stream=True,
)
for chunk in response:
    print(chunk.choices[0].delta.content or "", end="")
```

### curl from any machine

```bash
# Hit your Linux AI server from anywhere on the network
curl http://linux-ai-server:11435/api/chat -d '{
  "model": "codestral",
  "messages": [{"role": "user", "content": "Write a Dockerfile for a FastAPI app"}],
  "stream": false
}'
```

## Linux AI server environment

```bash
# Optimize Linux AI server Ollama
sudo systemctl edit ollama
# Add under [Service]:
#   Environment="OLLAMA_KEEP_ALIVE=-1"
#   Environment="OLLAMA_MAX_LOADED_MODELS=-1"
#   Environment="OLLAMA_NUM_PARALLEL=2"
sudo systemctl restart ollama
```

## Linux AI server firewall

```bash
# UFW (Ubuntu/Debian)
sudo ufw allow 11435/tcp

# firewalld (RHEL/Fedora)
sudo firewall-cmd --add-port=11435/tcp --permanent && sudo firewall-cmd --reload
```

## Linux AI server monitoring

```bash
# Linux AI server fleet status
curl -s http://localhost:11435/fleet/status | python3 -m json.tool

# Linux AI server health — 16 automated checks
curl -s http://localhost:11435/dashboard/api/health | python3 -m json.tool

# Linux AI server traces — recent requests
curl -s "http://localhost:11435/dashboard/api/traces?limit=10" | python3 -m json.tool

# Linux AI server logs
journalctl -u herd-router -f
tail -f ~/.fleet-manager/logs/herd.jsonl.$(date +%Y-%m-%d)
```

Dashboard at `http://linux-ai-server:11435/dashboard` — access from any browser on the network.

## Also available on Linux AI server

### Image generation
```bash
curl http://localhost:11435/api/generate-image \
  -d '{"model": "z-image-turbo", "prompt": "server rack visualization", "width": 1024, "height": 1024}'
```

### Embeddings
```bash
curl http://localhost:11435/api/embed \
  -d '{"model": "nomic-embed-text", "input": "Linux AI server headless inference"}'
```

## Full documentation

- [Agent Setup Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/agent-setup-guide.md)
- [API Reference](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/api-reference.md)
- [Configuration Reference](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/configuration-reference.md)

## Contribute

Ollama Herd is open source (MIT). Linux server admins welcome:
- [Star on GitHub](https://github.com/geeks-accelerator/ollama-herd)
- [Open an issue](https://github.com/geeks-accelerator/ollama-herd/issues)

## Guardrails

- **Linux AI server model downloads require explicit user confirmation.**
- **Linux AI server model deletion requires explicit user confirmation.**
- Never delete or modify files in `~/.fleet-manager/`.
- No models are downloaded automatically — all pulls are user-initiated or require opt-in.
