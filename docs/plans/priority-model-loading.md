# Priority Model Loading Plan

## Context

When the fleet restarts (pkill, crash, macOS update), Ollama unloads all models from memory. The first request that comes in determines what gets loaded. If that request is for `gemma3:27b` (42GB) instead of `gpt-oss:120b` (89GB), the smaller model loads first and consumes memory that the primary model needs. VRAM fallback makes this worse — it routes requests to whatever is loaded, reinforcing the wrong model.

**Discovered:** 2026-04-16 — during vision embedding testing, repeated restarts evicted `gpt-oss:120b` and VRAM fallback loaded `gemma3:27b` in its place.

## Design: Data-Driven Priority

Instead of manual config (`FLEET_PINNED_MODELS`), derive priority from actual usage data in the trace store. The most-used models load first after restart.

### Priority Score Formula

```
priority_score = (requests_24h * 3) + (requests_7d_daily_avg * 1)
```

- **24h weight (3x)** — catches workload shifts fast. If you started using a new model today, it rises quickly.
- **7d average (1x)** — stabilizes against one-off spikes. A model with steady daily usage won't drop off because of a quiet afternoon.

Example:

| Model | 24h Requests | 7d Total | 7d Daily Avg | Score |
|-------|-------------|----------|-------------|-------|
| gpt-oss:120b | 50 | 350 | 50 | **200** |
| nomic-embed-text | 30 | 210 | 30 | **120** |
| gemma3:4b | 5 | 35 | 5 | **20** |
| codestral:22b | 0 | 14 | 2 | **2** |

After restart: load `gpt-oss:120b` first (score 200), then `nomic-embed-text` (120), then `gemma3:4b` (20), stopping when memory is full.

### Implementation

#### 1. Trace Store Query (DONE)

**File:** `src/fleet_manager/server/trace_store.py`

New method `get_model_priority_scores()` — queries request_traces, computes weighted score, returns models sorted by priority. Already implemented.

#### 2. Startup Preloading

**File:** `src/fleet_manager/server/app.py` (or new `src/fleet_manager/server/model_preloader.py`)

After the first node registers (detected via registry callback or periodic check):

```python
async def preload_priority_models(registry, trace_store, proxy):
    """Load highest-priority models after fleet restart."""
    priorities = await trace_store.get_model_priority_scores()
    if not priorities:
        return  # No history — nothing to preload

    nodes = registry.get_online_nodes()
    if not nodes:
        return

    for entry in priorities:
        model = entry["model"]
        # Skip models not available on any node
        available_nodes = [
            n for n in nodes
            if n.ollama and model in [m.name for m in n.ollama.models_available]
        ]
        if not available_nodes:
            continue

        # Skip if already loaded
        loaded_nodes = [
            n for n in nodes
            if n.ollama and model in [m.name for m in n.ollama.models_loaded]
        ]
        if loaded_nodes:
            continue

        # Check if there's enough memory on the best node
        best = max(available_nodes, key=lambda n: n.memory.available_gb if n.memory else 0)
        model_size = _estimate_model_size(model)
        if best.memory and best.memory.available_gb < model_size * 1.2:
            logger.info(f"Priority preload stopping: not enough memory for {model} ({model_size:.0f}GB)")
            break

        logger.info(f"Priority preload: loading {model} (score={entry['priority_score']}) on {best.node_id}")
        await proxy.pre_warm(best.node_id, model)
```

**Trigger:** Run once after the first heartbeat from any node, with a short delay (5-10s) to let the node fully register.

**Key behaviors:**
- Only loads models that are available on disk (won't trigger downloads)
- Stops when memory is full (doesn't evict already-loaded models)
- Skips models already loaded (idempotent)
- Logs what it loads and why
- Non-blocking — runs as a background task

#### 3. VRAM Fallback Protection

**File:** `src/fleet_manager/server/routes/routing.py`

Before routing to a fallback model, check if the requested model has higher priority:

```python
# In _try_vram_fallback():
# If the requested model has significantly higher priority than the
# fallback candidate, don't fallback — queue the request instead and
# let it wait for the right model to load.
req_priority = _get_cached_priority(inference_req.model)
fallback_priority = _get_cached_priority(best_model)

if req_priority > fallback_priority * 2:
    logger.info(
        f"VRAM fallback blocked: '{inference_req.model}' (priority={req_priority}) "
        f"is higher priority than '{best_model}' (priority={fallback_priority})"
    )
    return None  # Let the request queue instead of routing to wrong model
```

**Cache:** Priority scores are cached for 5 minutes (recomputed from trace_store) to avoid hitting SQLite on every request.

#### 4. Health Check

**File:** `src/fleet_manager/server/health_engine.py`

New check `_check_priority_models()`:

- **WARNING** if a model with priority score > 50 is available on disk but not loaded
- **INFO** when priority preloading completed successfully
- Shows which models are loaded vs which should be loaded based on priority

#### 5. Dashboard / Fleet Intelligence

- Fleet Intelligence briefing includes priority model status
- "Priority Models" section in health recommendations when mismatched

---

## What This Does NOT Do

- **No manual pinned models config** — purely data-driven. If you want to force a model, just use it and its priority will rise naturally.
- **No model downloads** — only loads models already on disk. Priority preloading is not auto-pull.
- **No cross-node coordination** — each node loads its own highest-priority models based on global traces. Multi-node priority balancing is a separate feature.

## Files Changed

| File | Change |
|------|--------|
| `server/trace_store.py` | `get_model_priority_scores()` (DONE) |
| `server/app.py` | Startup hook to trigger preloading after first node registers |
| `server/routes/routing.py` | VRAM fallback checks priority before routing |
| `server/health_engine.py` | New `_check_priority_models()` health check |
| `server/routes/dashboard.py` | Fleet Intelligence includes priority model context |

## Verification

- [ ] Restart fleet — highest-priority models load automatically
- [ ] `gpt-oss:120b` loads before `gemma3:27b` (higher historical usage)
- [ ] VRAM fallback doesn't route away from a high-priority model
- [ ] Health page shows WARNING when priority model is not loaded
- [ ] Fleet Intelligence mentions priority model status
- [ ] Existing tests still pass (no regression)
- [ ] Preloader stops when memory is full (doesn't over-commit)
