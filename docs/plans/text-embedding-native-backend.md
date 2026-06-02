# Native Text Embedding Backend (fastembed / Apple Silicon)

## Context

Embed requests to `nomic-embed-text` via Ollama were failing with ReadTimeout under concurrent LLM load (202 errors over 1.5h on June 1, 2026). Root cause: `OLLAMA_NUM_PARALLEL` is a software queue limit — when gpt-oss:120b and gemma3:27b hold both slots, embed requests queue indefinitely inside Ollama. Hardware had 291 GB free and 14% CPU the entire time. A retry + trace-visibility fix was shipped, but it treats symptoms. The structural fix is to route text embeddings **out of Ollama entirely** — a dedicated fastembed server runs `nomic-embed-text-v1.5` natively via ONNX on M3 Ultra CPU, handling `/api/embed` calls before they ever reach Ollama.

**Goal:** Zero contention between LLM inference and embed requests. The embed path never touches Ollama.

---

## Technology Choice: fastembed

- **Package:** `fastembed>=0.4.0` (Qdrant, actively maintained, v0.8.0 March 2026)
- **Backend:** ONNX Runtime — no PyTorch dependency, no CUDA
- **Model:** `nomic-ai/nomic-embed-text-v1.5-Q` (130 MB int8 quantized, 768 dims, 8192 token context)
- **Why:** First-class nomic-embed-text-v1.5 support, zero torch, ~2k-4k sentences/sec on M3 Ultra ARM NEON cores, production-grade
- **NOT MLX:** mlx-embedding packages don't support nomic-embed-text-v1.5 and are stalled/pre-release (investigated 2026-06-01)

---

## Architecture

Follows the **exact same pattern** as the existing vision embedding backend (port 11438). Text embedding runs on **port 11439** (`ollama_port + 5`).

```
Client  POST /api/embed  (model=nomic-embed-text)
    └─ ollama_compat.ollama_embed()
        ├─ is_vision_embedding_model(model)?  → embed_image()    [existing, port 11438]
        ├─ is_text_embedding_model(model)?    → embed_text()     [NEW, port 11439]
        └─ else                               → Ollama /api/embed [fallback, unchanged]
```

Ollama is only called for text embedding models that are NOT in the native registry (e.g. `mxbai-embed-large` if only `nomic-embed-text` is configured). The native path is transparent — clients see the same Ollama-compatible JSON shape.

---

## fastembed API (exact)

```python
from fastembed import TextEmbedding

# Constructor
model = TextEmbedding(
    model_name="nomic-ai/nomic-embed-text-v1.5-Q",
    cache_dir="/path/to/cache",   # default: system temp dir — we set ~/.fleet-manager/models/text-embedding/
    threads=8,                     # ONNX Runtime threads (tune to M3 Ultra core count)
    lazy_load=True,                # defer load until first embed() call
)

# Inference — returns Generator[np.ndarray] (one vector per input)
embeddings = list(model.embed(["search_query: What is TSNE?"], batch_size=32))
# embeddings: list of np.ndarray, each shape (768,)

# Batch
embeddings = list(model.embed(["text1", "text2", "text3"], batch_size=32, parallel=0))
# parallel=0 → auto-detect CPU cores; None → single-threaded
```

**Task prefixes:** NOT automatic — caller prepends `"search_query: "` or `"search_document: "` etc. Ollama also doesn't add them. We pass `input` through as-is, same behavior as Ollama.

**Cache path:** We set `cache_dir=~/.fleet-manager/models/text-embedding/` (consistent with vision embedding cache at `~/.fleet-manager/models/`). Detectable by checking for subdirectories in that path.

---

## Ollama Response Format (must match exactly)

```json
{
  "model": "nomic-embed-text",
  "embeddings": [[0.010071, -0.001759, ...]],
  "total_duration": 14143917,
  "load_duration": 0,
  "prompt_eval_count": 8
}
```

- `embeddings`: array of arrays (one per input) — L2-normalized  
- Accepts `input` as string or list of strings  
- Duration fields in nanoseconds (we set accurately)  

---

## Files to Create

### `src/fleet_manager/node/text_embedding_models.py`

Model registry for fastembed text embedding models. Mirrors `embedding_models.py` structure.

```python
TEXT_EMBEDDING_CACHE_DIR = Path.home() / ".fleet-manager" / "models" / "text-embedding"

TEXT_EMBEDDING_MODELS: dict[str, dict] = {
    "nomic-embed-text": {
        "fastembed_name": "nomic-ai/nomic-embed-text-v1.5-Q",
        "dimensions": 768,
        "max_tokens": 8192,
        "description": "nomic-embed-text-v1.5 quantized (int8, 130 MB) — replaces Ollama nomic-embed-text",
    },
    "nomic-embed-text:latest": {   # Ollama tag alias
        "fastembed_name": "nomic-ai/nomic-embed-text-v1.5-Q",
        "dimensions": 768,
        "max_tokens": 8192,
        "description": "Alias for nomic-embed-text",
    },
}

# Public aliases used by is_text_embedding_model()
TEXT_EMBEDDING_MODEL_NAMES: set[str] = set(TEXT_EMBEDDING_MODELS.keys())

def is_text_embedding_model(model: str) -> bool:
    return model.lower().strip() in TEXT_EMBEDDING_MODEL_NAMES

def get_fastembed_name(model: str) -> str:
    """Resolve Ollama model name to fastembed model identifier."""
    ...

def is_model_cached(model: str) -> bool:
    """Check if model weights are on disk without loading fastembed."""
    # Check TEXT_EMBEDDING_CACHE_DIR / fastembed_name for files
    ...
```

### `src/fleet_manager/node/text_embedding_server.py`

FastAPI server running on port 11439. Mirrors `embedding_server.py`.

```python
_model: TextEmbedding | None = None
_model_name: str = ""

router = APIRouter()

@router.post("/embed")
async def embed_text(request: Request):
    body = await request.json()
    model = body.get("model", "nomic-embed-text")
    raw_input = body.get("input") or body.get("prompt", "")
    
    # Normalize input — string or list[str]
    texts: list[str] = [raw_input] if isinstance(raw_input, str) else list(raw_input)
    if not texts:
        return JSONResponse(status_code=400, content={"error": "input is required"})
    
    # Lazy load / swap model
    backend = await _get_model(model)
    
    start = time.perf_counter_ns()
    embeddings = [v.tolist() for v in backend.embed(texts, batch_size=32)]
    elapsed_ns = time.perf_counter_ns() - start
    
    return JSONResponse({
        "model": model,
        "embeddings": embeddings,
        "total_duration": elapsed_ns,
        "load_duration": 0,
        "prompt_eval_count": sum(len(t.split()) for t in texts),  # approx
    })

@router.get("/models")
async def list_models():
    """Return available text embedding models and cache status."""
    ...
```

### `src/fleet_manager/server/routes/text_embedding_compat.py`

Router-side endpoint. Mirrors `embedding_compat.py`. Called when `is_text_embedding_model()` returns true.

- Scores nodes by: `vision_embedding` availability (has text_embedding), idle state, available memory
- Extracts `hostname` from `node.ollama_base_url`, builds `http://{host}:{text_embedding_port}/embed`
- Proxies request body, returns response JSON
- Returns 503 if no node has text embedding available (falls back gracefully — caller handles)
- Re-exports `is_text_embedding_model` for import in `ollama_compat.py`

---

## Files to Modify

### `pyproject.toml`

Extend existing `embedding` extra (not a new separate extra — same `uv sync --extra embedding` command, simpler for operators):

```toml
[project.optional-dependencies]
embedding = [
    "numpy>=1.24",
    "Pillow>=10.0",
    "onnxruntime>=1.17",
    "huggingface-hub>=0.20",
    "fastembed>=0.4.0",      # ← ADD: text embedding via ONNX, no torch
]
```

**Why same extra:** Vision and text embedding are both "embedding" capabilities. Operators already run `uv sync --extra embedding` for vision. Adding fastembed here means one command enables both. The dependency is lightweight (no torch), so there's no cost to bundling.

### `src/fleet_manager/models/node.py`

Add Pydantic models + heartbeat fields (lines ~130 and ~192):

```python
# New models (after VisionEmbeddingMetrics):
class TextEmbeddingModel(BaseModel):
    name: str           # "nomic-embed-text"
    dimensions: int     # 768
    cached: bool        # weights on disk

class TextEmbeddingMetrics(BaseModel):
    models_available: list[TextEmbeddingModel]
    processing: bool = False

# In HeartbeatPayload (after vision_embedding_port):
text_embedding: TextEmbeddingMetrics | None = None
text_embedding_port: int = 0
text_embedding_status: dict = Field(default_factory=dict)
# status shape: {"backend_available": bool, "cached_model_count": int}
```

### `src/fleet_manager/node/collector.py`

Add two functions (mirroring `_detect_vision_embedding_models` and `_vision_backend_status`):

```python
def _detect_text_embedding_models() -> TextEmbeddingMetrics | None:
    # Stage 1: backend importable?
    try:
        import fastembed  # noqa: F401
    except ImportError:
        return None
    # Stage 2: which models cached?
    from fleet_manager.node.text_embedding_models import TEXT_EMBEDDING_MODELS, is_model_cached
    models = [
        TextEmbeddingModel(name=name, dimensions=spec["dimensions"], cached=True)
        for name, spec in TEXT_EMBEDDING_MODELS.items()
        if is_model_cached(name) and not name.endswith(":latest")  # skip aliases
    ]
    if not models:
        return None  # fastembed installed but no models cached yet
    return TextEmbeddingMetrics(models_available=models)

def _text_embedding_backend_status() -> dict:
    try:
        import fastembed  # noqa: F401
        backend_available = True
    except ImportError:
        backend_available = False
    from fleet_manager.node.text_embedding_models import TEXT_EMBEDDING_MODELS, is_model_cached
    cached = sum(1 for name in TEXT_EMBEDDING_MODELS if is_model_cached(name) and not name.endswith(":latest"))
    return {"backend_available": backend_available, "cached_model_count": cached}
```

Call both in `collect_heartbeat()`, include in `HeartbeatPayload` return.

**Note on server startup strategy:** The text embedding server starts even if no models are cached yet (as long as fastembed is importable). fastembed auto-downloads on first request. This means the server advertises availability and the first request triggers a one-time 130 MB download — acceptable for production. Health check warns if backend is not installed.

### `src/fleet_manager/node/agent.py`

Add `_ensure_text_embedding_server()` and wire into lifecycle:

- `__init__`: add `self._text_embedding_server_task: asyncio.Task | None = None` and `self._text_embedding_port: int = 0`
- `start()`: call `await self._ensure_text_embedding_server()` alongside vision server startup
- Heartbeat loop: inject `text_embedding_port` into payload when non-zero
- `_drain()`: cancel `self._text_embedding_server_task`

Port formula: `ollama_port + 5` → **11439** (confirmed unused in codebase)

**Startup gate:** Start if `fastembed` is importable (regardless of cached models — fastembed auto-downloads on first request). Silently skip with `logger.debug(...)` if fastembed not installed.

### `src/fleet_manager/server/routes/ollama_compat.py`

Add dispatch between vision check and Ollama fallback in `ollama_embed()`:

```python
# After is_vision_embedding_model check, before Ollama routing:
from fleet_manager.server.routes.text_embedding_compat import (
    embed_text,
    is_text_embedding_model,
)
if is_text_embedding_model(model):
    request.state._parsed_body = body
    return await embed_text(request)
```

### `src/fleet_manager/server/health_engine.py`

Add `_check_text_embedding_backend_missing()` mirroring `_check_vision_backend_missing()`:
- `check_id="text_embedding_backend_missing"`
- WARNING when: `cached_model_count > 0` AND `backend_available == False`
- Fix text: `uv sync --extra embedding` (since fastembed is in that extra)
- Wire into `run()` alongside vision check

### `CLAUDE.md`

- Update test count after tests added
- Update health check count (`32 + 1 = 33` checks)
- Add `text_embedding_backend_missing` gotcha (same structure as `vision_backend_missing` gotcha)
- Add `--extra embedding` now includes fastembed note in "Without --extra embedding" section

---

## Server Startup Behavior

| fastembed installed | Model cached | Server started | Heartbeat advertises |
|---|---|---|---|
| No | No | No | Nothing |
| No | Yes | No | `text_embedding_status.backend_available=false` → health WARNING |
| Yes | No | **Yes** | `text_embedding_port=11439`, no models in list yet |
| Yes | Yes | Yes | `text_embedding_port=11439`, model in list |

When server is started but model not cached, first `/api/embed` call triggers a 130 MB download (~30s on good network). Subsequent calls are fast. This matches the lazy-load behavior of Ollama itself.

---

## Model Registry (initial set)

Start with one model only — avoids scope creep. Additional models can be added later without architecture changes:

| Ollama name | fastembed name | Size | Dims |
|---|---|---|---|
| `nomic-embed-text` | `nomic-ai/nomic-embed-text-v1.5-Q` | 130 MB | 768 |
| `nomic-embed-text:latest` | (alias) | — | — |

`mxbai-embed-large`, `all-minilm` etc. are easily added to `TEXT_EMBEDDING_MODELS` — no code changes, just registry entries.

---

## Test Plan

### Unit tests (`tests/test_server/test_text_embedding.py`)
- `test_is_text_embedding_model()` — model name detection
- `test_embed_text_success()` — mock fastembed, verify Ollama-compatible response shape
- `test_embed_text_string_input()` — single string input normalized to list
- `test_embed_text_list_input()` — list of strings
- `test_embed_text_no_node()` — graceful 503 when no node has text embedding
- `test_detect_text_embedding_models_no_fastembed()` — returns None when import fails
- `test_text_embedding_backend_missing_health_check()` — fires WARNING correctly

### Integration (manual)
```bash
# Install text embedding
uv sync --extra embedding   # now includes fastembed

# Restart node
pkill -f "herd-node" && uv run herd-node &>/dev/null & disown

# Test — should hit native server, NOT Ollama
curl -s http://localhost:11435/api/embed \
  -H "Content-Type: application/json" \
  -d '{"model":"nomic-embed-text","input":"What is TSNE?"}' | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(f'dims={len(d[\"embeddings\"][0])} node={d.get(\"x-fleet-node\",\"native\")}')
print(f'total_duration={d.get(\"total_duration\",\"?\")}ns')
"

# Verify Ollama was NOT hit (check Ollama request count stays flat)
curl -s http://localhost:11434/api/ps

# Health should show text_embedding_backend_missing ABSENT
curl -s http://localhost:11435/dashboard/api/health | python3 -c "
import json,sys
d=json.load(sys.stdin)
checks=[r['check_id'] for r in d['recommendations']]
print('text_embedding_backend_missing' in checks, checks)
"

# Run tests
uv run pytest tests/ -q
```

---

## Implementation Order

1. `pyproject.toml` — add fastembed to embedding extra
2. `node/text_embedding_models.py` — model registry (new file)
3. `node/text_embedding_server.py` — FastAPI server (new file)
4. `models/node.py` — TextEmbeddingModel, TextEmbeddingMetrics, HeartbeatPayload fields
5. `node/collector.py` — detection + status functions
6. `node/agent.py` — server lifecycle
7. `server/routes/text_embedding_compat.py` — router endpoint (new file)
8. `server/routes/ollama_compat.py` — dispatch hook
9. `server/health_engine.py` — backend missing check
10. Tests + CLAUDE.md update
