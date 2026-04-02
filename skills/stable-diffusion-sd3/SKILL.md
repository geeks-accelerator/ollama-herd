---
name: stable-diffusion-sd3
description: Stable Diffusion 3 and SD3.5 Large on Apple Silicon — generate Stable Diffusion images locally with DiffusionKit's MLX-native backend. SD3 Medium for fast Stable Diffusion generation, SD3.5 Large for highest quality. Plus Flux models via mflux and Ollama native image gen. All routed across your device fleet. No cloud APIs, no DALL-E costs. 稳定扩散SD3本地图像生成。Difusion estable SD3 para generacion de imagenes local.
version: 1.0.1
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"art","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","pip"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["darwin","linux"]}}
---

# Stable Diffusion 3 — Local Image Generation on Your Fleet

Run Stable Diffusion 3 Medium and Stable Diffusion 3.5 Large (SD3.5) on your own Apple Silicon hardware. DiffusionKit provides MLX-native Stable Diffusion inference — no CUDA, no cloud, no per-image costs. The fleet router picks the best device for every Stable Diffusion generation request.

## Stable Diffusion Supported Models

| Stable Diffusion Model | Backend | Speed (M3 Ultra) | Peak RAM | Quality |
|-------|---------|-------------------|----------|---------|
| **SD3 Medium** | DiffusionKit | ~9s (512px) | 3.5GB | Good — fast Stable Diffusion iterations |
| **SD3.5 Large** | DiffusionKit | ~67s (512px) | 11.6GB | Highest — Stable Diffusion with T5 encoder |
| **z-image-turbo** | mflux | ~7s (512px) | 4GB | Good — fastest option |
| **flux-dev** | mflux | ~30s (1024px) | 6GB | High — detailed output |
| **x/z-image-turbo** | Ollama native | ~19s (1024px) | 12GB | Good — experimental |

## Stable Diffusion Setup

```bash
pip install ollama-herd    # Stable Diffusion fleet router from PyPI
herd                       # start the Stable Diffusion router (port 11435)
herd-node                  # run on each device — finds the router for Stable Diffusion routing
```

### Install DiffusionKit for Stable Diffusion models

```bash
uv tool install diffusionkit    # Stable Diffusion 3 and SD3.5 backend
```

**macOS 26 users:** Apply a one-time patch for Stable Diffusion compatibility:
```bash
./scripts/patch-diffusionkit-macos26.sh
```

First Stable Diffusion run downloads model weights from HuggingFace (~2-8GB depending on SD3 model). No models are downloaded during installation — all Stable Diffusion pulls are user-initiated.

### Install mflux for Flux models (optional, recommended alongside Stable Diffusion)

```bash
uv tool install mflux
```

The router prefers mflux over Ollama native for shared models to avoid evicting LLMs from memory during Stable Diffusion workloads.

## Generate Stable Diffusion Images

### Stable Diffusion 3 Medium (fast SD3 generation)

```bash
curl -o sd3_cityscape.png http://localhost:11435/api/generate-image \
  -H "Content-Type: application/json" \
  -d '{"model": "sd3-medium", "prompt": "Stable Diffusion rendering a futuristic cityscape at dusk", "width": 1024, "height": 1024, "steps": 20}'
```

### Stable Diffusion 3.5 Large (highest quality SD3)

```bash
curl -o sd3_portrait.png http://localhost:11435/api/generate-image \
  -H "Content-Type: application/json" \
  -d '{"model": "sd3.5-large", "prompt": "Stable Diffusion oil painting portrait, dramatic lighting", "width": 1024, "height": 1024, "steps": 30}'
```

### Stable Diffusion Python Integration

```python
import httpx

def generate_stable_diffusion(prompt, model="sd3-medium", width=1024, height=1024):
    """Generate an image using Stable Diffusion SD3 via the fleet router."""
    sd3_response = httpx.post(
        "http://localhost:11435/api/generate-image",
        json={"model": model, "prompt": prompt, "width": width, "height": height, "steps": 20},
        timeout=180.0,
    )
    sd3_response.raise_for_status()
    return sd3_response.content  # Stable Diffusion PNG bytes

# Quick Stable Diffusion iteration with SD3 Medium
sd3_png = generate_stable_diffusion("a robot painting a sunset in Stable Diffusion style")
with open("stable_diffusion_output.png", "wb") as f:
    f.write(sd3_png)
```

### Stable Diffusion Parameters

| SD3 Parameter | Default | Description |
|-----------|---------|-------------|
| `model` | (required) | `sd3-medium`, `sd3.5-large`, `z-image-turbo`, `flux-dev`, `flux-schnell` |
| `prompt` | (required) | Stable Diffusion text description of the image |
| `width` | `1024` | Stable Diffusion image width in pixels |
| `height` | `1024` | Stable Diffusion image height in pixels |
| `steps` | `4` | Stable Diffusion inference steps (20-30 recommended for SD3) |
| `guidance` | (model default) | Stable Diffusion guidance scale |
| `seed` | (random) | Seed for reproducible Stable Diffusion output |
| `negative_prompt` | `""` | What to avoid in Stable Diffusion generation |

## Monitor Stable Diffusion Generation

```bash
# Stable Diffusion generation stats (last 24h)
curl -s http://localhost:11435/dashboard/api/image-stats | python3 -m json.tool

# Which nodes have Stable Diffusion models
curl -s http://localhost:11435/fleet/status | python3 -c "
import sys, json
# Stable Diffusion node inspection
for n in json.load(sys.stdin).get('nodes', []):
    img = n.get('image', {})
    if img:
        sd3_models = [m['name'] for m in img.get('models_available', [])]
        print(f'{n[\"node_id\"]}: {sd3_models}')
"
```

Web dashboard at `http://localhost:11435/dashboard` — Stable Diffusion queues show with `[IMAGE]` badge alongside LLM queues.

## Also Available on This Fleet

### LLM inference alongside Stable Diffusion
Llama 3.3, Qwen 3.5, DeepSeek-V3, DeepSeek-R1 — any Ollama model through the same router that handles Stable Diffusion.

### Speech-to-text
```bash
curl http://localhost:11435/api/transcribe -F "file=@recording.wav" -F "model=qwen3-asr"
```

### Embeddings
```bash
curl http://localhost:11435/api/embed \
  -d '{"model": "nomic-embed-text", "input": "Stable Diffusion 3 image generation on Apple Silicon"}'
```

## Full Stable Diffusion Documentation

- [Image Generation Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/image-generation.md) — all 3 Stable Diffusion and Flux backends
- [Agent Setup Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/agent-setup-guide.md) — all 4 model types including Stable Diffusion
- [API Reference](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/api-reference.md) — complete Stable Diffusion endpoint docs

## Contribute

Ollama Herd is open source (MIT). We welcome contributions from both humans and AI agents:
- [GitHub](https://github.com/geeks-accelerator/ollama-herd) — star the repo, open issues, submit PRs
- 444 tests, fully async Python, Pydantic v2 models
- `CLAUDE.md` provides full context for AI agents

## Stable Diffusion Guardrails

- **No automatic downloads** — Stable Diffusion model weights are downloaded on first use, not during installation. All SD3 pulls require user confirmation.
- **Stable Diffusion model deletion requires explicit user confirmation.**
- Never delete or modify files in `~/.fleet-manager/` (contains Stable Diffusion routing data).
- All Stable Diffusion requests stay local — no data leaves your network.
