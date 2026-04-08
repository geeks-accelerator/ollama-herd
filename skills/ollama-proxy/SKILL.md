---
name: ollama-proxy
description: Ollama proxy — one endpoint that routes to multiple Ollama instances. Drop-in Ollama proxy replacement for localhost:11434. Same Ollama API, same model names, but the Ollama proxy routes requests to the best device. Auto-discovers Ollama nodes, scores on 7 signals, retries on failure. Works with Open WebUI, LangChain, Aider. Ollama代理 | proxy Ollama
version: 1.0.3
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"globe","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","pip"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["darwin","linux","windows"]}}
---

# Ollama Proxy — One Endpoint for All Your Ollama Instances

You have Ollama running on multiple machines. Instead of hardcoding IPs and manually picking which Ollama instance to hit, point everything at the Ollama proxy. The Ollama proxy routes to the best available device automatically.

```
Before:  App → http://macmini:11434  (one Ollama instance, hope it's not busy)
After:   App → http://ollama-proxy:11435   (Ollama proxy picks the best machine)
```

## Set up the Ollama proxy

```bash
pip install ollama-herd    # PyPI: https://pypi.org/project/ollama-herd/
```

**On one machine (the Ollama proxy):**
```bash
herd    # starts the Ollama proxy on port 11435
```

**On every machine running Ollama:**
```bash
herd-node    # discovers the Ollama proxy automatically on your network
```

Now point your apps at `http://ollama-proxy:11435` instead of `http://localhost:11434`. Same Ollama API, same model names, same streaming — the Ollama proxy handles smarter routing.

## Drop-in Ollama proxy replacement

Every Ollama API endpoint works through the Ollama proxy:

```bash
# Chat via Ollama proxy (same as direct Ollama)
curl http://ollama-proxy:11435/api/chat -d '{
  "model": "llama3.3:70b",
  "messages": [{"role": "user", "content": "Hello via Ollama proxy"}]
}'

# Generate via Ollama proxy (same as direct Ollama)
curl http://ollama-proxy:11435/api/generate -d '{
  "model": "qwen3:32b",
  "prompt": "Explain quantum computing via Ollama proxy"
}'

# List models via Ollama proxy (aggregated from all Ollama nodes)
curl http://ollama-proxy:11435/api/tags

# List loaded models via Ollama proxy (across all Ollama nodes)
curl http://ollama-proxy:11435/api/ps

# Pull a model via Ollama proxy (auto-selects best node)
curl -N http://ollama-proxy:11435/api/pull -d '{"name": "codestral"}'
```

### OpenAI-compatible Ollama proxy API

The Ollama proxy also exposes an OpenAI-compatible endpoint — same models, no code changes:

```python
from openai import OpenAI

# Point at the Ollama proxy instead of direct Ollama
ollama_proxy_client = OpenAI(base_url="http://ollama-proxy:11435/v1", api_key="not-needed")
ollama_proxy_response = ollama_proxy_client.chat.completions.create(
    model="llama3.3:70b",
    messages=[{"role": "user", "content": "Hello via Ollama proxy"}],
    stream=True,
)
```

## What the Ollama proxy does that direct Ollama doesn't

| Feature | Direct Ollama | Ollama Proxy (Herd) |
|---------|--------------|-------------------|
| Multiple machines | Manual IP switching | Ollama proxy routes automatically |
| Load balancing | None | Ollama proxy scores on 7 signals |
| Failover | None | Ollama proxy auto-retries on next node |
| Model discovery | Per-machine Ollama | Ollama proxy aggregates fleet-wide |
| Queue management | None | Ollama proxy manages per-node:model queues |
| Dashboard | None | Ollama proxy provides real-time web UI |
| Health checks | None | Ollama proxy runs 15 automated checks |
| Request tracing | None | Ollama proxy logs to SQLite trace store |
| Image generation | None | Ollama proxy routes mflux + DiffusionKit |
| Speech-to-text | None | Ollama proxy routes Qwen3-ASR |

## Ollama proxy works with your existing tools

Just change the Ollama URL to the Ollama proxy — no other configuration needed:

| Tool | Before (direct Ollama) | After (Ollama proxy) |
|------|--------|-------|
| **Open WebUI** | `http://localhost:11434` | `http://ollama-proxy:11435` |
| **Aider** | `--openai-api-base http://localhost:11434/v1` | `--openai-api-base http://ollama-proxy:11435/v1` |
| **Continue.dev** | Ollama at localhost | Ollama proxy at `ollama-proxy:11435` |
| **LangChain** | `Ollama(base_url="http://localhost:11434")` | `Ollama(base_url="http://ollama-proxy:11435")` |
| **LiteLLM** | `ollama/llama3.3:70b` | `ollama/llama3.3:70b` (point at Ollama proxy) |
| **CrewAI** | `OPENAI_API_BASE=http://localhost:11434/v1` | `OPENAI_API_BASE=http://ollama-proxy:11435/v1` |

## How the Ollama proxy routes requests

When a request arrives at the Ollama proxy, it scores all Ollama nodes that have the requested model:

1. **Thermal state** — is the model already loaded in the Ollama instance (hot)?
2. **Memory fit** — does the Ollama node have enough free RAM?
3. **Queue depth** — is the Ollama node busy with other requests?
4. **Latency history** — how fast has this Ollama node been recently?
5. **Role affinity** — the Ollama proxy sends big models to big machines
6. **Availability trend** — is this Ollama node reliably available?
7. **Context fit** — does the loaded context window match the request?

The highest-scoring Ollama node wins. If it fails, the Ollama proxy retries on the next best node automatically.

## Monitor your Ollama proxy fleet

Ollama proxy dashboard at `http://ollama-proxy:11435/dashboard` — see every Ollama node, every model, every queue in real time.

```bash
# Ollama proxy fleet overview
curl -s http://ollama-proxy:11435/fleet/status | python3 -m json.tool

# Ollama proxy health checks
curl -s http://ollama-proxy:11435/dashboard/api/health | python3 -m json.tool
```

## Full documentation

- [Agent Setup Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/agent-setup-guide.md) — setting up the Ollama proxy
- [API Reference](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/api-reference.md) — all Ollama proxy endpoints
- [Configuration](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/configuration-reference.md) — Ollama proxy settings

## Contribute

Ollama Herd (the Ollama proxy) is open source (MIT). We welcome contributions:
- [Star on GitHub](https://github.com/geeks-accelerator/ollama-herd) — help others find the Ollama proxy
- [Open an issue](https://github.com/geeks-accelerator/ollama-herd/issues) — bug reports, feature requests
- **PRs welcome** — `CLAUDE.md` gives AI agents full Ollama proxy context. 444 tests, async Python.

## Guardrails

- **No automatic model downloads** — the Ollama proxy requires explicit user confirmation for model pulls.
- **Model deletion requires explicit user confirmation via the Ollama proxy.**
- **All Ollama proxy requests stay local** — no data leaves your network.
- Never delete or modify files in `~/.fleet-manager/`.
