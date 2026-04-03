---
name: local-coding
description: Local coding assistant — run DeepSeek-Coder, Codestral, StarCoder, and Qwen-Coder across your device fleet. Code generation, review, refactoring, and debugging routed to the best available machine. Works with Aider, Continue.dev, Cline, and any OpenAI-compatible coding tool. No cloud API costs, all code stays local.
version: 1.0.1
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"keyboard","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","pip"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["darwin","linux","windows"]}}
---

# Local Coding Assistant — Code Models Across Your Fleet

Run the best open-source coding models on your own hardware. DeepSeek-Coder, Codestral, StarCoder, and Qwen-Coder routed across your devices — the fleet picks the best machine for every code generation request.

Your code never leaves your network. No GitHub Copilot subscription, no cloud API costs.

## Coding models available

| Model | Parameters | Ollama name | Strengths |
|-------|-----------|-------------|-----------|
| **Codestral** | 22B | `codestral` | 80+ languages, fill-in-the-middle, Mistral's code specialist |
| **DeepSeek-Coder-V2** | 236B MoE (21B active) | `deepseek-coder-v2` | Matches GPT-4 Turbo on code tasks |
| **DeepSeek-Coder** | 6.7B, 33B | `deepseek-coder:33b` | Purpose-built for code (87% code training data) |
| **Qwen2.5-Coder** | 7B, 32B | `qwen2.5-coder:32b` | Strong multi-language code generation |
| **StarCoder2** | 3B, 7B, 15B | `starcoder2:15b` | Trained on The Stack v2, 600+ languages |
| **CodeGemma** | 7B | `codegemma` | Google's code-focused Gemma variant |

## Quick start

```bash
pip install ollama-herd    # PyPI: https://pypi.org/project/ollama-herd/
herd                       # start the router (port 11435)
herd-node                  # run on each device — finds the router automatically
```

No models are downloaded during installation. All pulls require user confirmation.

## Code generation

### Write new code

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:11435/v1", api_key="not-needed")

response = client.chat.completions.create(
    model="codestral",
    messages=[{"role": "user", "content": "Write a thread-safe LRU cache in Python with TTL support"}],
)
print(response.choices[0].message.content)
```

### Code review

```bash
curl http://localhost:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-coder-v2:16b",
    "messages": [{"role": "user", "content": "Review this code for bugs and security issues:\n\n```python\ndef process_payment(amount, card_number):\n    ...\n```"}]
  }'
```

### Refactoring

```bash
curl http://localhost:11435/api/chat -d '{
  "model": "qwen2.5-coder:32b",
  "messages": [{"role": "user", "content": "Refactor this to use async/await: ..."}],
  "stream": false
}'
```

## Works with your IDE tools

The fleet exposes an OpenAI-compatible API at `http://localhost:11435/v1`. Point any coding tool at it:

| Tool | Config |
|------|--------|
| **Aider** | `aider --openai-api-base http://localhost:11435/v1 --model codestral` |
| **Continue.dev** | Set API base to `http://localhost:11435/v1` in VS Code settings |
| **Cline** | Set provider to OpenAI-compatible, base URL `http://localhost:11435/v1` |
| **Open WebUI** | Set Ollama URL to `http://localhost:11435` |
| **LangChain** | `ChatOpenAI(base_url="http://localhost:11435/v1", model="codestral")` |

## Pick the right model for your RAM

> **Cross-platform:** These are example configurations. Any device (Mac, Linux, Windows) with equivalent RAM works.

| Device | RAM | Best coding model |
|--------|-----|------------------|
| MacBook Air (8GB) | 8GB | `starcoder2:3b` or `deepseek-coder:6.7b` |
| Mac Mini (16GB) | 16GB | `codestral` or `starcoder2:15b` |
| Mac Mini (32GB) | 32GB | `qwen2.5-coder:32b` or `deepseek-coder:33b` |
| Mac Studio (128GB) | 128GB | `deepseek-coder-v2` — frontier code quality |

## Check what's running

```bash
# Models loaded in memory
curl -s http://localhost:11435/api/ps | python3 -m json.tool

# All available models
curl -s http://localhost:11435/api/tags | python3 -m json.tool

# Recent coding request traces
curl -s "http://localhost:11435/dashboard/api/traces?limit=5" | python3 -m json.tool
```

## Also available on this fleet

### General-purpose LLMs
Llama 3.3, Qwen 3.5, DeepSeek-R1, Mistral Large — for non-code tasks through the same endpoint.

### Image generation
```bash
curl http://localhost:11435/api/generate-image \
  -d '{"model": "z-image-turbo", "prompt": "developer workspace illustration", "width": 512, "height": 512}'
```

### Speech-to-text
```bash
curl http://localhost:11435/api/transcribe -F "file=@standup.wav" -F "model=qwen3-asr"
```

## Full documentation

- [Agent Setup Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/agent-setup-guide.md) — all 4 model types
- [API Reference](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/api-reference.md) — complete endpoint docs

## Guardrails

- **Model downloads require explicit user confirmation** — coding models range from 2GB to 130GB+. Always confirm before pulling.
- **Model deletion requires explicit user confirmation.**
- Never delete or modify files in `~/.fleet-manager/`.
- No models are downloaded automatically — all pulls are user-initiated or require opt-in.
- **Your code stays local** — no prompts or generated code leave your network.
