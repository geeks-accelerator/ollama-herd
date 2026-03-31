---
name: stable-diffusion-sd3
description: Stable Diffusion 3 and SD 3.5 Large on Apple Silicon — generate images locally with DiffusionKit's MLX-native backend. SD3 Medium for fast generation, SD3.5 Large for highest quality. Plus Flux models via mflux and Ollama native image gen. All routed across your device fleet with queue management and dashboard visibility. No cloud APIs, no DALL-E costs.
version: 1.0.0
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"art","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","pip"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["darwin","linux"]}}
---

# Stable Diffusion 3 — Local Image Generation on Your Fleet

Run Stable Diffusion 3 Medium and SD 3.5 Large on your own Apple Silicon hardware. DiffusionKit provides MLX-native inference — no CUDA, no cloud, no per-image costs. The fleet router picks the best device for every generation request.

## Supported models

| Model | Backend | Speed (M3 Ultra) | Peak RAM | Quality |
|-------|---------|-------------------|----------|---------|
| **SD3 Medium** | DiffusionKit | ~9s (512px) | 3.5GB | Good — fast iterations |
| **SD3.5 Large** | DiffusionKit | ~67s (512px) | 11.6GB | Highest — uses T5 encoder |
| **z-image-turbo** | mflux | ~7s (512px) | 4GB | Good — fastest option |
| **flux-dev** | mflux | ~30s (1024px) | 6GB | High — detailed output |
| **x/z-image-turbo** | Ollama native | ~19s (1024px) | 12GB | Good — experimental |

## Setup

```bash
pip install ollama-herd    # PyPI: https://pypi.org/project/ollama-herd/
herd                       # start the router (port 11435)
herd-node                  # run on each device — finds the router automatically
```

### Install DiffusionKit for Stable Diffusion models

```bash
uv tool install diffusionkit
```

**macOS 26 users:** Apply a one-time patch for compatibility:
```bash
./scripts/patch-diffusionkit-macos26.sh
```

First run downloads model weights from HuggingFace (~2-8GB depending on model). No models are downloaded during installation — all pulls are user-initiated.

### Install mflux for Flux models (optional, recommended)

```bash
uv tool install mflux
```

The router prefers mflux over Ollama native for shared models to avoid evicting LLMs from memory.

## Generate images

### Stable Diffusion 3 Medium (fast)

```bash
curl -o cityscape.png http://localhost:11435/api/generate-image \
  -H "Content-Type: application/json" \
  -d '{"model": "sd3-medium", "prompt": "a futuristic cityscape at dusk, cyberpunk", "width": 1024, "height": 1024, "steps": 20}'
```

### Stable Diffusion 3.5 Large (highest quality)

```bash
curl -o portrait.png http://localhost:11435/api/generate-image \
  -H "Content-Type: application/json" \
  -d '{"model": "sd3.5-large", "prompt": "oil painting portrait, dramatic lighting", "width": 1024, "height": 1024, "steps": 30}'
```

### Python integration

```python
import httpx

def generate_sd3(prompt, model="sd3-medium", width=1024, height=1024):
    resp = httpx.post(
        "http://localhost:11435/api/generate-image",
        json={"model": model, "prompt": prompt, "width": width, "height": height, "steps": 20},
        timeout=180.0,
    )
    resp.raise_for_status()
    return resp.content  # PNG bytes

# Quick iteration with SD3 Medium
png = generate_sd3("a robot painting a sunset")
with open("robot.png", "wb") as f:
    f.write(png)
```

### Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `model` | (required) | `sd3-medium`, `sd3.5-large`, `z-image-turbo`, `flux-dev`, `flux-schnell` |
| `prompt` | (required) | Text description of the image |
| `width` | `1024` | Image width in pixels |
| `height` | `1024` | Image height in pixels |
| `steps` | `4` | Inference steps (20-30 recommended for SD3) |
| `guidance` | (model default) | Guidance scale |
| `seed` | (random) | Seed for reproducible output |
| `negative_prompt` | `""` | What to avoid |

## Monitor image generation

```bash
# Image generation stats (last 24h)
curl -s http://localhost:11435/dashboard/api/image-stats | python3 -m json.tool

# Which nodes have image models
curl -s http://localhost:11435/fleet/status | python3 -c "
import sys, json
for n in json.load(sys.stdin).get('nodes', []):
    img = n.get('image', {})
    if img:
        models = [m['name'] for m in img.get('models_available', [])]
        print(f'{n[\"node_id\"]}: {models}')
"
```

Web dashboard at `http://localhost:11435/dashboard` — image queues show with `[IMAGE]` badge alongside LLM queues.

## Also available on this fleet

### LLM inference
Llama 3.3, Qwen 3.5, DeepSeek-V3, DeepSeek-R1, Phi 4, Mistral, Codestral — any Ollama model through the same router.

### Speech-to-text
```bash
curl http://localhost:11435/api/transcribe -F "file=@recording.wav" -F "model=qwen3-asr"
```

### Embeddings
```bash
curl http://localhost:11435/api/embed \
  -d '{"model": "nomic-embed-text", "input": "Stable Diffusion 3 image generation"}'
```

## Full documentation

- [Image Generation Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/image-generation.md) — all 3 backends, performance benchmarks
- [Agent Setup Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/agent-setup-guide.md) — all 4 model types
- [API Reference](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/api-reference.md) — complete endpoint docs

## Contribute

Ollama Herd is open source (MIT). We welcome contributions from both humans and AI agents:
- [GitHub](https://github.com/geeks-accelerator/ollama-herd) — star the repo, open issues, submit PRs
- 412 tests, fully async Python, Pydantic v2 models
- `CLAUDE.md` provides full context for AI agents to be productive from message one

## Guardrails

- **No automatic downloads** — model weights are downloaded on first use, not during installation. All pulls require user confirmation.
- **Model deletion requires explicit user confirmation.**
- Never delete or modify files in `~/.fleet-manager/`.
- All requests stay local — no data leaves your network.
