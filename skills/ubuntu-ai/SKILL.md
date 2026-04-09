---
name: ubuntu-ai
description: Ubuntu AI — build a local AI platform on Ubuntu and Debian. Ubuntu AI across x86 desktops, ARM edge devices, Raspberry Pi, Jetson Orin, and cloud VMs. Heterogeneous Ubuntu AI cluster with NVIDIA CUDA, AMD ROCm, and CPU inference. LLM, image generation, and embeddings on Ubuntu. Debian AI with the same stack. Ubuntu AI本地推理平台。Ubuntu IA plataforma local.
version: 1.0.1
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"penguin","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","pip","nvidia-smi","systemctl","apt"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["linux"]}}
---

# Ubuntu AI — Build a Local AI Platform on Ubuntu

Build a complete local AI platform on Ubuntu. LLM inference, image generation, and embeddings across desktops, servers, Raspberry Pis, and Jetson boards. Ubuntu AI supports x86-64 and ARM in the same cluster — heterogeneous hardware, one endpoint. No cloud APIs, no subscriptions.

## Why Ubuntu AI

- **Largest Linux ecosystem** — Ubuntu is the #1 Linux distribution for AI/ML workloads
- **apt-get native** — install dependencies with the Ubuntu package manager you know
- **systemd integration** — Ubuntu AI starts on boot, restarts on failure
- **NVIDIA CUDA on Ubuntu** — best GPU driver support on any Linux distribution
- **ARM + x86** — Ubuntu AI runs on x86-64 servers and ARM devices (Raspberry Pi, Jetson)
- **Free forever** — Ubuntu is free, Ollama is free, Ollama Herd is free

## Ubuntu AI quick start

```bash
# Ubuntu prerequisites
sudo apt update
sudo apt install python3-pip curl

# Install Ollama on Ubuntu
curl -fsSL https://ollama.ai/install.sh | sh

# Install Ubuntu AI router
pip install ollama-herd

# Start Ubuntu AI
herd          # Ubuntu AI router on port 11435
herd-node     # register this Ubuntu machine
```

On other Ubuntu/Debian machines:
```bash
pip install ollama-herd
herd-node     # auto-discovers the Ubuntu AI router
```

## Ubuntu AI with NVIDIA CUDA

```bash
# Install NVIDIA drivers on Ubuntu
sudo apt install nvidia-driver-550
sudo reboot

# Verify Ubuntu NVIDIA CUDA
nvidia-smi

# Ollama on Ubuntu automatically detects NVIDIA CUDA
ollama ps    # should show GPU acceleration
```

## Ubuntu AI systemd services

```bash
# Ubuntu AI router service
sudo tee /etc/systemd/system/herd-router.service << 'EOF'
[Unit]
Description=Ubuntu AI Router
After=network.target ollama.service

[Service]
Type=simple
ExecStart=/usr/local/bin/herd
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable --now herd-router
```

```bash
# Ubuntu AI node service
sudo tee /etc/systemd/system/herd-node.service << 'EOF'
[Unit]
Description=Ubuntu AI Node
After=network.target ollama.service

[Service]
Type=simple
ExecStart=/usr/local/bin/herd-node
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable --now herd-node
```

## Use Ubuntu AI

### OpenAI SDK

```python
from openai import OpenAI

# Your Ubuntu AI endpoint
client = OpenAI(base_url="http://localhost:11435/v1", api_key="not-needed")

response = client.chat.completions.create(
    model="qwen3.5:32b",
    messages=[{"role": "user", "content": "Write an Ubuntu systemd timer for daily backups"}],
    stream=True,
)
for chunk in response:
    print(chunk.choices[0].delta.content or "", end="")
```

### curl

```bash
# Ubuntu AI inference
curl http://localhost:11435/api/chat -d '{
  "model": "llama3.3:70b",
  "messages": [{"role": "user", "content": "Explain Ubuntu package management"}],
  "stream": false
}'
```

## Ubuntu AI hardware guide

| Ubuntu Device | GPU | Best Ubuntu AI models |
|---------------|-----|----------------------|
| Ubuntu desktop (RTX 4090) | 24GB | `llama3.3:70b`, `qwen3.5:32b` |
| Ubuntu desktop (RTX 4080) | 16GB | `phi4`, `codestral`, `qwen3.5:14b` |
| Ubuntu Server (A100) | 80GB | `deepseek-v3`, `qwen3.5:72b` |
| Ubuntu Server (no GPU) | CPU | `phi4-mini`, `gemma3:4b` |
| Raspberry Pi 5 (Ubuntu) | CPU | `gemma3:1b`, `phi4-mini` — edge Ubuntu AI |
| Jetson Orin Nano (Ubuntu) | 8GB shared | `phi4`, `llama3.2:3b` — ARM Ubuntu AI |

> Ubuntu AI supports x86-64 and ARM architectures. Heterogeneous Ubuntu AI clusters work automatically.

## Ubuntu AI environment

```bash
# Optimize Ollama on Ubuntu
sudo systemctl edit ollama
# Add under [Service]:
#   Environment="OLLAMA_KEEP_ALIVE=-1"
#   Environment="OLLAMA_MAX_LOADED_MODELS=-1"
sudo systemctl restart ollama
```

## Ubuntu AI firewall

```bash
# Ubuntu UFW
sudo ufw allow 11435/tcp
sudo ufw reload
```

## Monitor Ubuntu AI

```bash
# Ubuntu AI fleet status
curl -s http://localhost:11435/fleet/status | python3 -m json.tool

# Ubuntu AI health — 16 automated checks
curl -s http://localhost:11435/dashboard/api/health | python3 -m json.tool

# Ubuntu AI logs
journalctl -u herd-router -f
```

Dashboard at `http://localhost:11435/dashboard` — live Ubuntu AI monitoring.

## Also available on Ubuntu AI

### Image generation
```bash
curl http://localhost:11435/api/generate-image \
  -d '{"model": "z-image-turbo", "prompt": "Ubuntu penguin mascot", "width": 1024, "height": 1024}'
```

### Embeddings
```bash
curl http://localhost:11435/api/embed \
  -d '{"model": "nomic-embed-text", "input": "Ubuntu AI local inference Debian"}'
```

## Full documentation

- [Agent Setup Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/agent-setup-guide.md)
- [API Reference](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/api-reference.md)

## Contribute

Ollama Herd is open source (MIT). Ubuntu AI users welcome:
- [Star on GitHub](https://github.com/geeks-accelerator/ollama-herd)
- [Open an issue](https://github.com/geeks-accelerator/ollama-herd/issues)

## Guardrails

- **Ubuntu AI model downloads require explicit user confirmation.**
- **Ubuntu AI model deletion requires explicit user confirmation.**
- Never delete or modify files in `~/.fleet-manager/`.
- No models are downloaded automatically — all pulls are user-initiated or require opt-in.
