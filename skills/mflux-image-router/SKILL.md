---
name: mflux-image-router
description: Local mflux image generation on Apple Silicon — mflux routes Z-Image-Turbo, Flux Dev, Flux Schnell across your Mac fleet. mflux is MLX-native for Mac Studio, Mac Mini, MacBook Pro. mflux generates images in ~7s at 512px, ~18s at 1024px. Fleet-routed mflux with queue management. mflux图像生成 | generación de imágenes mflux
version: 1.0.1
homepage: https://github.com/geeks-accelerator/ollama-herd
metadata: {"openclaw":{"emoji":"art","requires":{"anyBins":["curl","wget"],"optionalBins":["python3","pip"]},"configPaths":["~/.fleet-manager/latency.db","~/.fleet-manager/logs/herd.jsonl"],"os":["darwin"]}}
---

# mflux Image Generation Router

You're helping someone generate images using mflux — an MLX-native image generation framework built for Apple Silicon. Instead of calling mflux as a subprocess on one machine, this routes mflux image generation requests across the fleet. The router picks the device with the mflux model loaded, the most free memory, and the lowest CPU load.

## Why route mflux image generation

One machine running mflux image generation blocks other workloads. An mflux 1024x1024 image takes ~18 seconds on an M3 Ultra. If an agent needs another mflux image during that time, it waits. With fleet routing, the second mflux request goes to a different device.

mflux image generation also competes with LLM inference for GPU memory. The router knows which nodes are busy with LLM requests and routes mflux image generation to the least-loaded device.

Zero cloud costs. A Mac Mini M4 running mflux generates images at $0/request after the hardware investment. DALL-E charges $0.04/image. At 80 mflux images per day, that's $96/month saved.

## Get started with mflux

```bash
pip install ollama-herd
herd                        # start the mflux image generation router (port 11435)
herd-node                   # start on each device running mflux
uv tool install mflux       # install mflux on devices for image generation
```

Enable mflux image generation:

```bash
curl -X POST http://localhost:11435/dashboard/api/settings \
  -H "Content-Type: application/json" \
  -d '{"image_generation": true}'
```

Package: [ollama-herd](https://pypi.org/project/ollama-herd/) | Repo: [github.com/geeks-accelerator/ollama-herd](https://github.com/geeks-accelerator/ollama-herd)

## Generate an image with mflux

### curl — mflux image generation

```bash
# mflux image generation via fleet router
curl -o mflux_output.png http://localhost:11435/api/generate-image \
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

### Python — mflux image generation

```python
import httpx

def mflux_generate_image(prompt, mflux_output_path="mflux_output.png", width=1024, height=1024):
    """Generate an image using mflux image generation via the fleet router."""
    mflux_resp = httpx.post(
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
    mflux_resp.raise_for_status()
    with open(mflux_output_path, "wb") as f:
        f.write(mflux_resp.content)

    mflux_node = mflux_resp.headers.get("X-Fleet-Node", "unknown")
    mflux_time_ms = mflux_resp.headers.get("X-Generation-Time", "?")
    print(f"mflux image generation completed on {mflux_node} in {mflux_time_ms}ms")
    return mflux_output_path
```

### JavaScript — mflux image generation

```javascript
async function mfluxGenerateImage(prompt, width = 1024, height = 1024) {
  // mflux image generation via fleet router
  const mflux_resp = await fetch("http://localhost:11435/api/generate-image", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      model: "z-image-turbo", prompt, width, height, steps: 4, quantize: 8,
    }),
  });
  if (!mflux_resp.ok) throw new Error((await mflux_resp.json()).error);
  return Buffer.from(await mflux_resp.arrayBuffer());
}
```

### mflux image generation parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `model` | (required) | `z-image-turbo`, `flux-dev`, or `flux-schnell` — mflux models |
| `prompt` | (required) | Text description for mflux image generation |
| `width` | `1024` | mflux image width in pixels |
| `height` | `1024` | mflux image height in pixels |
| `steps` | `4` | mflux inference steps (4 is optimal for z-image-turbo) |
| `quantize` | `8` | mflux quantization level (3-8 bit). 8 is the sweet spot |
| `seed` | random | Integer seed for reproducible mflux output |
| `negative_prompt` | `""` | What to avoid in the mflux image |

### mflux image generation response

- **200 OK**: Raw PNG bytes from mflux. `Content-Type: image/png`
- `X-Fleet-Node`: Which device ran mflux image generation
- `X-Fleet-Model`: mflux model used
- `X-Generation-Time`: mflux generation time in milliseconds

### Available mflux models

| mflux Model | Speed (M3 Ultra) | Quality | Use case |
|-------|-------------------|---------|----------|
| `z-image-turbo` | ~7s (512px), ~18s (1024px) | Good | Fast mflux iteration |
| `flux-dev` | ~30s (1024px) | Highest | Detailed mflux photorealistic |
| `flux-schnell` | ~10s (1024px) | Medium | Fastest mflux variant |

### mflux image generation with request tags

Track per-project mflux image generation in the dashboard:

```bash
curl -o mflux_output.png http://localhost:11435/api/generate-image \
  -H "Content-Type: application/json" \
  -d '{
    "model": "z-image-turbo",
    "prompt": "your prompt for mflux image generation",
    "metadata": {"tags": ["mflux-project", "mflux-content-gen"]}
  }'
```

## Also available on this fleet

### LLM inference

```bash
curl http://localhost:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-oss:120b","messages":[{"role":"user","content":"Hello"}]}'
```

### Speech-to-text

```bash
curl -s http://localhost:11435/api/transcribe \
  -F "audio=@recording.wav" | python3 -m json.tool
```

### Embeddings

```bash
curl http://localhost:11435/api/embeddings \
  -d '{"model":"nomic-embed-text","prompt":"search query"}'
```

## Monitoring mflux image generation

```bash
# mflux image generation stats (last 24h)
curl -s http://localhost:11435/dashboard/api/image-stats | python3 -m json.tool

# Fleet health (includes mflux image generation activity)
curl -s http://localhost:11435/dashboard/api/health | python3 -m json.tool
```

Dashboard at `http://localhost:11435/dashboard` — mflux image generation queues show with [IMAGE] badge.

## Full documentation

[Agent Setup Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/agent-setup-guide.md) — complete reference for all 4 model types including mflux.

[Image Generation Guide](https://github.com/geeks-accelerator/ollama-herd/blob/main/docs/guides/image-generation.md) — detailed mflux image generation API reference.

## Guardrails

- Never delete or modify mflux-generated images without explicit user confirmation.
- Never pull or delete mflux models without user confirmation — downloads can be 3+ GB.
- Never delete or modify files in `~/.fleet-manager/`.
- If no mflux image generation models available, suggest installing: `uv tool install mflux`.
