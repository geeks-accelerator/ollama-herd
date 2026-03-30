---
name: mflux-image-router
description: Generate images with mflux (MLX-native Flux) across your Apple Silicon fleet. Fleet-routed image generation with queue management, dashboard visibility, and automatic node selection. Models include Z-Image-Turbo (~7s at 512px), Flux Dev, Flux Schnell. Use when the user wants to generate images locally without cloud APIs.
version: 1.0.0
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"art","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","pip"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["darwin","linux"]}}
---

# mflux Image Router

You're helping someone generate images using mflux — an MLX-native implementation of Flux image models built for Apple Silicon. Instead of calling mflux as a subprocess on one machine, this routes requests across the fleet. The router picks the device with the model loaded, the most free memory, and the lowest CPU load.

## Why route image generation

One machine generating images blocks other workloads. A 1024x1024 image takes ~18 seconds on an M3 Ultra. If an agent needs another image during that time, it waits. With fleet routing, the second request goes to a different device.

Image generation also competes with LLM inference for GPU memory. The router knows which nodes are busy with LLM requests and routes image generation to the least-loaded device.

Zero cloud costs. A Mac Mini M4 generates images at $0/request after the hardware investment. ElevenLabs charges $0.04/image. At 80 images per day, that's $96/month saved.

## Get started

```bash
pip install ollama-herd
herd                        # start the router (port 11435)
herd-node                   # start on each device
uv tool install mflux       # install mflux on devices that should generate images
```

Enable image generation:

```bash
curl -X POST http://localhost:11435/dashboard/api/settings \
  -H "Content-Type: application/json" \
  -d '{"image_generation": true}'
```

Package: [ollama-herd](https://pypi.org/project/ollama-herd/) | Repo: [github.com/geeks-accelerator/ollama-herd](https://github.com/geeks-accelerator/ollama-herd)

## Generate an image

### curl

```bash
curl -o output.png http://localhost:11435/api/generate-image \
  -H "Content-Type: application/json" \
  -d '{
    "model": "z-image-turbo",
    "prompt": "a neon-lit Tokyo alley at midnight, cyberpunk aesthetic",
    "width": 1024,
    "height": 1024,
    "steps": 4,
    "quantize": 8
  }'
```

### Python

```python
import httpx

def generate_image(prompt, output_path="output.png", width=1024, height=1024):
    resp = httpx.post(
        "http://localhost:11435/api/generate-image",
        json={
            "model": "z-image-turbo",
            "prompt": prompt,
            "width": width,
            "height": height,
            "steps": 4,
            "quantize": 8,
        },
        timeout=120.0,
    )
    resp.raise_for_status()
    with open(output_path, "wb") as f:
        f.write(resp.content)

    node = resp.headers.get("X-Fleet-Node", "unknown")
    time_ms = resp.headers.get("X-Generation-Time", "?")
    print(f"Generated on {node} in {time_ms}ms")
    return output_path
```

### JavaScript

```javascript
async function generateImage(prompt, width = 1024, height = 1024) {
  const resp = await fetch("http://localhost:11435/api/generate-image", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      model: "z-image-turbo", prompt, width, height, steps: 4, quantize: 8,
    }),
  });
  if (!resp.ok) throw new Error((await resp.json()).error);
  return Buffer.from(await resp.arrayBuffer());
}
```

### Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `model` | (required) | `z-image-turbo`, `flux-dev`, or `flux-schnell` |
| `prompt` | (required) | Text description of the image |
| `width` | `1024` | Image width in pixels |
| `height` | `1024` | Image height in pixels |
| `steps` | `4` | Inference steps (4 is optimal for z-image-turbo) |
| `quantize` | `8` | Quantization level (3-8 bit). 8 is the sweet spot |
| `seed` | random | Integer seed for reproducible output |
| `negative_prompt` | `""` | What to avoid in the image |

### Response

- **200 OK**: Raw PNG bytes. `Content-Type: image/png`
- `X-Fleet-Node`: Which device generated the image
- `X-Fleet-Model`: Image model used
- `X-Generation-Time`: Generation time in milliseconds

### Available models

| Model | Speed (M3 Ultra) | Quality | Use case |
|-------|-------------------|---------|----------|
| `z-image-turbo` | ~7s (512px), ~18s (1024px) | Good | Fast iteration, social media |
| `flux-dev` | ~30s (1024px) | Highest | Detailed, photorealistic |
| `flux-schnell` | ~10s (1024px) | Medium | Fastest Flux variant |

### With request tags

Track per-project image generation in the dashboard:

```bash
curl -o output.png http://localhost:11435/api/generate-image \
  -H "Content-Type: application/json" \
  -d '{
    "model": "z-image-turbo",
    "prompt": "your prompt",
    "metadata": {"tags": ["my-project", "content-gen"]}
  }'
```

## Also available on this fleet

### LLM inference

```bash
curl http://localhost:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-oss:120b","messages":[{"role":"user","content":"Hello"}]}'
```

Drop-in OpenAI SDK compatible.

### Speech-to-text

```bash
curl -s http://localhost:11435/api/transcribe \
  -F "audio=@recording.wav" | python3 -m json.tool
```

Requires `FLEET_TRANSCRIPTION=true`. Uses Qwen3-ASR.

### Embeddings

```bash
curl http://localhost:11435/api/embeddings \
  -d '{"model":"nomic-embed-text","prompt":"search query"}'
```

## Monitoring

```bash
# Image generation stats (last 24h)
curl -s http://localhost:11435/dashboard/api/image-stats | python3 -m json.tool

# Fleet health
curl -s http://localhost:11435/dashboard/api/health | python3 -m json.tool
```

Dashboard at `http://localhost:11435/dashboard` — image queues show with [IMAGE] badge.

## Full documentation

[Agent Setup Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/agent-setup-guide.md) — complete reference for all 4 model types.

[Image Generation Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/image-generation.md) — detailed image generation API reference.

## Guardrails

- Never delete or modify generated images without explicit user confirmation.
- Never pull or delete models without user confirmation — downloads can be 3+ GB.
- Never delete or modify files in `~/.fleet-manager/`.
- If no image models available, suggest installing: `uv tool install mflux`.
