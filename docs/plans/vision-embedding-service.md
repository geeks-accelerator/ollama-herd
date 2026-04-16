# Vision Embedding Service Plan

## Context

Community News needs CLIP-style image embeddings for video frame deduplication — comparing two frames to determine if they show the same scene. They tried pulling `clip` via Ollama Herd, but `clip` doesn't exist in the Ollama registry. Ollama's `/api/embed` endpoint only supports text, not images.

Rather than bundling a 352MB ONNX model in the Community News Electron app, the right architecture is for Herd to serve image embeddings — same pattern as image generation (mflux) and speech-to-text (qwen3-asr). The node downloads the model, runs a lightweight embedding service, and the router proxies requests to it.

### Model Selection (2026 Research)

CLIP ViT-B/32 is legacy. Three models, two runtimes, one API:

| Model | Runtime | Platforms | Size | Dims | Use Case |
|-------|---------|-----------|------|------|----------|
| **DINOv2 ViT-S/14** | MLX | macOS (Apple Silicon) | 85MB | 384 | Primary — best visual similarity, smallest, fastest |
| **SigLIP2-base-patch16-224** | MLX | macOS (Apple Silicon) | 350MB | 768 | General-purpose — text+image if needed later |
| **CLIP ViT-B/32 int8** | ONNX | Linux, Windows, macOS (Intel) | 90MB | 512 | Cross-platform fallback |

DINOv2 scores 64% on image similarity benchmarks vs CLIP's 28% — more than 2x better at the actual task, in a 4x smaller model.

Node auto-selects: MLX available → DINOv2. No MLX → CLIP via ONNX. Same endpoint, same response format.

### Research sources

- `docs/research/compute-sharing-credit-economy.md` (private repo) — broader context
- [DINOv2 vs CLIP comparison](https://medium.com/aimonks/clip-vs-dinov2-in-image-similarity-6fa5aa7ed8c6)
- [mlx-image (DINOv2 on MLX)](https://github.com/riccardomusmeci/mlx-image)
- [Qdrant/clip-ViT-B-32-vision](https://huggingface.co/Qdrant/clip-ViT-B-32-vision) — ONNX model
- [SigLIP 2 blog](https://huggingface.co/blog/siglip2)

---

## Architecture

Follows the existing image gen / STT pattern exactly:

```
Client → POST /api/embed-image → Router → score nodes → proxy to node's embedding port
                                                              ↓
                                            Node embedding service (:11438)
                                            DINOv2 (MLX) or CLIP (ONNX)
                                                              ↓
                                            512/384/768-dim float32 vector
```

### Port Assignment

Following the existing convention:
- Ollama: `11434`
- Router: `11435`
- Image gen: `ollama_port + 2` → `11436`
- STT: `ollama_port + 3` → `11437`
- **Embedding: `ollama_port + 4` → `11438`**

---

## Implementation

### Phase 1: Models & Node Service

#### 1.1 Embedding model definitions

**File:** `src/fleet_manager/models/node.py`

Add alongside `ImageModel` and `TranscriptionModel`:

```python
class VisionEmbeddingModel(BaseModel):
    name: str       # "dinov2-vit-s14", "siglip2-base", "clip-vit-b32"
    runtime: str    # "mlx" or "onnx"
    dimensions: int # 384, 768, or 512

class VisionEmbeddingMetrics(BaseModel):
    models_available: list[VisionEmbeddingModel] = Field(default_factory=list)
    processing: bool = False
```

Add to `HeartbeatPayload`:

```python
vision_embedding: VisionEmbeddingMetrics | None = None
vision_embedding_port: int = 0
```

Add to `NodeState`:

```python
vision_embedding: VisionEmbeddingMetrics | None = None
vision_embedding_port: int = 0
```

#### 1.2 Model detection

**File:** `src/fleet_manager/node/collector.py`

New function `_detect_vision_embedding_models()`:

- Check if MLX is available (`import mlx` succeeds on Apple Silicon)
  - If yes: check for DINOv2 weights in cache dir (`~/.fleet-manager/models/dinov2-vit-s14/`)
  - Register as `VisionEmbeddingModel(name="dinov2-vit-s14", runtime="mlx", dimensions=384)`
  - Also check for SigLIP2 weights in cache
- If no MLX: check for CLIP ONNX model in cache dir (`~/.fleet-manager/models/clip-vit-b32/`)
  - Register as `VisionEmbeddingModel(name="clip-vit-b32", runtime="onnx", dimensions=512)`
- Return `VisionEmbeddingMetrics` or `None`

Call from `collect_heartbeat()` alongside image/STT detection.

#### 1.3 Embedding service

**New file:** `src/fleet_manager/node/embedding_server.py`

A lightweight FastAPI app with one endpoint:

```
POST /embed
{
  "model": "dinov2-vit-s14",     # optional — uses default if omitted
  "images": ["base64data", ...],  # one or more base64-encoded images
}

Response:
{
  "model": "dinov2-vit-s14",
  "embeddings": [[0.123, -0.456, ...], ...],  # list of float32 vectors
  "dimensions": 384
}
```

**MLX backend (Apple Silicon):**

```python
# Uses mlx-image for DINOv2 or mlx-embeddings for SigLIP
# Preprocessing: resize to 224x224 (DINOv2: 518x518), normalize, float16
# Inference: ~5-15ms per image on M-series
```

**ONNX backend (cross-platform):**

```python
# Uses onnxruntime + Pillow + numpy
# Preprocessing: resize 224x224, center crop, normalize with CLIP mean/std
# Inference: ~10-30ms per image on CPU
```

The server auto-selects the best available backend at startup.

#### 1.4 Model auto-download

**New file:** `src/fleet_manager/node/embedding_models.py`

Downloads models on first use to `~/.fleet-manager/models/`:

| Model | Source | Size | Cached Path |
|-------|--------|------|-------------|
| DINOv2 ViT-S/14 | HuggingFace `facebook/dinov2-small` | ~85MB | `~/.fleet-manager/models/dinov2-vit-s14/` |
| SigLIP2-base | HuggingFace `google/siglip2-base-patch16-224` | ~350MB | `~/.fleet-manager/models/siglip2-base/` |
| CLIP ViT-B/32 int8 | HuggingFace `Qdrant/clip-ViT-B-32-vision` | ~90MB | `~/.fleet-manager/models/clip-vit-b32/` |

Download triggered on first request or via `POST /api/pull` with model name.

#### 1.5 Node agent integration

**File:** `src/fleet_manager/node/agent.py`

New method `_ensure_embedding_server()` following `_ensure_image_server()` pattern:

- Call `_detect_vision_embedding_models()` from collector
- If models found, start `embedding_server` FastAPI app on port `ollama_port + 4`
- Store port in `self._embedding_port`
- Add to heartbeat: `payload.vision_embedding_port = self._embedding_port`

### Phase 2: Router Integration

#### 2.1 Registry storage

**File:** `src/fleet_manager/server/registry.py`

In `update_from_heartbeat()`, store vision embedding metrics:

```python
node.vision_embedding = payload.vision_embedding
node.vision_embedding_port = payload.vision_embedding_port
```

#### 2.2 Configuration

**File:** `src/fleet_manager/models/config.py`

```python
# Vision embedding routing
vision_embedding: bool = True
vision_embedding_timeout: float = 30.0   # Much shorter than image gen — embeddings are fast
```

#### 2.3 Router endpoint

**New file:** `src/fleet_manager/server/routes/embedding_compat.py`

Two endpoints:

**`POST /api/embed-image`** — dedicated image embedding endpoint:

```python
@router.post("/api/embed-image")
async def embed_image(request: Request):
    body = await request.json()
    model = body.get("model", "")  # optional — auto-select if empty
    images = body.get("images", [])  # list of base64 strings
    
    # Find node with embedding capability
    # Score: prefer nodes with model loaded, lowest queue, most memory
    # Proxy to node's embedding_port
```

**Extend `POST /api/embed`** — when model name matches a vision embedding model (e.g., `clip`, `dinov2`, `siglip`), route to the embedding service instead of Ollama:

```python
# In ollama_embed() handler:
if model in VISION_EMBEDDING_MODELS:
    return await _route_vision_embedding(request, model, body)
# else: existing Ollama text embedding path
```

This means Community News can keep calling `/api/embed` with `model=dinov2-vit-s14` — it just works.

#### 2.4 Streaming proxy

**File:** `src/fleet_manager/server/streaming.py`

New method `make_embedding_process_fn()` following image/STT pattern:

```python
def make_embedding_process_fn(self, queue_key, queue_manager, timeout=30.0):
    # POST to node's embedding service
    # Return single JSON chunk with embeddings
```

New method `embed_image_on_node()`:

```python
async def embed_image_on_node(self, node, body, timeout):
    # Build URL: http://{host}:{vision_embedding_port}/embed
    # POST with images
    # Return embedding vectors
```

#### 2.5 Pull support

**File:** `src/fleet_manager/server/routes/ollama_compat.py`

Extend `/api/pull` to handle vision embedding models. When model name is `dinov2-vit-s14`, `siglip2-base`, or `clip-vit-b32`:

- Route pull command to node via heartbeat command channel
- Node agent downloads model weights to `~/.fleet-manager/models/`
- Node restarts embedding server with new model available

#### 2.6 Dashboard integration

**File:** `src/fleet_manager/server/routes/dashboard.py`

- SSE events include vision embedding models in node data
- Fleet status shows embedding model availability
- Model badges: teal/cyan for embedding models (distinct from purple LLM embed, orange image, green STT)

### Phase 3: Model Knowledge & Health

#### 3.1 Model catalog

**File:** `src/fleet_manager/server/model_knowledge.py`

Add `VISION_EMBEDDING` category to `ModelCategory` enum. Add entries:

```python
ModelSpec(
    name="dinov2-vit-s14",
    category=ModelCategory.VISION_EMBEDDING,
    parameters="22M",
    size_gb=0.085,
    context_length=0,  # Not applicable
    notes="DINOv2 ViT-S/14 — 384-dim image embeddings, best visual similarity",
)
```

#### 3.2 Health check

**File:** `src/fleet_manager/server/health_engine.py`

New check `_check_vision_embedding()`:

- WARNING if vision embedding requests are failing
- INFO if vision embedding service is available but unused

#### 3.3 Fleet Intelligence

Include vision embedding status in briefing prompt context.

---

## API Contract

### Request

```
POST /api/embed-image
Content-Type: application/json

{
  "model": "dinov2-vit-s14",     // optional — node picks best available
  "images": ["base64..."],        // required — one or more base64 images
  "normalize": true               // optional — L2 normalize (default: true)
}
```

Also works via existing endpoint:

```
POST /api/embed
{
  "model": "dinov2-vit-s14",
  "input": ["base64..."]          // reuse Ollama's input field for images
}
```

### Response

```json
{
  "model": "dinov2-vit-s14",
  "embeddings": [
    [0.0234, -0.1567, 0.0891, ...]
  ],
  "dimensions": 384,
  "node": "Neons-Mac-Studio"
}
```

### Comparing Embeddings (Client-Side)

```python
import numpy as np

def cosine_similarity(a, b):
    return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))

# Frame dedup: similarity > 0.92 = same scene
similarity = cosine_similarity(frame1_embedding, frame2_embedding)
is_duplicate = similarity > 0.92
```

---

## Dependencies

### Required (new)

| Package | Purpose | Size |
|---------|---------|------|
| `onnxruntime` | CLIP inference (cross-platform) | ~50MB |
| `numpy` | Array ops | Already a dependency |
| `Pillow` | Image preprocessing | Already a dependency |

### Optional (macOS only)

| Package | Purpose | Size |
|---------|---------|------|
| `mlx` | Apple Silicon runtime | Already available if MLX-based tools installed |
| `mlx-image` | DINOv2 on MLX | ~5MB |
| `mlx-embeddings` | SigLIP on MLX | ~5MB |

Dependencies are optional extras in `pyproject.toml`:

```toml
[project.optional-dependencies]
embedding = ["onnxruntime>=1.17", "Pillow>=10.0"]
embedding-mlx = ["mlx-image>=0.1", "mlx-embeddings>=0.1"]
```

---

## Testing

### Unit tests

**New file:** `tests/test_server/test_vision_embedding.py`

- Model detection: MLX detection, ONNX fallback, no models available
- Embedding service: single image, batch images, invalid base64
- Token dimensions: DINOv2=384, SigLIP=768, CLIP=512
- Router: node selection, model auto-selection, unavailable model handling
- `/api/embed` model name routing: text models → Ollama, vision embedding models → embedding service
- Pull: model download, already cached, invalid model name

### Integration test

- Start herd + herd-node
- Pull DINOv2 model
- POST /api/embed-image with a test image
- Verify 384-dim vector returned
- POST two similar images, verify high cosine similarity
- POST two different images, verify low cosine similarity

---

## Migration for Community News

Once implemented, Community News changes one line:

```diff
- model: "clip"
+ model: "dinov2-vit-s14"
```

The existing `embedImage()` call to `/api/embed` with the image payload works as-is. The Settings UI "Pull" button works because Herd handles the download.

---

## Verification Checklist

- [ ] `herd-node` detects DINOv2/CLIP models on startup
- [ ] Heartbeat reports vision embedding capability and port
- [ ] Dashboard shows embedding model on node cards
- [ ] `POST /api/embed-image` returns correct dimension vectors
- [ ] `POST /api/embed` with `model=dinov2-vit-s14` routes to embedding service
- [ ] `POST /api/embed` with `model=nomic-embed-text` still routes to Ollama (no regression)
- [ ] `POST /api/pull` with `model=dinov2-vit-s14` downloads model weights
- [ ] ONNX fallback works when MLX is not available
- [ ] Fleet Intelligence mentions embedding capability
- [ ] All existing tests still pass
