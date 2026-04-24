---
name: mistral-codestral
description: Mistral and Codestral — run Mistral Large, Mistral-Nemo, Codestral, and Mistral-Small locally. Mistral AI's open-source LLMs for code generation and reasoning. Codestral by Mistral trained on 80+ languages. Mistral routed across your fleet. Mistral本地推理。Mistral IA local. Codestral código local.
version: 1.0.2
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"sparkles","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","pip"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["darwin","linux","windows"]}}
---

# Mistral & Codestral — Mistral AI Models on Your Local Fleet

Mistral AI's open-source models run locally on your hardware. Mistral Large for frontier reasoning, Mistral-Nemo for efficiency, Codestral for code generation. The fleet router picks the best device for every Mistral request.

## Supported Mistral models

| Mistral Model | Parameters | Ollama name | Best for |
|---------------|-----------|-------------|----------|
| **Codestral** (by Mistral) | 22B | `codestral` | Mistral's code specialist — 80+ languages |
| **Mistral Large** | 123B | `mistral-large` | Mistral's frontier reasoning, multilingual |
| **Mistral-Nemo** | 12B | `mistral-nemo` | Mistral's efficient general-purpose model |
| **Mistral-Small** | 22B | `mistral-small` | Mistral's fast reasoning model |
| **Mistral 7B** | 7B | `mistral:7b` | Mistral's lightweight model |

## Setup Mistral locally

```bash
pip install ollama-herd    # install Mistral fleet router
herd                       # start the Mistral-compatible router
herd-node                  # run on each device — Mistral requests route automatically
```

No Mistral models downloaded during installation. All Mistral model pulls are user-initiated.

## Codestral code generation

Codestral is Mistral AI's dedicated coding model — trained on 80+ programming languages with fill-in-the-middle support.

```python
from openai import OpenAI

# Connect to local Mistral fleet
mistral_fleet = OpenAI(base_url="http://localhost:11435/v1", api_key="not-needed")

# Codestral by Mistral for code generation
codestral_response = mistral_fleet.chat.completions.create(
    model="codestral",  # Mistral's Codestral model
    messages=[{"role": "user", "content": "Write a Redis-backed rate limiter in Go"}],
)
print(codestral_response.choices[0].message.content)
```

### Codestral via curl

```bash
# Codestral code generation on local Mistral fleet
curl http://localhost:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "codestral", "messages": [{"role": "user", "content": "Implement a B-tree in Rust — Mistral Codestral excels at systems programming"}]}'
```

## Mistral Large reasoning

```bash
# Mistral Large for complex reasoning
curl http://localhost:11435/api/chat -d '{
  "model": "mistral-large",
  "messages": [{"role": "user", "content": "Compare Mistral vs GPT-4 for enterprise deployments"}],
  "stream": false
}'
```

### Mistral-Nemo for efficiency

```bash
# Mistral-Nemo — best quality/size ratio from Mistral AI
curl http://localhost:11435/api/chat -d '{
  "model": "mistral-nemo",
  "messages": [{"role": "user", "content": "Summarize this Mistral AI technical paper"}],
  "stream": false
}'
```

## Mistral hardware recommendations

> **Cross-platform:** These are example configurations. Any device (Mac, Linux, Windows) with equivalent RAM works. The fleet router runs on all platforms.

| Mistral Model | Min RAM | Example hardware |
|---------------|---------|------------------------|
| `mistral:7b` | 8GB | Any Mac — lightweight Mistral |
| `mistral-nemo` | 10GB | Mac Mini (16GB) — efficient Mistral |
| `codestral` | 16GB | Mac Mini (24GB) — Mistral's code model |
| `mistral-small` | 16GB | Mac Mini (24GB) — fast Mistral |
| `mistral-large` | 80GB | Mac Studio (128GB) — Mistral's best |

## Monitor Mistral fleet

```bash
# See which Mistral models are loaded
curl -s http://localhost:11435/api/ps | python3 -m json.tool

# Mistral fleet overview
curl -s http://localhost:11435/fleet/status | python3 -m json.tool

# Mistral model performance stats
curl -s http://localhost:11435/dashboard/api/models | python3 -m json.tool
```

Example Mistral fleet response:
```json
{
  "node_id": "Mistral-Server",
  "models_loaded": ["codestral:22b", "mistral-nemo:12b"],
  "mistral_inference": "active"
}
```

Mistral dashboard at `http://localhost:11435/dashboard`.

## Also available alongside Mistral

### Other LLMs (same Mistral-compatible endpoint)
Llama 3.3, Qwen 3.5, DeepSeek-V3, Phi 4, Gemma 3 — route alongside Mistral models.

### Image generation
```bash
curl http://localhost:11435/api/generate-image \
  -d '{"model": "z-image-turbo", "prompt": "Mistral AI logo reimagined as abstract art", "width": 512, "height": 512}'
```

### Speech-to-text
```bash
curl http://localhost:11435/api/transcribe -F "file=@mistral_meeting.wav" -F "model=qwen3-asr"
```

### Embeddings
```bash
curl http://localhost:11435/api/embed \
  -d '{"model": "nomic-embed-text", "input": "Mistral AI open source language models Codestral"}'
```

## Full documentation

- [Agent Setup Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/agent-setup-guide.md)
- [API Reference](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/api-reference.md)

## Contribute

Ollama Herd is open source (MIT). Run Mistral locally, contribute globally:
- [Star on GitHub](https://github.com/geeks-accelerator/ollama-herd) — help Mistral users find local inference
- [Open an issue](https://github.com/geeks-accelerator/ollama-herd/issues) — share your Mistral setup
- **PRs welcome** — `CLAUDE.md` gives AI agents full context. 929 tests.

## Guardrails

- **Mistral model downloads require explicit user confirmation** — Mistral models range from 4GB to 70GB+.
- **Mistral model deletion requires explicit user confirmation.**
- Never delete or modify files in `~/.fleet-manager/`.
- No Mistral models downloaded automatically — all pulls are user-initiated.
