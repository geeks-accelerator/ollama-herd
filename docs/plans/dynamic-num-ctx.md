# Dynamic `num_ctx` Management

**Status**: Planning
**Date**: April 2026
**Issue**: #21
**Config**: `FLEET_DYNAMIC_NUM_CTX=false` (off by default)

## Problem

Ollama allocates KV cache for the full default context window per model, even when most requests use a fraction of it. On a 512GB Mac Studio:

```
gpt-oss:120b with default_num_ctx=131072:
  Model weights:     ~67 GB
  KV cache (2 slots): ~50 GB  (131072 × 2 parallel × overhead)
  Total VRAM:        ~117 GB

With dynamic num_ctx=32768 (covers p99 of actual usage):
  Model weights:     ~67 GB
  KV cache (2 slots): ~12 GB
  Total VRAM:        ~79 GB
  Savings:           ~38 GB — enough for codestral:22b + llama3.2:1b
```

This KV cache waste prevents loading multiple models on a single node, degrades smart benchmark coverage, and wastes GPU memory bandwidth on unused cache pages.

## Solution — Three Phases

All three phases build on each other but each delivers standalone value.

### Phase 1: Observe — Track Actual Context Usage

**Goal:** Visibility into actual vs allocated context per model.

#### 1.1 Add prompt token percentile queries to TraceStore

**File:** `src/fleet_manager/server/trace_store.py`

Add method:
```python
async def get_prompt_token_stats(self, days: int = 7) -> list[dict]:
    """Per-model prompt token stats: count, p50, p75, p95, p99, max."""
```

SQL using `PERCENT_RANK()` window function (same pattern as `_refresh_cache` in latency_store.py):
```sql
WITH ranked AS (
    SELECT model, prompt_tokens,
           PERCENT_RANK() OVER (PARTITION BY model ORDER BY prompt_tokens) as prank
    FROM request_traces
    WHERE timestamp > ? AND prompt_tokens > 0 AND status = 'completed'
)
SELECT model,
    COUNT(*) as request_count,
    CAST(AVG(prompt_tokens) AS INTEGER) as avg_tokens,
    -- p50: pick row closest to 0.5
    MAX(CASE WHEN prank <= 0.50 THEN prompt_tokens END) as p50,
    MAX(CASE WHEN prank <= 0.75 THEN prompt_tokens END) as p75,
    MAX(CASE WHEN prank <= 0.95 THEN prompt_tokens END) as p95,
    MAX(CASE WHEN prank <= 0.99 THEN prompt_tokens END) as p99,
    MAX(prompt_tokens) as max_tokens
FROM ranked
GROUP BY model
ORDER BY request_count DESC
```

#### 1.2 Add dashboard API endpoint

**File:** `src/fleet_manager/server/routes/dashboard.py`

```python
@router.get("/dashboard/api/context-usage")
async def context_usage(request: Request, days: int = 7):
    """Per-model context usage stats with allocated vs actual."""
```

Returns:
```json
{
  "models": [
    {
      "model": "gpt-oss:120b",
      "allocated_ctx": 131072,
      "request_count": 5420,
      "prompt_tokens": { "avg": 2100, "p50": 1800, "p75": 3200, "p95": 8400, "p99": 16200, "max": 42000 },
      "utilization_pct": 2.4,
      "recommended_ctx": 32768,
      "savings_gb": 38.0
    }
  ]
}
```

Cross-references `prompt_token_stats` with each model's `context_length` from fleet status to compute utilization % and recommended context.

#### 1.3 Add context usage section to Health dashboard

**File:** `src/fleet_manager/server/routes/dashboard.py` (benchmarks or health page)

New card or section showing per-model context utilization:
- Model name | Allocated | Actual p95 | Utilization % | Recommended | Potential Savings
- Color-coded: green (<25% util), yellow (25-75%), red (>75%)
- Shows up in the Health tab as a new health check

#### 1.4 Add health check for context waste

**File:** `src/fleet_manager/server/health_engine.py`

New check `_check_context_waste()`:
- Uses `trace_store.get_prompt_token_stats()`
- Cross-references with loaded model context lengths
- WARNING when allocated context > 4× actual p99 usage
- Includes savings estimate in recommendation

### Phase 2: Recommend — Dashboard Settings for num_ctx

**Goal:** Let users set optimal num_ctx per model from the dashboard.

#### 2.1 Add per-model num_ctx override to settings

**File:** `src/fleet_manager/models/config.py`

```python
class ServerSettings(BaseSettings):
    # ... existing ...
    dynamic_num_ctx: bool = False                    # Master toggle
    num_ctx_overrides: dict[str, int] = {}           # Per-model: {"gpt-oss:120b": 32768}
    num_ctx_auto_calculate: bool = False             # Phase 3: auto-calculate from traces
```

#### 2.2 Apply num_ctx overrides in context protection

**File:** `src/fleet_manager/server/streaming.py`

In `_apply_context_protection()`, when `dynamic_num_ctx` is enabled:
- If the request has no `num_ctx` AND the model has an override in `num_ctx_overrides`:
  - Inject `num_ctx = override_value` into the request
  - This tells Ollama to use a smaller context window
  - Log the injection as a context protection event
- This only takes effect on first load — Ollama allocates KV cache at load time

**Important:** Injecting num_ctx only helps when the model is being loaded for the first time (cold load). For already-loaded models, changing num_ctx triggers a reload. So this setting is most effective after an Ollama restart.

#### 2.3 Dashboard settings UI for num_ctx

**File:** `src/fleet_manager/server/routes/dashboard.py` (settings page)

Add a "Context Management" section to the Settings tab:
- Toggle: `Dynamic num_ctx` (on/off)
- Per-model table showing:
  - Model | Current ctx | Actual p95 | Recommended | Override
  - "Apply Recommended" button auto-fills the override field
  - Override input: number field per model
- "Apply & Restart Ollama" button (Phase 3)

#### 2.4 Settings API for num_ctx overrides

**File:** `src/fleet_manager/server/routes/dashboard.py`

Extend `POST /dashboard/api/settings` to accept:
```json
{
  "dynamic_num_ctx": true,
  "num_ctx_overrides": {"gpt-oss:120b": 32768, "codestral:22b": 16384}
}
```

Store in `ServerSettings` at runtime. These survive as long as the router runs.

### Phase 3: Auto-Adjust — Dynamic Management with Restart

**Goal:** Automatically optimize num_ctx and restart Ollama when beneficial.

#### 3.1 Context optimizer background task

**File:** `src/fleet_manager/server/context_optimizer.py` (new)

```python
class ContextOptimizer:
    """Periodically analyzes prompt token usage and optimizes num_ctx."""

    async def run(self, interval: float = 300):
        """Background loop: check every 5 minutes."""
        while True:
            await self._check_and_optimize()
            await asyncio.sleep(interval)

    async def _check_and_optimize(self):
        """Compare current num_ctx vs actual usage, update overrides."""
        stats = await self._trace_store.get_prompt_token_stats(days=7)
        for model_stats in stats:
            model = model_stats["model"]
            p99 = model_stats["p99"]
            current_ctx = self._get_allocated_ctx(model)

            # Recommend: next power-of-2 above p99, min 2048
            recommended = max(2048, self._next_power_of_2(p99 * 1.25))

            if current_ctx > recommended * 4:
                # Allocated is >4x what's needed — recommend reduction
                self._settings.num_ctx_overrides[model] = recommended
```

#### 3.2 Router-to-node command channel

**File:** `src/fleet_manager/server/routes/heartbeat.py`

Extend heartbeat response to include commands:
```json
{
  "status": "ok",
  "node_status": "online",
  "commands": [
    {"type": "restart_ollama", "env": {"OLLAMA_NUM_CTX": "32768"}}
  ]
}
```

**File:** `src/fleet_manager/node/agent.py`

Process commands in heartbeat response:
```python
async def _send_heartbeat(self, payload):
    resp = await self._http.post(...)
    data = resp.json()
    for cmd in data.get("commands", []):
        if cmd["type"] == "restart_ollama":
            await self._restart_ollama(cmd.get("env", {}))
```

Add `_restart_ollama(env_overrides)`:
1. Send drain heartbeat (no new requests)
2. Wait for in-flight to complete (up to 30s)
3. Kill Ollama process
4. Set new env vars
5. Start Ollama with updated env
6. Wait for healthy
7. Resume normal heartbeats

#### 3.3 Dashboard "Apply & Restart" flow

**File:** `src/fleet_manager/server/routes/dashboard.py`

New endpoint:
```python
@router.post("/dashboard/api/context/apply")
async def apply_context_settings(request: Request):
    """Queue Ollama restart with new num_ctx for a node."""
```

Stores the restart command in the registry's pending commands for the target node. Next heartbeat picks it up.

Dashboard UI:
- "Apply & Restart" button shows confirmation dialog
- "Restarting Ollama on {node}..." progress indicator
- Shows before/after memory savings estimate

#### 3.4 Auto-restart on high error rates

**File:** `src/fleet_manager/server/context_optimizer.py`

If `num_ctx_auto_calculate` is enabled:
- Monitor error rate for context-related failures
- If a request genuinely needs more context than the override allows:
  - Queue a restart with higher num_ctx (next power-of-2 above the failed request)
  - Rate-limit restarts to max 1 per 30 minutes per node
- If error rate drops after restart, confirm the new setting
- Log all decisions to JSONL for observability

## Files to Create/Modify

| File | Action | Phase |
|------|--------|-------|
| `src/fleet_manager/server/trace_store.py` | Add `get_prompt_token_stats()` | 1 |
| `src/fleet_manager/server/health_engine.py` | Add `_check_context_waste()` | 1 |
| `src/fleet_manager/server/routes/dashboard.py` | Add `/dashboard/api/context-usage` endpoint + health section | 1 |
| `src/fleet_manager/models/config.py` | Add `dynamic_num_ctx`, `num_ctx_overrides`, `num_ctx_auto_calculate` | 2 |
| `src/fleet_manager/server/streaming.py` | Inject num_ctx overrides in `_apply_context_protection()` | 2 |
| `src/fleet_manager/server/routes/dashboard.py` | Add settings UI section for context management | 2 |
| `src/fleet_manager/server/context_optimizer.py` | **Create** — background optimizer task | 3 |
| `src/fleet_manager/server/routes/heartbeat.py` | Add commands to heartbeat response | 3 |
| `src/fleet_manager/node/agent.py` | Process restart commands, add `_restart_ollama()` | 3 |
| `src/fleet_manager/server/app.py` | Start context optimizer in lifespan | 3 |

## Existing Code to Reuse

| Component | Location | What to Reuse |
|-----------|----------|---------------|
| Percentile SQL pattern | `latency_store.py` `_refresh_cache()` | `PERCENT_RANK()` window function |
| Context protection events | `streaming.py` lines 20-48 | `_record_context_protection()` |
| Settings toggle pattern | `dashboard.py` lines 493-514 | `POST /dashboard/api/settings` |
| Health check pattern | `health_engine.py` | `Recommendation` dataclass, severity levels |
| Ollama startup | `agent.py` lines 47-107 | `_ensure_ollama()` for restart logic |
| Drain signal | `agent.py` lines 361-374 | Graceful shutdown pattern |

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_DYNAMIC_NUM_CTX` | `false` | Enable context size overrides |
| `FLEET_NUM_CTX_AUTO_CALCULATE` | `false` | Enable auto-calculation from traces (Phase 3) |

Runtime-configurable via dashboard settings. Overrides stored in memory (lost on router restart — Phase 3 could persist to SQLite).

## Verification

### Phase 1
1. `uv run pytest` passes
2. `curl /dashboard/api/context-usage` returns per-model stats
3. Health tab shows context waste warning for gpt-oss:120b
4. Dashboard shows: "gpt-oss:120b: allocated 131K, actual p95 8K, utilization 6%"

### Phase 2
1. Enable `dynamic_num_ctx` in dashboard settings
2. Set override for gpt-oss:120b to 32768
3. Cold-load gpt-oss:120b → verify it loads with 32K context
4. Verify codestral:22b can now coexist (freed ~38GB)
5. Smart benchmark loads multiple models successfully

### Phase 3
1. Auto-restart: change override → node restarts Ollama within 1 heartbeat
2. Error handling: request needing 64K on 32K override → auto-increases
3. Rate limiting: max 1 restart per 30 minutes per node
4. Dashboard shows restart history and before/after memory

## Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| num_ctx too low truncates prompts | Incorrect model responses | Auto-detect and increase on context errors; min 2048 floor |
| Ollama restart kills in-flight requests | Client errors | Drain before restart; wait for in-flight to complete |
| Frequent restarts from oscillating usage | Fleet instability | Rate-limit restarts to 1/30min; require >4× waste before recommending |
| num_ctx override not respected by all models | Wasted effort | Some models have minimum context — detect and warn |
| Settings lost on router restart | User confusion | Phase 3: persist overrides to SQLite |
