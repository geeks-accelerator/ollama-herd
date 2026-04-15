# Vision Model Support Plan

## Context

Users want to send images (e.g., 1080p government meeting frames with speakers and presentation slides) and get text descriptions back. The routing pipeline already passes multimodal payloads through unchanged to Ollama — no data is stripped or corrupted. What's missing is vision-aware model classification, image token estimation for accurate scoring, and dedicated vision models in the catalog.

## What Works Today

- OpenAI `/v1/chat/completions` with `image_url` content blocks passes through fine
- Ollama `/api/chat` with `images` field passes through fine
- Streaming proxy is format-agnostic — forwards payloads unchanged
- `estimate_tokens()` already handles multimodal OpenAI format (counts text parts, ignores image parts)
- Gemma 3 models are in the catalog but only noted as "multimodal capable" in their notes string

## What's Missing

1. **No VISION category** — vision-capable LLMs are classified as GENERAL, not queryable as vision models
2. **No vision model entries** — llama3.2-vision, llava, moondream not in catalog
3. **Image token estimation** — context fit scoring ignores image tokens (~130-200 per image depending on resolution)
4. **No vision-specific tests** — no automated tests for multimodal chat in either format
5. **No vision request tracking** — traces don't distinguish vision vs text requests

## Phase 1: Model Catalog & Classification (Must Do)

### 1.1 Add VISION category to ModelCategory enum

**File:** `src/fleet_manager/server/model_knowledge.py`

Add `VISION = "vision"` to `ModelCategory`. This is distinct from `IMAGE` (which is image generation, text-to-image). VISION is image-to-text (understanding).

### 1.2 Add vision models to MODEL_CATALOG

| Model | Params | RAM (Q4) | Context | Notes |
|-------|--------|----------|---------|-------|
| `gemma3:4b` | 4B | 4 GB | 128K | Lightweight vision, already in catalog — update category |
| `gemma3:12b` | 12B | 9 GB | 128K | Already in catalog — update category |
| `gemma3:27b` | 27B | 19 GB | 128K | Best balance for OCR + scene. Already in catalog — update category |
| `llama3.2-vision:11b` | 11B | 8 GB | 128K | Meta's dedicated vision model |
| `llama3.2-vision:90b` | 90B | 55 GB | 128K | Highest quality vision, needs big machine |
| `llava:7b` | 7B | 5 GB | 4K | Older but proven, short context |
| `llava:13b` | 13B | 10 GB | 4K | Better quality llava |
| `llava:34b` | 34B | 22 GB | 4K | Best llava variant |
| `moondream:1.8b` | 1.8B | 2 GB | 2K | Ultra-lightweight, basic OCR |
| `minicpm-v:8b` | 8B | 6 GB | 4K | Compact, good document understanding |

For gemma3 models already in the catalog: change `category` to `ModelCategory.VISION` and add `ModelCategory.GENERAL` (or `CODING`) to `secondary_categories`. This way they show up when someone queries for vision models but are still recommended for general use.

### 1.3 Add `is_vision_model()` helper

Similar to existing `is_image_model()` and `is_thinking_model()`:

```python
def is_vision_model(name: str) -> bool:
    spec = lookup_model(name)
    if spec:
        return spec.category == ModelCategory.VISION or ModelCategory.VISION in spec.secondary_categories
    # Heuristic fallback
    lower = name.lower()
    return any(p in lower for p in ("vision", "llava", "moondream", "minicpm-v"))
```

### 1.4 Update `classify_model()` heuristic fallback

Add vision pattern matching:
```python
if any(k in lower for k in ("vision", "llava", "moondream", "minicpm-v")):
    return ModelCategory.VISION
```

## Phase 2: Image Token Estimation (Should Do)

### 2.1 Add image token cost to `estimate_tokens()`

**File:** `src/fleet_manager/server/scorer.py`

The function already handles multimodal OpenAI format and skips image parts. Add estimation:

- Low-res images: ~85 tokens
- High-res images (1080p): ~170 tokens  
- Default estimate: ~150 tokens per image (conservative for 1080p meeting frames)

For OpenAI format: count `image_url` content blocks and add `150 * image_count`.

For Ollama format: count items in `images` list field per message and add `150 * image_count`.

This improves context fit scoring accuracy for vision requests.

## Phase 3: Tests (Should Do)

### 3.1 Vision routing tests

**File:** `tests/test_server/test_vision_routing.py`

- Test OpenAI multimodal format routes to node with vision model loaded
- Test Ollama `images` field passes through to streaming proxy
- Test vision model scoring prefers nodes with vision models hot
- Test fallback from vision model to another vision model
- Test `estimate_tokens()` includes image token costs
- Test `is_vision_model()` for known and unknown models
- Test `classify_model()` returns VISION for vision model names

## Phase 4: Analytics & Dashboard (Nice to Have)

### 4.1 Vision request tracking in traces

Add `has_images: bool` field to `InferenceRequest` — set to `True` when messages contain image content. This enables:

- Dashboard filtering: vision vs text requests
- Per-model vision request volume
- Vision-specific latency tracking (vision requests are slower)

### 4.2 Model recommender vision awareness

Update `model_recommender.py` to:
- Check if any node has a vision model loaded
- If vision requests appear in traces but no vision model is available, recommend one
- Suggest gemma3:27b as default vision recommendation (good OCR + scene understanding)

## Use Case: Government Meeting Analysis

For the specific use case of analyzing 1080p government meeting recordings:

**Recommended model:** `gemma3:27b` (19 GB RAM)
- Strong OCR for presentation slides with bullet points, charts, small text
- Good scene understanding for speaker identification and room layout
- 128K context for detailed descriptions
- ~5-10 seconds per frame on Mac Studio M2 Ultra

**Budget option:** `llama3.2-vision:11b` (8 GB RAM)
- Decent OCR, good speaker detection
- Fits on smaller machines alongside other models
- ~3-5 seconds per frame

**Processing approach:**
- Extract key frames from video (scene changes, new slides)
- Send each frame with prompt like "Describe this government meeting frame. If there are presentation slides visible, transcribe all text on them."
- Route through Herd — fleet handles concurrent frame analysis across nodes

## Files to Modify

| File | Change |
|------|--------|
| `src/fleet_manager/server/model_knowledge.py` | Add VISION category, vision models, `is_vision_model()` |
| `src/fleet_manager/server/scorer.py` | Image token estimation in `estimate_tokens()` |
| `src/fleet_manager/models/request.py` | Optional `has_images` field on InferenceRequest |
| `src/fleet_manager/server/model_recommender.py` | Vision category awareness |
| `tests/test_server/test_vision_routing.py` | New test file |
| `tests/test_server/test_scorer.py` | Image token estimation tests |

## Estimated Effort

- Phase 1: ~1 hour (catalog updates, helpers)
- Phase 2: ~30 min (token estimation)
- Phase 3: ~1 hour (tests)
- Phase 4: ~2 hours (analytics, dashboard, recommender)
