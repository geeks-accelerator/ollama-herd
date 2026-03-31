# Image Generation via Ollama Herd

Route image generation requests across your fleet — three backends, one endpoint, zero configuration.

## Backends

Ollama Herd supports three image generation backends:

| Backend | Models | Install | How it works |
|---------|--------|---------|-------------|
| **mflux** | z-image-turbo, flux-dev, flux-schnell | `uv tool install mflux` | CLI subprocess on port 11436 |
| **DiffusionKit** | sd3-medium, sd3.5-large | `uv tool install diffusionkit` | CLI subprocess on port 11436 |
| **Ollama native** | x/z-image-turbo, x/flux2-klein | `ollama pull x/z-image-turbo` | Standard Ollama API proxy |

All backends route through the same `/api/generate-image` endpoint. The router detects which backend handles each model and routes accordingly.

## Why route images through Herd?

If you run AI agents that generate images (social media bots, content pipelines, creative tools), you have the same problem with image generation that you had with LLM inference: one machine isn't enough, and managing which machine has which model is painful.

**The problem:**
- Image models run as CLI subprocesses on Apple Silicon — there's no HTTP API, no service discovery
- In a fleet of Mac Minis with 24-64GB each, not every device has the image model installed
- Agents on other devices can't discover which node has image capabilities and route to it
- No visibility into which node generated which image, or how long it took

**The solution:**
- Herd's node agents detect mflux and DiffusionKit binaries and report them in heartbeats
- Ollama native image models are detected through the standard Ollama model list
- A lightweight image server on each node wraps CLI tools as an HTTP endpoint
- The router scores candidates and proxies requests to the best available node
- One endpoint (`/api/generate-image`) replaces direct subprocess calls

## Install image backends

### mflux (Flux models — fastest)

```bash
uv tool install mflux
```

The first image generation downloads model weights (~3GB from Hugging Face). Subsequent runs use the local cache.

Verify: `mflux-generate-z-image-turbo --prompt "test" --width 512 --height 512 --steps 4 --output /tmp/test.png`

### DiffusionKit (Stable Diffusion 3 / 3.5)

```bash
uv tool install diffusionkit
```

The first run downloads model weights from HuggingFace (~2-8GB depending on model).

Verify: `diffusionkit-cli --prompt "test" --model-version argmaxinc/mlx-stable-diffusion-3-medium --width 512 --height 512 --steps 10 --output-path /tmp/test.png`

**macOS 26 patch required:** DiffusionKit has a bug on macOS 26+ where `sw_vers` output parsing fails. Apply this one-time fix after installation:

```bash
# Find the installed file
ARGMAX_FILE=$(python3 -c "import argmaxtools.test_utils; print(argmaxtools.test_utils.__file__)" 2>/dev/null || find ~/.local -name test_utils.py -path "*/argmaxtools/*" 2>/dev/null | head -1)

# If installed via uv tool, it's typically at:
# ~/.local/share/uv/tools/diffusionkit/lib/python*/site-packages/argmaxtools/test_utils.py

# Apply the patch: replace the os_spec parsing (lines ~595-598)
# Change this:
#   os_type, os_version, os_build_number = [
#       line.rsplit("\t\t")[1]
#       for line in sw_vers.rsplit("\n")
#   ]
# To this:
#   parsed = {
#       line.rsplit("\t\t")[0].rstrip(":"): line.rsplit("\t\t")[1]
#       for line in sw_vers.rsplit("\n")
#       if "\t\t" in line
#   }
#   os_type = parsed.get("ProductName", "macOS")
#   os_version = parsed.get("ProductVersion", "0.0")
#   os_build_number = parsed.get("BuildVersion", "unknown")
```

This patch handles the `ProductVersionExtra` field that macOS 26 added to `sw_vers` output. Without it, DiffusionKit crashes with `IndexError: list index out of range`.

### Ollama native (experimental, Ollama v0.14.3+)

```bash
ollama pull x/z-image-turbo      # 12GB
ollama pull x/flux2-klein         # ~6GB
```

Ollama native image models work through the standard `/api/generate` endpoint. No separate image server needed — requests flow through the existing Ollama proxy pipeline.

Verify it works:

```bash
mflux-generate-z-image-turbo --prompt "a test image" --width 512 --height 512 --steps 4 --output /tmp/test.png
```

## Enable image generation

Image generation routing is disabled by default. Enable it:

### Via settings API

```bash
curl -X POST http://localhost:11435/dashboard/api/settings \
  -H "Content-Type: application/json" \
  -d '{"image_generation": true}'
```

### Via environment variable

```bash
FLEET_IMAGE_GENERATION=true uv run herd
```

### Via dashboard

Open `http://localhost:11435/dashboard/settings` and toggle "Image Generation" on.

## Generate an image

```bash
curl -o image.png http://localhost:11435/api/generate-image \
  -H "Content-Type: application/json" \
  -d '{
    "model": "z-image-turbo",
    "prompt": "a neon-lit Tokyo alley at midnight, cyberpunk aesthetic",
    "width": 1024,
    "height": 1024,
    "steps": 4
  }'
```

**Response:** Raw PNG bytes with headers:
- `X-Fleet-Node` — which node generated the image
- `X-Fleet-Model` — which model was used
- `X-Generation-Time` — generation time in milliseconds
- `Content-Type: image/png`

### Full request parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `model` | (required) | Image model name: `z-image-turbo`, `flux-dev`, `flux-schnell`, `sd3-medium`, `sd3.5-large`, `x/z-image-turbo`, `x/flux2-klein` |
| `prompt` | (required) | Text description of the image to generate |
| `negative_prompt` | `""` | What to avoid in the image |
| `width` | `1024` | Image width in pixels |
| `height` | `1024` | Image height in pixels |
| `steps` | `4` | Inference steps (more = higher quality, slower) |
| `guidance` | (model default) | Guidance scale — how strongly to follow the prompt |
| `seed` | (random) | Entropy seed for reproducible generation |
| `quantize` | `8` | Quantization level (3, 4, 5, 6, or 8 bit) |

### Example: Python integration

```python
import httpx

HERD_URL = "http://localhost:11435"

def generate_image(prompt: str, width=1024, height=1024) -> bytes:
    resp = httpx.post(
        f"{HERD_URL}/api/generate-image",
        json={
            "model": "z-image-turbo",
            "prompt": prompt,
            "width": width,
            "height": height,
            "steps": 4,
        },
        timeout=120.0,
    )
    resp.raise_for_status()
    return resp.content

# Generate and save
png_bytes = generate_image("a cat coding on a laptop")
with open("cat.png", "wb") as f:
    f.write(png_bytes)
```

### Example: JavaScript/Node.js integration

```javascript
async function generateImage(prompt, width = 1024, height = 1024) {
  const resp = await fetch("http://localhost:11435/api/generate-image", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      model: "z-image-turbo",
      prompt,
      width,
      height,
      steps: 4,
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
```

## How routing works

### Node detection

Every 5 seconds, each node agent checks for image generation binaries via `shutil.which()`:

**mflux:**
- `mflux-generate-z-image-turbo` found → reports `z-image-turbo` model
- `mflux-generate` found → reports `flux-dev` model

**DiffusionKit:**
- `diffusionkit-cli` found → reports `sd3-medium` and `sd3.5-large` models

**Ollama native:**
- Image models (e.g., `x/z-image-turbo`) appear in the standard Ollama model list and are detected via the `x/` prefix

Active mflux or DiffusionKit processes are detected via `psutil` — if a generation is in progress, the node reports `generating: true`.

The node agent starts a lightweight FastAPI server on port 11436 that wraps CLI tools (mflux and DiffusionKit) as HTTP endpoints. Ollama native image models route through the standard Ollama proxy (port 11434) instead.

### Scoring

When an image generation request arrives, the router scores all online nodes that have the requested model:

1. **Busy penalty** (-50) — node currently generating another image
2. **Memory headroom** (+0.5 per GB available) — more free memory = better
3. **CPU utilization** (-0.2 per %) — cooler/less busy node preferred

The highest-scoring node wins. This is intentionally simpler than the 7-signal LLM scoring — image generation is one-at-a-time per node, so the main decision is "who's idle?"

### Request flow

**mflux / DiffusionKit models:**
```
Client → POST /api/generate-image → Router (:11435)
  → Score candidates (which nodes have the model + are idle?)
  → Proxy to best node's image server (:11436)
  → Node runs mflux or diffusionkit-cli subprocess → writes PNG to temp file
  → Returns PNG bytes → Router forwards to client
```

**Ollama native models (x/ prefix):**
```
Client → POST /api/generate or /api/generate-image → Router (:11435)
  → Detect image model (x/ prefix) → force non-streaming
  → Score with standard 7-signal engine → enqueue
  → Proxy to Ollama (:11434) via standard streaming proxy
  → Ollama returns JSON with base64 "image" field
  → Router decodes base64 → returns PNG bytes to client
```

## Monitoring

### Dashboard visibility

Image requests flow through the same queue manager as LLM requests. Open `http://localhost:11435/dashboard` and you'll see image queues in the **Request Queues** section alongside LLM queues — with pending, in-flight, done, and failed counters.

### Image generation stats

```bash
curl -s http://localhost:11435/dashboard/api/image-stats | python3 -m json.tool
```

Returns:
- Total/completed/failed counts (last 24h)
- Average generation time
- Breakdown by node and model
- 10 most recent events with timestamps and dimensions

### Health checks

The health page (`http://localhost:11435/dashboard/health`) automatically surfaces:

- **Image Generation Activity** — summary of images generated, avg time, nodes used
- **Expand Image Generation** — when image gen is used 3+ times in 24h, recommends installing mflux on nodes that don't have it but have sufficient memory

### Check which nodes have image models

```bash
curl -s http://localhost:11435/dashboard/api/settings | python3 -c "
import json, sys
d = json.load(sys.stdin)
for n in d['nodes']:
    models = n.get('image_models', [])
    port = n.get('image_port', 0)
    if models:
        print(f'{n[\"node_id\"]}: {models} (port {port})')
    else:
        print(f'{n[\"node_id\"]}: no image models')
"
```

## Available models

### mflux models (Flux family)

| Model | Speed (M3 Ultra) | Quality | Notes |
|-------|-------------------|---------|-------|
| `z-image-turbo` | ~7s (512px), ~18s (1024px) | Good | Optimized for 4-step generation |
| `flux-dev` | ~30s (1024px) | High | More detailed, slower |
| `flux-schnell` | ~10s (1024px) | Medium | Fastest Flux variant |

### DiffusionKit models (Stable Diffusion 3 family)

| Model | Speed (M3 Ultra) | Quality | Peak RAM | Notes |
|-------|-------------------|---------|----------|-------|
| `sd3-medium` | ~9s (512px) | Good | 3.5GB | Stability AI's SD3 architecture |
| `sd3.5-large` | ~67s (512px) | Highest | 11.6GB | Best quality, uses T5 encoder |

### Ollama native models (experimental)

| Model | Speed (M3 Ultra) | Quality | Notes |
|-------|-------------------|---------|-------|
| `x/z-image-turbo` | ~19s (1024px) | Good | Same model as mflux, runs via Ollama |
| `x/flux2-klein` | ~20s (1024px) | Good | Good text rendering |
| `x/flux2-klein:9b` | ~30s (1024px) | Higher | 9B variant |

## Configuration

| Setting | Default | Env Var | Description |
|---------|---------|---------|-------------|
| `image_generation` | `false` | `FLEET_IMAGE_GENERATION` | Enable `/api/generate-image` routing |
| `image_timeout` | `120.0` | `FLEET_IMAGE_TIMEOUT` | Max seconds to wait for image generation |

## Error handling

| Status | Meaning |
|--------|---------|
| `200` | Success — body is PNG bytes |
| `400` | Missing `model` or `prompt` |
| `404` | Model not available on any node — response lists available models |
| `502` | Node failed to generate — check node logs |
| `503` | Image generation disabled — enable via settings |
| `504` | Generation timed out (exceeded `image_timeout`) |

## Differences from LLM routing

| Aspect | LLM (`/api/chat`) | Image (`/api/generate-image`) |
|--------|-------------------|-------------------------------|
| Concurrency | 8-16 parallel per node | 1 at a time per node |
| Response | Streaming text (NDJSON/SSE) | Single binary (PNG) |
| Latency | 1-10s (tokens stream as generated) | 7-30s (entire image at once) |
| Model loading | Stays in VRAM (`keep_alive: -1`) | Loaded from disk per request |
| Scoring | 7 signals (thermal, memory, queue, latency...) | 3 signals (busy, memory, CPU) |
| Queue | Per-node:model queue with workers | Direct proxy (no queue) |
| Retry | Auto-retry before first chunk | No retry (full request fails) |
