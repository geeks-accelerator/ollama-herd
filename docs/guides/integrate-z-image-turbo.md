# Integrating Z-Image-Turbo via Ollama Herd

A guide for AI agents and scripts that need to generate images using Z-Image-Turbo (Tongyi-MAI/Z-Image-Turbo) through the Ollama Herd fleet router.

## What this gives you

Instead of calling `mflux-generate-z-image-turbo` as a subprocess on a specific machine, you hit a single HTTP endpoint. The router handles:

- **Node selection** — picks the device with mflux installed, the most free memory, and the lowest CPU load
- **Queue management** — serializes image requests (one at a time per node), tracks pending/in-flight/done/failed
- **Monitoring** — every generation appears in the fleet dashboard with timing data
- **Failover** — if the selected node is busy or down, routes to the next best option

## Prerequisites

The fleet must have Ollama Herd running with image generation enabled.

### On the router machine

```bash
pip install ollama-herd
herd  # starts the router on port 11435
```

### On each machine that should generate images

```bash
pip install ollama-herd
uv tool install mflux              # install the mflux CLI
herd-node                          # starts the node agent (auto-discovers router)
# OR
herd-node --router-url http://<router-ip>:11435
```

The first image generation on a new node downloads model weights (~3GB from Hugging Face). Subsequent runs use the local cache.

### Enable image generation (one time)

```bash
curl -X POST http://localhost:11435/dashboard/api/settings \
  -H "Content-Type: application/json" \
  -d '{"image_generation": true}'
```

Or set the environment variable before starting the router:

```bash
FLEET_IMAGE_GENERATION=true herd
```

### Verify

```bash
# Check that image models are detected
curl -s http://localhost:11435/dashboard/api/settings | python3 -c "
import json, sys
d = json.load(sys.stdin)
for n in d['nodes']:
    models = n.get('image_models', [])
    if models:
        print(f'{n[\"node_id\"]}: {models} (port {n[\"image_port\"]})')
"
```

Expected output:

```
Neons-Mac-Studio: ['z-image-turbo', 'flux-dev'] (port 11436)
```

## API endpoint

```
POST http://localhost:11435/api/generate-image
Content-Type: application/json
```

### Request body

```json
{
  "model": "z-image-turbo",
  "prompt": "a neon-lit Tokyo alley at midnight, cyberpunk aesthetic, rain reflections",
  "width": 1024,
  "height": 1024,
  "steps": 4,
  "quantize": 8
}
```

### Parameters

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `model` | Yes | — | Must be `z-image-turbo` |
| `prompt` | Yes | — | Text description of the image |
| `width` | No | `1024` | Image width in pixels |
| `height` | No | `1024` | Image height in pixels |
| `steps` | No | `4` | Inference steps. Z-Image-Turbo is optimized for 4 steps |
| `quantize` | No | `8` | Quantization level (3, 4, 5, 6, or 8 bit). 8 is the sweet spot |
| `seed` | No | random | Integer seed for reproducible output |
| `negative_prompt` | No | `""` | What to avoid in the image |
| `guidance` | No | model default | How strongly to follow the prompt |

### Response

**Success (200):**

```
Content-Type: image/png
X-Fleet-Node: Neons-Mac-Studio
X-Fleet-Model: z-image-turbo
X-Generation-Time: 18315
```

Body: raw PNG bytes. Write directly to file.

**Errors:**

| Status | Body | Meaning |
|--------|------|---------|
| `400` | `{"error": "prompt is required"}` | Missing required field |
| `404` | `{"error": "Image model 'z-image-turbo' not available..."}` | No node has mflux installed |
| `502` | `{"error": "Image generation failed on ..."}` | mflux subprocess crashed |
| `503` | `{"error": "Image generation is disabled..."}` | Need to enable via settings |

## Integration examples

### Python (httpx)

```python
import httpx

HERD_URL = "http://localhost:11435"

def generate_image(
    prompt: str,
    width: int = 1024,
    height: int = 1024,
    steps: int = 4,
    seed: int | None = None,
) -> bytes:
    """Generate an image via Ollama Herd and return PNG bytes."""
    body = {
        "model": "z-image-turbo",
        "prompt": prompt,
        "width": width,
        "height": height,
        "steps": steps,
        "quantize": 8,
    }
    if seed is not None:
        body["seed"] = seed

    resp = httpx.post(
        f"{HERD_URL}/api/generate-image",
        json=body,
        timeout=120.0,  # images take 7-20s depending on resolution
    )
    resp.raise_for_status()

    node = resp.headers.get("X-Fleet-Node", "unknown")
    time_ms = resp.headers.get("X-Generation-Time", "?")
    print(f"Generated on {node} in {time_ms}ms")

    return resp.content


# Usage
png_bytes = generate_image("a cat wearing a space helmet, digital art")
with open("output.png", "wb") as f:
    f.write(png_bytes)
```

### Python (aiohttp, async)

```python
import aiohttp

HERD_URL = "http://localhost:11435"

async def generate_image(prompt: str, width=1024, height=1024) -> bytes:
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{HERD_URL}/api/generate-image",
            json={
                "model": "z-image-turbo",
                "prompt": prompt,
                "width": width,
                "height": height,
                "steps": 4,
                "quantize": 8,
            },
            timeout=aiohttp.ClientTimeout(total=120),
        ) as resp:
            resp.raise_for_status()
            return await resp.read()
```

### JavaScript / Node.js

```javascript
const fs = require("fs");

const HERD_URL = "http://localhost:11435";

async function generateImage(prompt, width = 1024, height = 1024) {
  const resp = await fetch(`${HERD_URL}/api/generate-image`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      model: "z-image-turbo",
      prompt,
      width,
      height,
      steps: 4,
      quantize: 8,
    }),
  });

  if (!resp.ok) {
    const err = await resp.json();
    throw new Error(err.error);
  }

  const node = resp.headers.get("X-Fleet-Node");
  const timeMs = resp.headers.get("X-Generation-Time");
  console.log(`Generated on ${node} in ${timeMs}ms`);

  return Buffer.from(await resp.arrayBuffer());
}

// Usage
const png = await generateImage("a robot painting a landscape");
fs.writeFileSync("output.png", png);
```

### curl

```bash
# Basic generation
curl -o image.png http://localhost:11435/api/generate-image \
  -H "Content-Type: application/json" \
  -d '{"model":"z-image-turbo","prompt":"your prompt here","width":1024,"height":1024,"steps":4}'

# With seed for reproducibility
curl -o image.png http://localhost:11435/api/generate-image \
  -H "Content-Type: application/json" \
  -d '{"model":"z-image-turbo","prompt":"your prompt","width":1024,"height":1024,"steps":4,"seed":42}'

# Smaller/faster (512x512)
curl -o image.png http://localhost:11435/api/generate-image \
  -H "Content-Type: application/json" \
  -d '{"model":"z-image-turbo","prompt":"your prompt","width":512,"height":512,"steps":4}'
```

## Migrating from direct mflux calls

If your project currently calls mflux as a subprocess:

### Before (subprocess)

```python
import subprocess
import os

def generate_image(prompt, output_path, width=1024, height=1024):
    cmd = [
        "mflux-generate-z-image-turbo",
        "--prompt", prompt,
        "--width", str(width),
        "--height", str(height),
        "--steps", "4",
        "--quantize", "8",
        "--output", output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=180)
    if result.returncode != 0:
        raise RuntimeError(f"mflux failed: {result.stderr.decode()}")
    return output_path
```

### After (Herd API)

```python
import httpx

HERD_URL = "http://localhost:11435"

def generate_image(prompt, output_path, width=1024, height=1024):
    resp = httpx.post(
        f"{HERD_URL}/api/generate-image",
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
    return output_path
```

### What changes

| Aspect | Before (subprocess) | After (Herd API) |
|--------|-------------------|-----------------|
| Where it runs | Only the local machine | Any machine in the fleet with mflux |
| Failure mode | Silent crash, check returncode | HTTP error codes with messages |
| Monitoring | None | Dashboard queues, health checks, stats API |
| Concurrency | Must manage yourself | Queue handles serialization |
| Discovery | Hardcoded machine | Auto-routed to best available |
| Output | Writes file to local disk | Returns PNG bytes over HTTP |

### What stays the same

- Same model (Z-Image-Turbo)
- Same parameters (prompt, width, height, steps, seed, quantize)
- Same output quality (identical mflux binary underneath)
- Same generation speed (~7s for 512px, ~18s for 1024px on M3 Ultra)

## Performance expectations

Tested on Mac Studio M3 Ultra (512GB):

| Resolution | Steps | Time | File Size |
|-----------|-------|------|-----------|
| 512x512 | 4 | ~7s | 100-400 KB |
| 1024x1024 | 4 | ~18s | 1-2 MB |

Add ~50ms for HTTP overhead (router → node → mflux → response). Negligible compared to generation time.

## Monitoring your integration

### Check recent generations

```bash
curl -s http://localhost:11435/dashboard/api/image-stats | python3 -m json.tool
```

Returns completed/failed counts, average generation time, breakdown by node and model.

### Check queue status

Image requests show up alongside LLM queues in the dashboard at `http://localhost:11435/dashboard`. Look for `Neons-Mac-Studio:z-image-turbo:latest` in the Request Queues section.

### Health checks

The health page at `http://localhost:11435/dashboard/health` shows:
- **Image Generation Activity** — summary of recent generations
- **Expand Image Generation** — suggests installing mflux on more nodes if image gen is popular

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| 503 "disabled" | Image gen not enabled | `curl -X POST .../dashboard/api/settings -d '{"image_generation":true}'` |
| 404 "not available" | No node has mflux | Install mflux: `uv tool install mflux` on at least one node |
| 502 "failed" | mflux subprocess crashed | Check node logs: `tail ~/.fleet-manager/logs/herd.jsonl` |
| Slow first request | Model weights downloading | First run downloads ~3GB from Hugging Face. Subsequent runs use cache |
| Connection refused | Router not running | Start with `herd` or `uv run herd` |
| Image blank/corrupt | Bad prompt or parameters | Try simpler prompt, default parameters |

## Full API reference

See [Image Generation Guide](./image-generation.md) for the complete API reference including all parameters, error codes, scoring details, and comparison with LLM routing.
