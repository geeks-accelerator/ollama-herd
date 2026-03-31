# Expanded Image Generation: Ollama Native + DiffusionKit

## Problem

The fleet router currently supports only mflux for image generation — three Flux-based models (`z-image-turbo`, `flux-dev`, `flux-schnell`) wrapped as CLI subprocesses. This misses:

- **Ollama native image gen** (v0.14.3+) — `x/z-image-turbo` and `x/flux2-klein` work through the standard `/api/generate` endpoint, which our router already proxies
- **Stable Diffusion 3 / 3.5** — the most-requested open diffusion models, available via DiffusionKit's MLX-native CLI
- **SDXL** — still the most widely adopted community model (LoRAs, ControlNet, etc.), available via Apple's mlx-examples

Users searching for "stable diffusion", "SDXL", "SD3" find our skills but can't actually use those models today.

## Proposal

Two phases, ordered by effort:

1. **Phase 1: Ollama native image gen** — near-zero code changes, uses existing proxy infrastructure
2. **Phase 2: DiffusionKit backend** — adds SD3/SD3.5 via the same subprocess pattern as mflux

## Phase 1: Ollama Native Image Generation

### Why this is nearly free

Ollama v0.14.3+ supports image generation through the standard `/api/generate` endpoint. Our router already proxies `/api/generate` requests through the full scoring → queuing → streaming pipeline. The response includes an `"image"` field with base64 PNG data when `done: true`.

This means image gen requests can flow through the **existing LLM routing path** — no separate image server, no port 11436, no custom scoring function. The node just needs the model pulled via `ollama pull x/z-image-turbo`.

### Models available

| Model | Ollama name | Size | Notes |
|-------|------------|------|-------|
| Z-Image-Turbo | `x/z-image-turbo` | ~6B | Same model as mflux z-image-turbo, now native in Ollama |
| FLUX.2 Klein 4B | `x/flux2-klein` | ~4B | Good text rendering, smaller footprint |
| FLUX.2 Klein 9B | `x/flux2-klein:9b` | ~9B | Higher quality variant |

### What needs to change

#### 1. Model detection in collector.py

The heartbeat already reports `ollama.models_available`. No change needed — Ollama native image models show up in the model list like any other model. But the router needs to know which models are image models vs. LLM models.

**Option A — Prefix convention:** Ollama image models use the `x/` prefix (`x/z-image-turbo`, `x/flux2-klein`). We can detect image models by prefix.

**Option B — Model knowledge catalog:** Add these models to `model_knowledge.py` with a new `category: "image"` field. The scoring engine already uses model_knowledge for routing decisions.

**Recommendation:** Option B — extend model_knowledge.py. It's the pattern we already use, and it keeps the detection logic centralized.

```python
# In model_knowledge.py, add to catalog:
"x/z-image-turbo": ModelInfo(
    category="image",
    parameters_b=6,
    min_ram_gb=8,
    ...
),
"x/flux2-klein": ModelInfo(
    category="image",
    parameters_b=4,
    min_ram_gb=6,
    ...
),
```

#### 2. Response handling in streaming.py

The current streaming proxy expects text/NDJSON/SSE responses. Ollama native image gen returns JSON with a base64 `image` field:

```json
{"model": "x/z-image-turbo", "response": "", "done": false}
{"model": "x/z-image-turbo", "response": "", "done": true, "image": "iVBORw0KGgo..."}
```

The streaming proxy needs to:
1. Detect when a response contains an `image` field
2. Decode the base64 PNG
3. Return it as `image/png` with appropriate headers

**Implementation:** Add an `is_image_model()` check in the route handler. If the model is an image model, use a non-streaming request and extract the `image` field from the final JSON response.

#### 3. Route entry point

Two options for how clients request Ollama native image gen:

**Option A — Through existing `/api/generate`:** Clients send a normal generate request with an image model name. The router detects it's an image model, routes accordingly, and returns the base64 image in the standard Ollama response format.

**Option B — Through `/api/generate-image`:** The existing image endpoint detects the model is an Ollama native model (not mflux) and routes through the Ollama proxy instead of the image server.

**Recommendation:** Support both. `/api/generate` works automatically (zero client changes). `/api/generate-image` also accepts Ollama native models and converts them internally. This way existing mflux clients and new Ollama clients both work.

#### 4. Scoring adjustments

Ollama native image models compete for the same VRAM as LLM models. The scoring engine needs to account for:
- Image models are large (4-9B) and may evict smaller LLM models from memory
- Image generation is slow (~10-30s) — nodes actively generating should be deprioritized
- Image models benefit from being pre-loaded (warm) just like LLM models

The existing 7-signal scoring already handles all of this — memory fit, queue depth, and model-loaded checks all apply. No special image scoring needed.

#### 5. Dashboard updates

- Image models should show with the `[IMAGE]` badge (already implemented for mflux)
- Queue entries for Ollama native image models should show `request_type: "image"` (needs the model detection from step 1)

### Files to modify

| File | Change |
|------|--------|
| `server/model_knowledge.py` | Add `x/z-image-turbo`, `x/flux2-klein` to catalog with `category: "image"` |
| `server/routes/ollama_compat.py` | Detect image model in `/api/generate`, set `request_type="image"`, handle base64 `image` response |
| `server/routes/image_compat.py` | Accept Ollama native models alongside mflux models, route through Ollama proxy |
| `server/streaming.py` | Handle `image` field in Ollama response (decode base64, return PNG) |
| `node/collector.py` | No change — Ollama models already reported in heartbeat |

### Estimated effort: Small (1-2 hours)

The infrastructure is already there. The main work is response format handling and model detection.

---

## Phase 2: DiffusionKit Backend

### Why DiffusionKit

[DiffusionKit](https://github.com/argmaxinc/DiffusionKit) by Argmax (makers of WhisperKit) is an MLX-native diffusion library that:
- Installs via pip: `pip install diffusionkit`
- Provides a CLI binary: `diffusionkit-cli`
- Supports SD3, SD3.5 Large, FLUX.1-schnell, FLUX.1-dev
- Runs headless on Apple Silicon (no GUI needed)
- Supports quantization for memory-constrained devices

This follows the **exact same pattern** as our mflux integration — subprocess wrapping a CLI binary.

### Models added

| Model | CLI command | Size | Notes |
|-------|-----------|------|-------|
| SD 3 Medium | `diffusionkit-cli --model sd3-medium` | ~2B | Stability AI's latest architecture |
| SD 3.5 Large | `diffusionkit-cli --model sd3.5-large` | ~8B | Highest quality SD model |
| FLUX.1 Schnell | `diffusionkit-cli --model flux-schnell` | ~12B | Already available via mflux, but DiffusionKit variant |
| FLUX.1 Dev | `diffusionkit-cli --model flux-dev` | ~12B | Already available via mflux |

The key additions are **SD3 Medium and SD3.5 Large** — these are new model architectures we can't run today.

### What needs to change

#### 1. Extend `_MODEL_BINARIES` in image_server.py

```python
_MODEL_BINARIES: dict[str, list[str]] = {
    # Existing mflux models
    "z-image-turbo": ["mflux-generate-z-image-turbo"],
    "flux-dev": ["mflux-generate", "--model", "dev"],
    "flux-schnell": ["mflux-generate", "--model", "schnell"],
    # New DiffusionKit models
    "sd3-medium": ["diffusionkit-cli", "--model", "sd3-medium"],
    "sd3.5-large": ["diffusionkit-cli", "--model", "sd3.5-large"],
}
```

#### 2. CLI argument mapping

DiffusionKit CLI uses different argument names than mflux:

| Parameter | mflux flag | DiffusionKit flag |
|-----------|-----------|------------------|
| Prompt | `--prompt` | `--prompt` (same) |
| Width | `--width` | `--w` |
| Height | `--height` | `--h` |
| Steps | `--steps` | `--num-steps` |
| Guidance | `--guidance` | `--cfg` |
| Seed | `--seed` | `--seed` (same) |
| Output | `--output` | `--output-path` |
| Quantize | `--quantize` | `--a16` / `--w16` (different system) |

The `generate_image` endpoint in `image_server.py` currently builds mflux-specific CLI args. This needs to be generalized:

**Option A — Backend-aware arg builder:** Detect the backend from the binary name and build args accordingly.

**Option B — Per-model arg templates:** Each entry in `_MODEL_BINARIES` includes an arg mapping.

**Recommendation:** Option A — a simple if/else on the binary name. There are only two backends (mflux and diffusionkit), and the arg differences are minor.

#### 3. Extend model detection in collector.py

```python
_DIFFUSIONKIT_BINARY = "diffusionkit-cli"

def _detect_image_models():
    models = []
    # Existing mflux detection
    for binary, model_name in _MFLUX_BINARIES:
        if shutil.which(binary):
            models.append(model_name)
    # New DiffusionKit detection
    if shutil.which(_DIFFUSIONKIT_BINARY):
        models.extend(["sd3-medium", "sd3.5-large"])
    return models
```

#### 4. Update image_compat.py model matching

The router's candidate selection filters nodes by `image.models_available`. Adding DiffusionKit models to the collector means they appear in the heartbeat, so the router picks them up automatically.

### Files to modify

| File | Change |
|------|--------|
| `node/image_server.py` | Add DiffusionKit entries to `_MODEL_BINARIES`, generalize CLI arg builder |
| `node/collector.py` | Add DiffusionKit binary detection |
| `server/model_knowledge.py` | Add SD3, SD3.5 model info |
| `server/routes/image_compat.py` | No change — model matching already generic |
| `docs/guides/image-generation.md` | Document new models and installation |

### Installation for users

```bash
# Install DiffusionKit (adds diffusionkit-cli to PATH)
pip install diffusionkit

# Or via uv
uv tool install diffusionkit

# Verify
diffusionkit-cli --help
```

First run downloads model weights from HuggingFace (~2-8GB depending on model). Subsequent runs use cached weights.

### Estimated effort: Small-Medium (2-3 hours)

Mostly mechanical — extending the existing pattern. The CLI arg mapping is the only non-trivial part.

---

## Combined result

After both phases, the fleet supports:

| Model | Backend | Source | New? |
|-------|---------|--------|------|
| z-image-turbo | mflux CLI | Existing | No |
| flux-dev | mflux CLI | Existing | No |
| flux-schnell | mflux CLI | Existing | No |
| x/z-image-turbo | Ollama native | Phase 1 | Yes |
| x/flux2-klein | Ollama native | Phase 1 | Yes |
| x/flux2-klein:9b | Ollama native | Phase 1 | Yes |
| sd3-medium | DiffusionKit CLI | Phase 2 | Yes |
| sd3.5-large | DiffusionKit CLI | Phase 2 | Yes |

**8 image models across 3 backends**, all routed through the same fleet infrastructure.

### Client experience

```bash
# Existing mflux (unchanged)
curl http://localhost:11435/api/generate-image \
  -d '{"model": "z-image-turbo", "prompt": "a sunset", "width": 512, "height": 512}'

# New: Ollama native (through standard Ollama API)
curl http://localhost:11435/api/generate \
  -d '{"model": "x/z-image-turbo", "prompt": "a sunset"}'

# New: Ollama native (through image endpoint)
curl http://localhost:11435/api/generate-image \
  -d '{"model": "x/flux2-klein", "prompt": "a sunset", "width": 1024, "height": 1024}'

# New: Stable Diffusion 3.5 (through image endpoint)
curl http://localhost:11435/api/generate-image \
  -d '{"model": "sd3.5-large", "prompt": "a sunset", "width": 1024, "height": 1024}'
```

All models use the same `/api/generate-image` endpoint. The router detects the backend and routes accordingly.

## Future considerations

### Phase 3 (not in scope)

- **SDXL via Apple's mlx-examples** — requires cloning a repo and managing Python script paths, messier than pip-installed CLIs. Consider if there's demand for SDXL-specific LoRAs/ControlNet.
- **ComfyUI integration** — workflow-based API is complex but unlocks the entire SD ecosystem. Better as an optional power-user integration than a core backend.
- **Draw Things HTTP API** — requires GUI app running, not suitable for headless fleet deployment. Skip.
- **LoRA/ControlNet support** — DiffusionKit and ComfyUI both support LoRAs. Could add `--lora` flag passthrough in Phase 2 if there's demand.
- **img2img** — both mflux and DiffusionKit support image-to-image. Would need a multipart upload endpoint.

## Implementation order

1. Phase 1 first — near-zero effort, unlocks Ollama native image gen immediately
2. Phase 2 second — extends the proven mflux pattern to DiffusionKit
3. Update skills/docs after each phase
4. Run full test suite after each phase (378+ tests should still pass, plus new image tests)

## Risk assessment

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Ollama image gen API changes (experimental) | Medium | Pin to v0.14.3+ behavior, add version check |
| DiffusionKit CLI flag changes | Low | Pin version in docs, test on update |
| Memory contention (image models evicting LLM models) | **Confirmed** | **Fixed:** Router prefers mflux over Ollama native to avoid VRAM eviction. See `docs/issues.md` for details. |
| Slow image gen blocking queues | Low | Image requests already use dedicated queue entries with custom timeouts |
