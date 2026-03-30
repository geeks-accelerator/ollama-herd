# Image Generation Routing via `/api/generate-image`

**Status**: Planned
**Priority**: Medium — valuable when fleet has multiple devices with limited memory

## Problem

mflux (MLX-native Flux image generation) runs as a CLI subprocess on Apple Silicon. Currently it bypasses Herd entirely. In a fleet of Mac Minis with 24-64GB each, only one or two might have the image model loaded. Agents on other devices have no way to discover which node has the model and route to it.

## Solution

Add image generation routing to Herd using the same pattern as LLM routing: node agents report image model availability in heartbeats, router scores nodes and queues requests, a thin HTTP wrapper on each node invokes mflux as a subprocess.

### Endpoint

```
POST /api/generate-image
{
  "model": "z-image-turbo",
  "prompt": "a neon-lit Tokyo alley at midnight",
  "negative_prompt": "",
  "width": 1024,
  "height": 1024,
  "steps": 4,
  "guidance": 3.5,
  "seed": null,
  "quantize": 8
}

Response: 200 OK
Content-Type: image/png
X-Fleet-Node: mac-studio
X-Fleet-Generation-Time: 18500
Body: <raw PNG bytes>
```

## Architecture

```
Client                    Router (:11435)              Node Agent
  │                           │                            │
  │  POST /api/generate-image │                            │
  │──────────────────────────>│                            │
  │                           │  Score nodes with          │
  │                           │  image model loaded        │
  │                           │                            │
  │                           │  POST /api/generate-image  │
  │                           │───────────────────────────>│
  │                           │                            │  subprocess:
  │                           │                            │  mflux-generate-z-image-turbo
  │                           │                            │  --prompt "..." --width 1024
  │                           │                            │  --height 1024 --steps 4
  │                           │                            │  --output /tmp/img_abc.png
  │                           │        PNG bytes           │
  │                           │<───────────────────────────│
  │        PNG bytes          │                            │
  │<──────────────────────────│                            │
```

## Key differences from LLM routing

| Aspect | LLM (current) | Image gen |
|--------|---------------|-----------|
| Concurrency | 8-16 parallel per node | 1 at a time per node |
| Latency | 1-10s streaming | 15-30s batch |
| Response | Text stream (NDJSON/SSE) | Binary file (PNG) |
| Model loading | Stays in VRAM, hot/warm/cold | On-disk weights, invoked per-request |
| Retry | Before first chunk → re-route | Full request → re-route |
| Memory impact | Model stays loaded (keep_alive: -1) | ~3GB during generation, freed after |

## Implementation steps

### Step 1: Heartbeat — report image model availability

**`models/node.py`** — New models:
```python
class ImageModel(BaseModel):
    name: str               # "z-image-turbo", "flux-dev"
    binary: str             # "mflux-generate-z-image-turbo"
    quantize: int = 8       # default quantization level

class ImageMetrics(BaseModel):
    models_available: list[ImageModel] = []
    generating: bool = False  # currently running a generation
```

**`HeartbeatPayload`** — Add optional field:
```python
image: ImageMetrics | None = None
```

**`NodeState`** — Same field added.

**`node/collector.py`** — Add `_detect_image_models()`:
- `shutil.which("mflux-generate-z-image-turbo")` → reports z-image-turbo as available
- `shutil.which("mflux-generate")` → reports generic mflux as available
- Check for active mflux processes (`pgrep mflux`) → sets `generating: True`

### Step 2: Node agent — image generation HTTP endpoint

**`node/image_server.py`** — New file, thin FastAPI app:

```python
@app.post("/api/generate-image")
async def generate_image(request: Request):
    body = await request.json()
    prompt = body["prompt"]
    model = body.get("model", "z-image-turbo")
    width = body.get("width", 1024)
    height = body.get("height", 1024)
    steps = body.get("steps", 4)
    quantize = body.get("quantize", 8)
    seed = body.get("seed")

    # Build mflux CLI command
    cmd = ["mflux-generate-z-image-turbo",
           "--prompt", prompt,
           "--width", str(width), "--height", str(height),
           "--steps", str(steps), "--quantize", str(quantize),
           "--output", output_path]
    if seed is not None:
        cmd += ["--seed", str(seed)]

    # Run subprocess
    result = await asyncio.create_subprocess_exec(
        *cmd, stdout=PIPE, stderr=PIPE)
    await result.wait()

    # Return PNG
    return Response(
        content=open(output_path, "rb").read(),
        media_type="image/png",
        headers={"X-Generation-Time": str(elapsed_ms)}
    )
```

**Integration**: The node agent starts this alongside the Ollama proxy. Listens on a separate port (e.g., 11436). The port is reported in heartbeats so the router knows where to send image requests.

### Step 3: Router — scoring and routing

**`server/routes/image_compat.py`** — New route file:

```python
@router.post("/api/generate-image")
async def generate_image(request: Request):
    body = await request.json()
    model = body.get("model", "")

    # Find nodes with this image model available
    registry = request.app.state.registry
    candidates = [n for n in registry.get_online_nodes()
                  if n.image and any(m.name == model for m in n.image.models_available)]

    if not candidates:
        return JSONResponse(status_code=404,
            content={"error": f"Image model '{model}' not available on any node"})

    # Score: prefer nodes not currently generating, with most memory
    best = _score_image_nodes(candidates)

    # Proxy to node's image endpoint
    proxy = request.app.state.streaming_proxy
    png_bytes = await proxy.generate_image_on_node(best.node_id, body)

    return Response(content=png_bytes, media_type="image/png",
        headers={"X-Fleet-Node": best.node_id})
```

**Scoring for image gen** — simpler than LLM:
1. **Busy penalty** — node currently generating gets heavy penalty (only 1 at a time)
2. **Memory available** — more headroom = better
3. **Thermal** — cooler node preferred (image gen is compute-heavy)
4. **Queue depth** — if node has LLM requests queued, penalize (resource contention)

### Step 4: Settings and configuration

**`models/config.py`** — New settings:
```python
# Image generation
image_generation: bool = False           # Enable /api/generate-image routing
image_timeout: float = 120.0            # Max seconds to wait for image generation
image_default_model: str = "z-image-turbo"
```

**Toggle via settings API**: Add `image_generation` to the mutable toggles whitelist.

### Step 5: Dashboard visibility

- **Fleet Overview**: Show image model availability per node (similar to LLM models)
- **Settings page**: Toggle for image generation routing
- **Traces**: Log image generation requests with model, node, generation time, resolution

### Step 6: Health check

- **Image generation timeout**: Flag if generations consistently exceed the timeout
- **Node contention**: Flag if image gen and LLM requests are fighting on the same node

## What NOT to build (v1 scope)

- No image-to-image support (add later via `--image-path` flag)
- No LoRA routing (add later)
- No batch generation (one image per request)
- No image caching or deduplication
- No model auto-download (user must install mflux and download models manually)
- No streaming progress updates (wait for full PNG)

## Testing approach

1. **Unit tests**: Mock subprocess calls, test scoring logic, test heartbeat detection
2. **Integration test**: Start node with mflux available, send request through router, verify PNG returned
3. **Manual test**: Generate an image via `curl -o test.png http://localhost:11435/api/generate-image -d '{"model":"z-image-turbo","prompt":"a cat"}'`

## Files to create/modify

| File | Changes |
|------|---------|
| `models/node.py` | Add `ImageModel`, `ImageMetrics` |
| `models/config.py` | Add `image_generation`, `image_timeout`, `image_default_model` |
| `node/collector.py` | Add `_detect_image_models()` |
| `node/image_server.py` | **New** — thin HTTP wrapper for mflux subprocess |
| `node/agent.py` | Start image server alongside Ollama proxy |
| `server/routes/image_compat.py` | **New** — `/api/generate-image` route |
| `server/streaming.py` | Add `generate_image_on_node()` |
| `server/routes/dashboard.py` | Settings toggle, image model display |
| `server/health_engine.py` | Image generation health checks |
| `tests/test_server/test_image_routing.py` | **New** — image routing tests |

## Estimated effort

- Step 1 (heartbeat): ~30 min
- Step 2 (node endpoint): ~1 hour
- Step 3 (router): ~1 hour
- Step 4 (config): ~15 min
- Step 5 (dashboard): ~30 min
- Step 6 (health): ~30 min
- Tests: ~1 hour

Total: ~5 hours of implementation
