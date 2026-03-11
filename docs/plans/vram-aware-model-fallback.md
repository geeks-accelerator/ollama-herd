# VRAM-Aware Model Fallback

**Status**: Implemented
**Date**: March 2026
**Config**: `FLEET_VRAM_FALLBACK=true` (default: enabled)

## Problem

With `keep_alive: -1` set on all routed requests, models stay loaded in VRAM indefinitely. When a request arrives for an unloaded model (e.g., `qwen3-coder:latest`), Ollama must cold-load it, which can take 10+ minutes on large models. This causes request timeouts and a poor user experience.

Example scenario: Two models loaded in VRAM (`gpt-oss:120b` at 89GB + `qwen3.5:122b-a10b` at 87GB = 176GB of 512GB), and a script requests `qwen3-coder:latest` which isn't loaded. Ollama has to swap models, triggering the timeout.

## Solution

When the requested model isn't loaded in VRAM, the router automatically routes to the best **loaded** model in the same category instead of triggering a cold load:

```
Request for qwen3-coder:latest (CODING model, not loaded)
  |
  1. Score normally -> best node has thermal=10 (COLD) -> cold load likely
  2. Detect cold thermal -> trigger VRAM-aware fallback
  3. Find loaded models across fleet -> classify each by category
  4. Match: loaded model in CODING category? -> use it
  5. No match? -> use best loaded model regardless of category
  6. Response includes X-Fleet-Fallback header
```

### Category classification

Categories come from the model name via `classify_model()` in `model_knowledge.py`, not from the prompt content:

| Category | Model name patterns |
|----------|-------------------|
| CODING | `coder`, `codestral`, `devstral`, `starcoder` |
| REASONING | `deepseek-r1`, `phi-4`, `reasoning` |
| CREATIVE | `creative`, `mistral-nemo`, `story` |
| GENERAL | Everything else |

Known models (30+ in the catalog) are matched exactly. Unknown models use heuristic name matching.

### Ranking loaded models

Loaded models are scored using the full 7-signal scoring pipeline (thermal, memory fit, queue depth, wait time, role affinity, availability trend, context fit), plus a **quality bonus** from benchmark data. This ensures bigger, higher-quality models rank first as compensation for not getting the exact requested model.

### Fallback cascade

1. **Same category**: If a loaded model matches the requested model's category, use it
2. **Cross-category**: If no same-category model is loaded, use the best loaded model regardless of category
3. **Cold load**: If no models are loaded at all, fall through to the normal cold-load path

## Implementation

### Files modified

| File | Change |
|------|--------|
| `models/config.py` | Added `vram_fallback: bool = True` setting |
| `server/scorer.py` | Added `score_loaded_models()` method |
| `server/routes/routing.py` | Added `_try_vram_fallback()` in `score_with_fallbacks()` |
| `server/health_engine.py` | Added `_check_vram_fallbacks()` health check |
| `tests/test_server/test_scorer.py` | 5 tests for `score_loaded_models()` |
| `tests/test_server/test_routing.py` | 5 tests for VRAM fallback flow |

### Routing flow change

`score_with_fallbacks()` was restructured:

1. **First pass**: Try all models (primary + fallbacks). If any scores HOT (thermal >= 50), return immediately
2. **VRAM fallback** (new): If only COLD/WARM results found and `vram_fallback=True`, try loaded models by category
3. **Cold results**: If VRAM fallback found nothing, return the cold results (triggers cold load)
4. **Holding queue**: Unchanged — retries for up to 30s if model exists but no node is available
5. **Auto-pull**: Unchanged — pulls model onto best node if it doesn't exist anywhere

### Health visibility

VRAM fallback events are tracked in-memory (last 100 events) and surface on the health page as an INFO card:

- **Title**: "VRAM fallback active: N request(s) rerouted in 24h"
- **Description**: Lists which models were most requested but not loaded
- **Fix suggestion**: `ollama pull` commands for frequently-requested models

## Configuration

| Env var | Default | Description |
|---------|---------|-------------|
| `FLEET_VRAM_FALLBACK` | `true` | Enable VRAM-aware model fallback |

Set `FLEET_VRAM_FALLBACK=false` to disable and always cold-load the requested model.

## Response headers

When a VRAM fallback occurs, the response includes `X-Fleet-Fallback: true` (existing header from the standard fallback mechanism). The `actual_model` field in traces records which model was actually used.
