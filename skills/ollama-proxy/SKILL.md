---
name: ollama-proxy
description: Ollama proxy — one endpoint that routes to multiple Ollama instances. Drop-in replacement for localhost:11434. Same API, same model names, but requests go to the best available device across your network. Auto-discovers Ollama nodes, scores on 7 signals, retries on failure. Works with Open WebUI, LangChain, Aider, Continue.dev, and any Ollama client.
version: 1.0.0
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"globe","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","pip"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["darwin","linux"]}}
---

# Ollama Proxy — One Endpoint for All Your Ollama Instances

You have Ollama running on multiple machines. Instead of hardcoding IPs and manually picking which machine to hit, point everything at the proxy. It routes to the best available device automatically.

```
Before:  App → http://macmini:11434  (one machine, hope it's not busy)
After:   App → http://proxy:11435   (best machine picked automatically)
```

## Setup

```bash
pip install ollama-herd    # PyPI: https://pypi.org/project/ollama-herd/
```

**On one machine (the proxy):**
```bash
herd    # starts on port 11435
```

**On every machine running Ollama:**
```bash
herd-node    # discovers the proxy automatically on your network
```

Now point your apps at `http://proxy-ip:11435` instead of `http://localhost:11434`. Same API, same model names, same streaming — just smarter routing.

## Drop-in Ollama replacement

Every Ollama API endpoint works through the proxy:

```bash
# Chat (same as Ollama)
curl http://proxy-ip:11435/api/chat -d '{
  "model": "llama3.3:70b",
  "messages": [{"role": "user", "content": "Hello"}]
}'

# Generate (same as Ollama)
curl http://proxy-ip:11435/api/generate -d '{
  "model": "qwen3:32b",
  "prompt": "Explain quantum computing"
}'

# List models (aggregated from all nodes)
curl http://proxy-ip:11435/api/tags

# List loaded models (across all nodes)
curl http://proxy-ip:11435/api/ps
```

### OpenAI-compatible API (bonus)

The proxy also exposes an OpenAI-compatible endpoint — same models, no code changes:

```python
from openai import OpenAI

client = OpenAI(base_url="http://proxy-ip:11435/v1", api_key="not-needed")
response = client.chat.completions.create(
    model="llama3.3:70b",
    messages=[{"role": "user", "content": "Hello"}],
    stream=True,
)
```

## What the proxy does that Ollama doesn't

| Feature | Direct Ollama | Ollama Proxy (Herd) |
|---------|--------------|-------------------|
| Multiple machines | Manual IP switching | Automatic routing |
| Load balancing | None | 7-signal scoring |
| Failover | None | Auto-retry on next node |
| Model discovery | Per-machine | Fleet-wide aggregated |
| Queue management | None | Per-node:model queues |
| Dashboard | None | Real-time web UI |
| Health checks | None | 11 automated checks |
| Request tracing | None | SQLite trace log |
| Image generation | None | mflux + DiffusionKit + Ollama native |
| Speech-to-text | None | Qwen3-ASR routing |

## Works with your existing tools

Just change the Ollama URL — no other configuration needed:

| Tool | Before | After |
|------|--------|-------|
| **Open WebUI** | `http://localhost:11434` | `http://proxy-ip:11435` |
| **Aider** | `--openai-api-base http://localhost:11434/v1` | `--openai-api-base http://proxy-ip:11435/v1` |
| **Continue.dev** | Ollama at localhost | Ollama at `proxy-ip:11435` |
| **LangChain** | `Ollama(base_url="http://localhost:11434")` | `Ollama(base_url="http://proxy-ip:11435")` |
| **LiteLLM** | `ollama/llama3.3:70b` | `ollama/llama3.3:70b` (change base URL in config) |
| **CrewAI** | `OPENAI_API_BASE=http://localhost:11434/v1` | `OPENAI_API_BASE=http://proxy-ip:11435/v1` |

## How routing works

When a request arrives, the proxy scores all nodes that have the requested model:

1. **Thermal state** — is the model already loaded in memory (hot)?
2. **Memory fit** — does the node have enough free RAM?
3. **Queue depth** — is the node busy with other requests?
4. **Latency history** — how fast has this node been recently?
5. **Role affinity** — big models prefer big machines
6. **Availability trend** — is this node reliably available?
7. **Context fit** — does the loaded context window match the request?

The highest-scoring node wins. If it fails, the proxy retries on the next best node automatically.

## Monitor your fleet

Dashboard at `http://proxy-ip:11435/dashboard` — see every node, every model, every queue in real time.

```bash
# Fleet overview
curl -s http://proxy-ip:11435/fleet/status | python3 -m json.tool

# Health checks
curl -s http://proxy-ip:11435/dashboard/api/health | python3 -m json.tool
```

## Full documentation

- [Agent Setup Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/agent-setup-guide.md)
- [API Reference](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/api-reference.md)
- [Configuration](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/configuration-reference.md)

## Contribute

Ollama Herd is open source (MIT). We welcome contributions:
- [Star on GitHub](https://github.com/geeks-accelerator/ollama-herd) — help others find the project
- [Open an issue](https://github.com/geeks-accelerator/ollama-herd/issues) — bug reports, feature requests
- **PRs welcome** — `CLAUDE.md` gives AI agents full context. 412 tests, async Python.

## Guardrails

- **No automatic model downloads** — model pulls require explicit user confirmation.
- **Model deletion requires explicit user confirmation.**
- **All requests stay local** — no data leaves your network.
- Never delete or modify files in `~/.fleet-manager/`.
