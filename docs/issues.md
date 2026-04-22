# Known Issues & Improvements

Identified via code review of the full codebase. Organized by priority.

**Status key:** `OPEN` — not yet addressed. `PARTIAL` — partially fixed. `FIXED` — resolved.

---

## Routing Safety

### Ollama native image models can evict LLMs from memory `PARTIAL`

**File:** `src/fleet_manager/server/routes/ollama_compat.py`
**Severity:** High

When an Ollama native image model (e.g., `x/z-image-turbo` at 12GB) is requested via `/api/generate`, Ollama may evict the resident LLM to make room. On a single-node fleet, this means ALL text inference fails with 500 errors until the LLM is reloaded.

**Observed:** 2026-03-30. After generating images with `x/z-image-turbo`, `gpt-oss:120b` was evicted. All DriftsBot text requests failed with 500 for several minutes.

**Proposed fixes (in order of complexity):**
1. **Prefer mflux over Ollama native** — when both mflux `z-image-turbo` and Ollama `x/z-image-turbo` are available, prefer mflux since it doesn't compete for Ollama VRAM
2. **Guard single-LLM nodes** — don't route Ollama native image requests to a node if it's the only node serving text LLM requests and the image model isn't already loaded
3. **Memory budget check** — before routing, verify that loading the image model won't push total VRAM past available memory (Ollama reports `size_vram` per model)
4. **Auto-unload after generation** — send `keep_alive: 0` after image generation completes to immediately free VRAM for the LLM

**Fix #1 implemented:** The router now prefers mflux over Ollama native when both are available. If a client requests `x/z-image-turbo` via `/api/generate` and mflux has `z-image-turbo` on any node, the router redirects to the mflux image server automatically. Ollama native is only used as a fallback when mflux isn't installed. This prevents LLM eviction because mflux runs as a separate subprocess outside Ollama's VRAM.

**Remaining:** Fixes #2 (guard single-LLM nodes) and #3 (memory budget check) are not yet implemented. These would protect against Ollama native image models on multi-node fleets where some nodes have mflux and others don't.

---

## External Dependencies

### DiffusionKit `argmaxtools` crashes on macOS 26+ `FIXED` (local patch)

**File:** `argmaxtools/test_utils.py` (installed dependency, not our code)
**Severity:** High (blocks all DiffusionKit image generation)

The `os_spec()` function in `argmaxtools.test_utils` parses `sw_vers` output expecting exactly 3 lines. macOS 26 added a `ProductVersionExtra` field (4th line), causing `IndexError: list index out of range`. This crashes `diffusionkit-cli` on any image generation attempt.

**Workaround applied:** Patched the installed `test_utils.py` to parse `sw_vers` output as a key-value dict instead of positional list. See [image generation guide](guides/image-generation.md) for the patch instructions.

**Upstream status:** No fix as of `argmaxtools` v0.1.23 (2026-03-30). The `argmaxtools` repo appears to be private — no way to submit a PR directly. Filed on DiffusionKit GitHub as the integration surface.

**Note:** This patch must be re-applied after any `uv tool upgrade diffusionkit` or `pip install --upgrade diffusionkit`.

---

### DiffusionKit SD3.5 Large — Python crash on cleanup `OPEN`

**File:** `diffusionkit/mlx/__init__.py` (installed dependency)
**Severity:** Low (image generates successfully, crash is post-generation)

SD3.5 Large (11.6GB peak memory) occasionally triggers a "Python quit unexpectedly" crash dialog on macOS after the image has been written to disk. The image is valid — the crash happens during post-generation telemetry/cleanup. SD3 Medium (3.5GB peak) does not exhibit this behavior.

**Workaround:** Use SD3 Medium for production workloads. SD3.5 Large works but may show the macOS crash dialog to users.

**Root cause:** Likely a memory-related segfault in the MLX/Metal cleanup path when using system Python 3.9. May resolve with a newer Python version or future DiffusionKit update.

---

## Performance (Will Bite at Scale)

### 1. `LatencyStore.get_percentile()` — Unbounded Memory Growth `FIXED`

**File:** `src/fleet_manager/server/latency_store.py`
**Severity:** High

`get_percentile()` loaded ALL historical latency rows into memory every time a latency observation was recorded. For a high-traffic deployment with thousands of observations per `(node, model)` pair, this grew without bound.

**Fix:** Capped to the most recent 500 observations per `(node, model)` pair using a subquery with `ORDER BY timestamp DESC LIMIT 500`. Memory usage is now bounded regardless of history size.

---

### 2. `_refresh_cache()` — N+1 Query Pattern `FIXED`

**File:** `src/fleet_manager/server/latency_store.py`
**Severity:** Medium

On startup, `_refresh_cache()` first queried all distinct `(node_id, model_name)` pairs, then issued a separate `get_percentile()` call for each pair. For a fleet with many node/model combinations, this meant dozens of sequential SQLite round-trips.

**Fix:** Replaced with a single SQL query using `ROW_NUMBER()` and `PERCENT_RANK()` window functions to compute all p75 values at once. Also caps to the latest 500 observations per pair. Startup is now one query regardless of fleet size.

---

### 3. `in_flight` List — O(n) Membership and Removal `FIXED`

**File:** `src/fleet_manager/server/queue_manager.py`
**Severity:** Low–Medium

The `in_flight` field on each queue was a `list`. Both `in` checks and `.remove()` were O(n). Under high concurrency with deep queues, this was a bottleneck.

**Fix:** Changed to `dict[str, QueueEntry]` keyed by `request_id`. All operations (`__contains__`, `pop`, `[]`) are now O(1). The reaper, `mark_completed`, `mark_failed`, and worker all use dict operations.

---

## Code Quality

### 4. `_request_tokens` Dict — Leaking Internal State `FIXED`

**File:** `src/fleet_manager/server/streaming.py`
**Severity:** Low

Route handlers in `openai_compat.py` and `ollama_compat.py` accessed the private `proxy._request_tokens` and `proxy._request_meta` dicts directly via `.pop()`. This broke encapsulation and coupled route logic to internal implementation details.

**Fix:** Added public methods `pop_token_counts(request_id)` and `pop_request_meta(request_id)` on `StreamingProxy`. All route handler access updated to use the public API.

---

### 5. `asyncio.ensure_future` — Deprecated API `FIXED`

**File:** `src/fleet_manager/common/discovery.py` (line ~65)
**Severity:** Low

`asyncio.ensure_future()` has been deprecated since Python 3.10 in favor of `asyncio.create_task()`. The project requires Python 3.11+, so this should be updated.

**Fix:** Replaced `asyncio.ensure_future(...)` with `asyncio.create_task(...)`.

---

### 6. Unused Dependencies and Imports `OPEN`

**Files:** `pyproject.toml`, `src/fleet_manager/server/app.py`
**Severity:** Low

- `sse-starlette` is listed in `pyproject.toml` but never imported in the source code.
- `pyyaml` is listed in `pyproject.toml` but never imported in the source code.
- `StaticFiles` is imported in `app.py` but never used.

**Fix:** Remove unused dependencies from `pyproject.toml` and the dead import from `app.py`.

---

### 7. `HeartbeatPayload.arch` — Hardcoded Default `OPEN`

**File:** `src/fleet_manager/models/` (HeartbeatPayload definition)
**Severity:** Low

The `arch` field defaults to `"apple_silicon"`, which is incorrect for non-Mac nodes (e.g., Linux/x86 or Linux/ARM).

**Fix:** Default to `platform.machine()` or similar runtime detection.

---

### 8. `event_stream()` Re-fetches State Every Tick `OPEN`

**File:** `src/fleet_manager/server/routes/dashboard.py`
**Severity:** Low

The SSE `event_stream()` function re-fetches `request.app.state` on every tick (every 2 seconds). The references should be captured once before the loop starts.

**Fix:** Capture `registry = request.app.state.registry` etc. before entering the `while True` loop.

---

### 9. Dashboard Inline HTML/CSS/JS — Growing Maintenance Burden `OPEN`

**File:** `src/fleet_manager/server/routes/dashboard.py`
**Severity:** Low (for now)

The dashboard is a large amount of inline HTML/CSS/JS in Python strings across 5 pages (Fleet Overview, Trends, Model Insights, Apps, Benchmarks). This is pragmatic for a single-file deployment but will become painful as more dashboard features are added (e.g., tag filtering on Trends/Models views).

**Fix:** When the dashboard grows further, extract to Jinja2 templates or a separate frontend build.

---

## Test Coverage Gaps

### 10. Untested Modules `PARTIAL`

**Severity:** Medium

The following modules still have zero test coverage:

- `server/rebalancer.py` — pre-warm trigger and queue move logic
- `common/discovery.py` — mDNS advertise and browse
- `common/system_metrics.py` — psutil metric collection
- `common/ollama_client.py` — Ollama HTTP client

Previously untested, now covered:
- ~~`node/agent.py`~~ — now has 6 tests in `tests/test_node/test_agent.py`

The rebalancer in particular has meaningful logic (deciding when to move pending requests, triggering pre-warm) that warrants unit tests.

---

### 11. `test_move_pending` — Tautological Assertion `OPEN`

**File:** `tests/test_server/test_queue_manager.py`
**Severity:** Low

The test asserts `moved >= 0`, which is always true for a non-negative integer. This assertion provides no verification that entries were actually moved.

**Fix:** Assert `moved >= 1` or verify the target queue received the expected entries.

---

### 12. `test_shutdown` — Vacuous Test `OPEN`

**File:** `tests/test_server/test_queue_manager.py`
**Severity:** Low

The test body is `pass  # No assertion needed`. It only verifies that no exception is raised, which provides minimal confidence.

**Fix:** Assert post-shutdown state — e.g., that worker tasks are cancelled, queues are empty, or new enqueues are rejected.

---

## Known Limitations

### 13. Meeting Detector False Positives on Dev Machines

**Severity:** Low

The macOS meeting detector (`node/meeting_detector.py`) detects active camera/microphone as "in meeting" and triggers a hard pause. Developers using webcam-based tools (video calls, streaming, screen sharing) during development will get false positives, causing the node to stop accepting work.

**Workaround:** Set `FLEET_NODE_ENABLE_CAPACITY_LEARNING=false` (the default) to disable meeting detection entirely. Tests use `@patch.object(MeetingDetector, "is_in_meeting", return_value=False)` to work around this.

---

### 14. Capacity Learning 7-Day Bootstrap Period

**Severity:** Low

The capacity learner requires 7 days of real observations to graduate from "bootstrapping" to "learned" mode. During the bootstrap period, the learner contributes less confidence to routing decisions. This cannot be validated in automated tests — it requires a week of real usage.

**Workaround:** Pre-seed the capacity learner JSON file with synthetic data if faster convergence is needed.

---

### 15. Tag Filtering Not Yet on Trends/Models Views

**Severity:** Low (feature gap)

The tagging system records tags on every trace and provides a dedicated Apps dashboard tab. However, the existing Trends and Model Insights views cannot yet be filtered by tag. Adding tag-based filtering to these views is a natural next step.

---

### 16. OLLAMA_NUM_PARALLEL Auto-Calculation Causes KV Cache Bloat and Model Thrashing `PARTIAL`

**Severity:** High

On high-memory machines (e.g., 512GB Mac Studio), Ollama's `auto` setting for `OLLAMA_NUM_PARALLEL` calculates a high slot count (e.g., 16). Each parallel slot pre-allocates KV cache for the full context window. With 16 slots and `default_num_ctx=262144`:

```
KV cache per model = 262144 ctx × 16 parallel = 4,194,304 KvSize → 384 GB
```

A single model consumes ~413 GB (17 GB weights + 384 GB KV cache + 12 GB compute), leaving no room for other models on a 464 GB VRAM machine. When a second model is requested, Ollama evicts the first — and vice versa — creating a thrashing loop that freezes the machine for 10-60 seconds per swap.

**Symptoms:**
- Models drop to 0 loaded at regular intervals (visible in herd dashboard and heartbeat data)
- Ollama logs show `"model requires more gpu memory than is currently available, evicting a model to make space"` repeatedly
- Machine freezes during model swaps (loading 88-151 GB models saturates memory bandwidth)
- `OLLAMA_KEEP_ALIVE=-1` alone does NOT fix this — eviction is space-based, not time-based

**Evidence:** Ollama server logs (`~/.ollama/logs/server-*.log`) showed eviction cascades at hourly intervals coinciding with bot-simulation model rotation. KV cache sizes confirmed via `load request` log entries showing `KvSize:4194304` with `Parallel:16`.

**Fix (user-side):** Set `OLLAMA_NUM_PARALLEL=2` (or 3-4). KV cache drops to ~20 GB per model, allowing 3-4 large models to coexist.

```bash
launchctl setenv OLLAMA_NUM_PARALLEL 2
# Restart Ollama
```

**Herd-side detection (implemented):** The health engine's `_check_kv_cache_bloat()` detects this by comparing VRAM used vs expected weight sizes. When overhead exceeds 50%, it reports CRITICAL severity with cross-platform fix instructions (macOS launchctl, Linux systemd, Windows env var). The model thrashing check (`_check_model_thrashing()`) catches the downstream symptom — frequent cold loads from eviction cascades. Both checks surface in the dashboard Health tab and `/dashboard/api/health` API.

**Remaining:** Could inject `num_ctx` overrides in proxied requests to cap context windows, but this risks changing model behavior. Current approach (detect + recommend) is safer.

---

### 21. Dynamic `num_ctx` Management Based on Actual Usage `PARTIAL`

**Severity:** Medium
**Files:** New module + `server/streaming.py`, `server/routes/dashboard.py` (settings)

Ollama allocates KV cache for the full `default_num_ctx` per model, even if most requests only use a fraction of it. A model with 131K default context uses ~67GB, but if 95% of requests only need 8K-16K context, the fleet is wasting 50+GB of memory per model on unused KV cache. This prevents loading additional models.

**Proposed approach — 3 phases:**

**Phase 1: Observe** — Track actual `num_ctx` usage per model from request traces.
- Log `prompt_eval_count` (prompt tokens) from every completed request
- Compute p50, p95, p99 of actual prompt sizes per model
- Surface in dashboard settings: "gpt-oss:120b: avg context 2K, p95 8K, p99 16K, allocated 131K"
- No behavior change — just visibility

**Phase 2: Recommend** — Use observed data to suggest optimal `num_ctx` per model.
- Dashboard shows: "Recommended: set num_ctx=32768 for gpt-oss:120b (covers p99 of your usage, saves ~50GB)"
- Health engine warns when allocated context >> actual usage by 4x+
- Settings page has a slider or input per model to set recommended `num_ctx`

**Phase 3: Auto-adjust** — Dynamically manage `num_ctx` via Ollama settings.
- Herd injects `num_ctx` in proxied requests based on learned optimal value
- If a request arrives that exceeds the current setting, Herd either:
  - a) Queues it and triggers an Ollama restart with higher `num_ctx` (slow but correct)
  - b) Passes it through with an explicit higher `num_ctx` (triggers model reload in Ollama)
  - c) Returns a warning header and serves at the current context limit
- Auto-restart Ollama if error rate spikes due to context truncation
- Settings toggle: `FLEET_DYNAMIC_NUM_CTX=true` (off by default)
- Settings page shows current vs recommended vs actual usage with toggle

**Key data already available:**
- `request_traces.prompt_tokens` in SQLite — has actual prompt sizes for every request
- Health engine already detects KV cache bloat (`_check_kv_cache_bloat()`)
- Context protection (`streaming.py`) already intercepts `num_ctx` in requests
- Dashboard settings page already has runtime toggles

**Why this matters:** On the 512GB Mac Studio, gpt-oss:120b with 131K context uses ~67GB. If actual usage is 16K context, it could use ~12GB — freeing 55GB for 2-3 additional models. This directly fixes the smart benchmark's inability to load multiple models.

---

### 17. Zombie In-Flight Queue Entries Block Concurrency Slots `FIXED`

**File:** `src/fleet_manager/server/queue_manager.py`
**Severity:** High

The queue worker adds entries to `in_flight` then hands an async generator to the route handler via a Future. If the client disconnects mid-stream or the generator is never fully consumed, `mark_completed`/`mark_failed` in the `_tracked_stream` finally block never executes. The entry stays in `in_flight` forever, permanently consuming a concurrency slot.

In production, 5 of 8 slots became zombied, causing the router to accept new connections but never process them (0 bytes returned after 2 minutes).

**Fix:** Added a background reaper task that runs every 60s and removes any in-flight entries older than 15 minutes (past the 10-minute Ollama read timeout). Reaped entries are marked as failed. The reaper starts automatically via `queue_mgr.start_reaper()` during app lifespan.

---

### 18. mDNS `NonUniqueNameException` Prevents Router Restart `FIXED`

**File:** `src/fleet_manager/common/discovery.py`
**Severity:** High

When the router crashes or is killed without clean shutdown, the zeroconf mDNS service registration persists in the network. On restart, `async_register_service()` raises `NonUniqueNameException` because the stale service name is still registered by the OS, causing the router to fail to start entirely.

**Fix:** Wrapped registration in try/except. On `NonUniqueNameException`, close the zeroconf instance, create a fresh one, and re-register with `allow_name_change=True`. This handles both stale registrations and concurrent instances gracefully.

---

### 19. Duplicate Queues from Unnormalized Model Names `FIXED`

**File:** `src/fleet_manager/models/request.py`
**Severity:** Medium

Ollama returns model names with explicit tags (e.g., `qwen3-coder:latest`) but client requests often omit the tag (e.g., `qwen3-coder`). This caused duplicate queues (`node:qwen3-coder` and `node:qwen3-coder:latest`), scoring mismatches, latency cache misses, and broken pre-warm tracking. Dashboard showed two separate queue cards for the same model with split stats (20 done vs 4520 done).

**Fix:** Added a Pydantic `model_validator` on `InferenceRequest` that appends `:latest` to model names (and fallback_models) that lack a tag. Normalization happens at construction time so all downstream code sees consistent names.

---

### 20. Client `num_ctx` Triggers Full Model Reload and Hang in Ollama `FIXED`

**File:** `src/fleet_manager/server/streaming.py`
**Severity:** Critical

When a client sends `num_ctx` in request options that differs from the loaded model's context window, Ollama's scheduler calls `needsReload()` and triggers a full model unload+reload. For large models (89GB `gpt-oss:120b`), this causes multi-minute hangs or complete deadlocks — 0 bytes returned. Reproduced: `num_ctx: 4096` on a model loaded at 32768 hangs indefinitely; without `num_ctx` works in 3 seconds. Confirmed directly against Ollama (bypassing Herd) — Ollama itself hangs.

Root causes compound: GPT-OSS minimum context override (Ollama bumps `num_ctx < 8192` to 8192), runner startup timeout exceeded during 89GB reload, and KV cache fill loop on small context values.

**Fix:** Added context-size protection (`FLEET_CONTEXT_PROTECTION=strip` by default) in `_build_ollama_body()`. Strips `num_ctx` when ≤ loaded context (prevents needless reload). When `num_ctx` > loaded context, searches fleet for a loaded model with sufficient context and more parameters, and auto-switches. Logged for operator visibility.

---

### 21. Stream Error Messages Are Empty Strings `FIXED`

**File:** `src/fleet_manager/server/streaming.py`
**Severity:** Medium

Failed request traces in the trace store have empty `error_message` fields. The `logger.error()` calls in `_stream_with_tracking` and `_stream_with_retry` format the exception with `{e}` but the exception objects sometimes stringify to empty strings (e.g., `httpx.RemoteProtocolError` with no message). This makes post-mortem debugging blind — you can see a request failed but not why.

**Fix:** Changed all `str(e)` to `f"{type(e).__name__}: {e}"` in stream error paths. Now error messages always include the exception class (e.g., `RemoteProtocolError:` instead of empty string). Applied in both `_stream_with_tracking` and `_stream_with_retry`.

---

### 22. Client Disconnects Recorded as "completed" `FIXED`

**File:** `src/fleet_manager/server/streaming.py`
**Severity:** High

When a client disconnects mid-stream (HTTP timeout, connection drop), FastAPI sends `GeneratorExit` to the streaming generator. Both `_stream_with_tracking` and `_stream_with_retry` caught this but marked the request as **completed** — silently hiding failures from the dashboard and trace store.

**Observed:** 2026-04-01. Another agent reported "4 fetch failed — Ollama connection drops on large payloads" but the dashboard showed only 1 failed request out of 24,650. The disconnect failures were all recorded as successful completions.

**Fix:** `GeneratorExit` now records status `"client_disconnected"` and calls `mark_failed` instead of `mark_completed`. The trace store gets the real status so the dashboard accurately reflects failure rates.

---

### 23. Incomplete Streams (No done:true) Recorded as "completed" `FIXED`

**File:** `src/fleet_manager/server/streaming.py`
**Severity:** High

If Ollama drops the TCP connection after sending partial data but without raising an exception, httpx's `aiter_lines()` stops iterating cleanly. The `finally` block saw `error_occurred = False` and marked it "completed" — even though the response was truncated and Ollama never sent the final `done: true` chunk.

**Fix:** After the stream loop completes without error, check if `_request_tokens` has an entry for this request (only populated when `done: true` is parsed in `stream_from_node`). If missing, record as `"incomplete"` and call `mark_failed`. This catches Ollama process deaths, OOM kills, and silent connection drops.

---

## Future Considerations

- **Extract dashboard frontend** — see issue #9 above
- **`event_stream()` optimization** — see issue #8 above
- **Tag filtering on Trends/Models** — see issue #15 above
- **`collector.py` catch-all** — silently returns empty metrics when Ollama is unreachable, which could mask bugs during development. Consider logging at `WARNING` level.

### #21 — Empty error messages on timeout failures `FIXED`
**File:** `server/streaming.py`
**Severity:** Low
**Problem:** httpx timeout exceptions have empty `str(e)`, so `f"{type(e).__name__}: {e}"` produces `ReadTimeout: ` with no details.
**Fix:** Use `repr(e)` as fallback when `str(e)` is empty: `f"{type(e).__name__}: {repr(e)}"`. Now captures the exception args (timeout value, URL, etc.) even when the string representation is empty.

---

### 22. Custom Date Range Selector for Dashboard Pages `FIXED`

**Files:** `src/fleet_manager/server/routes/dashboard.py`, `src/fleet_manager/server/trace_store.py`
**Severity:** Low (feature enhancement)

The Trends page has preset time buttons (24h, 48h, 72h, 7d) but no custom date/time range selector. The Model Insights and Apps pages have a `days` parameter but no time range UI at all.

**Proposed fix:**

1. **Shared date range component** — reusable across Trends, Model Insights, and Apps pages:
   - Preset buttons: 24h, 48h, 72h, 7d, 30d
   - Custom range: two datetime-local inputs (start, end)
   - All times in user's local timezone (JS `Date` handles this natively)
   - Component stores selection in URL params for shareability

2. **Backend changes:**
   - Add `start_ts` and `end_ts` query params to `/dashboard/api/trends`, `/dashboard/api/models`, `/dashboard/api/apps`
   - TraceStore queries already filter by timestamp — just expose the params
   - Timezone conversion: frontend sends UTC timestamps, backend uses them directly (traces are stored as Unix timestamps)

3. **Pages to update:**
   - Trends: replace current time buttons with shared component
   - Model Insights: add time range component (currently hardcoded to `days` param)
   - Apps: add time range component (currently hardcoded to `days` param)

---

## Model Management

### No priority/pinned model concept — restarts can evict primary models `FIXED`

**Severity:** High
**Discovered:** 2026-04-16 — during vision embedding testing, repeated fleet restarts (`pkill -9`) caused `gpt-oss:120b` (89GB, primary reasoning model) to be unloaded. VRAM fallback then routed requests to `gemma3:27b` (42GB), which loaded and consumed the memory `gpt-oss:120b` needed. Result: primary model evicted, replaced by a less capable one.

**Root cause:** No concept of "this model must always be loaded." VRAM fallback picks whatever is loaded without considering model importance. Ollama's `OLLAMA_KEEP_ALIVE=-1` keeps models loaded but can't prevent eviction when memory is consumed by other models loading first after a restart.

**Proposed fix:**
1. Add `FLEET_PINNED_MODELS` config — comma-separated list of models that must always be loaded (e.g., `gpt-oss:120b,nomic-embed-text`)
2. After node restart, load pinned models first before accepting other requests
3. VRAM fallback should never route to a non-pinned model if a pinned model exists for that category
4. Health check: WARNING if a pinned model is not loaded
5. Dashboard Settings: UI to manage pinned models

**Files:** `server/streaming.py` (VRAM fallback), `node/agent.py` (startup model loading), `models/config.py` (pinned models config), `server/health_engine.py` (health check)

### CoreML provider triggers macOS TCC dialog that freezes the node agent `FIXED`

**Severity:** Critical
**Discovered:** 2026-04-19 — after adding the vision embedding service, the node agent began freezing overnight. User reported a macOS permission dialog appearing asking Python for access. The dialog blocks the Python process indefinitely, causing heartbeats to stop, router marks node offline, all inference fails until someone dismisses the dialog.

**Pattern:** Consistent 120-130 errors/hour from midnight through 8 AM. Not random drops — a steady multi-hour outage until the user returns to the machine and dismisses the dialog. Happened twice in 5 days (April 14 and April 19).

**Root cause:** `CoreMLExecutionProvider` in `ONNXBackend.__init__` was enabled automatically on macOS. On first inference, CoreML compiles the ONNX model for the Neural Engine, which can trigger macOS TCC permission dialogs (Neural Engine access, Desktop folder access if cache scans adjacent paths). Once the dialog appears, the subprocess and the entire Python process block waiting for user interaction.

**Fix (commit d61d3cb+):** Default to CPU-only inference. CPU is fast enough on M-series chips (~60ms/image for DINOv2). Users can opt-in to CoreML via `FLEET_EMBEDDING_USE_COREML=true` with a warning in the logs.

**Files:** `src/fleet_manager/node/embedding_models.py`

---

### Queue concurrency ignores OLLAMA_NUM_PARALLEL — allows 8 in-flight but Ollama only runs 2 `OPEN`

**Severity:** Medium
**Discovered:** 2026-04-16 — dashboard always shows "1/8 in-flight" regardless of model or node. On a 512GB machine the concurrency formula always hits the `_MAX_CONCURRENCY=8` cap because headroom is massive (436GB / 2GB per slot = 218, clamped to 8).

**Root cause:** `compute_concurrency()` in `queue_manager.py` calculates slots from memory headroom divided by estimated KV cache cost (2GB), then clamps to `[1, 8]`. It has no knowledge of `OLLAMA_NUM_PARALLEL`, which controls how many requests Ollama actually processes simultaneously. With `OLLAMA_NUM_PARALLEL=2`, the queue allows 8 in-flight but Ollama queues anything beyond 2 internally, adding unnecessary latency.

**Impact:** On a 512GB machine with `OLLAMA_NUM_PARALLEL=2`:
- Queue reports 8 concurrency slots
- Ollama processes 2 at a time
- 6 requests sit in Ollama's internal queue, invisible to Herd's scoring
- Scoring engine thinks the node has capacity when it's actually backed up
- Wait time estimates are wrong

**Proposed fix:**
1. Node agent reads `OLLAMA_NUM_PARALLEL` from environment or Ollama's config and reports it in the heartbeat
2. `compute_concurrency()` uses `min(memory_slots, ollama_num_parallel)` instead of just memory slots
3. If `OLLAMA_NUM_PARALLEL` is not reported, fall back to current memory-based calculation
4. Dashboard shows actual concurrency (e.g., "1/2" not "1/8")

**Files:** `server/queue_manager.py` (compute_concurrency), `node/collector.py` (read OLLAMA_NUM_PARALLEL), `models/node.py` (add to heartbeat)

**Files:** `server/streaming.py` (VRAM fallback), `node/agent.py` (startup model loading), `models/config.py` (pinned models config), `server/health_engine.py` (health check)

---

### Ollama 3-model concurrent-load cap unconfigurable on macOS (upstream) `OPEN`

**Severity:** Medium
**Discovered:** 2026-04-22 — during Claude Code + ollama-herd setup on a 512GB M3 Ultra Mac Studio
**Upstream:** [ollama/ollama#7041](https://github.com/ollama/ollama/issues/7041), [#4855](https://github.com/ollama/ollama/issues/4855), [#5722](https://github.com/ollama/ollama/issues/5722), [#14953](https://github.com/ollama/ollama/issues/14953)

**Symptom:** Ollama 0.20.4 on macOS refuses to keep more than 3 models concurrently hot in VRAM, regardless of `OLLAMA_MAX_LOADED_MODELS` configuration. Loading a 4th model always evicts one of the existing three (LRU). Silently causes herd's VRAM fallback to fire and degrades Claude Code tool-use quality when mapped models get evicted.

**Root cause (partial):** From Ollama source (`envconfig/config.go`):

```go
MaxRunners = Uint("OLLAMA_MAX_LOADED_MODELS", 0)
```

- `Uint` parses the env value as **unsigned integer**
- `-1` cannot be parsed as unsigned → silently falls through to default `0`
- `0` resolves to `defaultModelsPerGPU = 3` in the scheduler

So setting `OLLAMA_MAX_LOADED_MODELS=-1` (a common "I want unlimited" pattern that propagated via our shell init files) was silently invalid. **But setting a positive integer doesn't fix it either** — see test table below.

**Test evidence (all on Ollama 0.20.4 / macOS 15.x / M3 Ultra 512GB):**

| Attempt | Process env (`ps eww`) | Cap behavior |
|---|---|---|
| `launchctl setenv OLLAMA_MAX_LOADED_MODELS 10` (confirmed via `launchctl getenv`) | shows `-1` | still 3 |
| Plist `EnvironmentVariables.OLLAMA_MAX_LOADED_MODELS=10` at `~/Library/LaunchAgents/homebrew.mxcl.ollama.plist` | regenerated by brew on restart, stripped | still 3 |
| `~/.zshrc` `export OLLAMA_MAX_LOADED_MODELS=10` | only affects new shells, not GUI Mac App | still 3 |
| Direct CLI: `OLLAMA_MAX_LOADED_MODELS=10 /Applications/Ollama.app/Contents/Resources/ollama serve` | process still shows `-1` | still 3 |
| Full kill (Mac App + all runners) + fixed launchctl + clean relaunch via `open -a Ollama` | process still shows `-1` | still 3 |
| Load 4 **distinct** models (different weight blobs) to rule out shared-blob conflict | — | 4th evicts LRU; memory not the constraint |

**Memory was never the issue.** During the 4-distinct-model test: 358 GB of RAM available, 149 GB hot, Ollama still refused the 4th load. The dashboard shows ~292 GB used / 512 GB — plenty of headroom.

**Impact on ollama-herd:**

- **VRAM fallback fires silently** — when a mapped model in `FLEET_ANTHROPIC_MODEL_MAP` isn't hot, requests fall back to nearest available model. Debugged via `x-fleet-fallback` response header. For Claude Code specifically, this means tool-heavy requests hit weaker models (e.g. gemma3:4b) that can't emit `tool_calls` cleanly, breaking the agent loop.
- **Typical Claude Code fleet needs 4+ hot models** — 1 for haiku, 1 for sonnet, 1 for opus (or pull 480B), 1 for vision, plus user's non-Claude-Code scripts. Currently forced to cap at 3 and accept reload cost on the rest.
- **Captures of this failure mode are invisible** until you check the trace DB — no health check currently surfaces it.

**Proposed workarounds:**

1. **Accept the 3-cap** (current behavior) — pick the 3 most critical models per node, accept reload cost on others
2. **Second Ollama instance on another port** — each daemon has its own 3-slot budget; register both as separate nodes or route-target in herd. Doubles capacity to 6 hot.
3. **`mlx-lm.server` for specific heavyweight models** — bypass Ollama entirely; no cap, plus potential 2× decode speed and native prefix caching. Moderate engineering.
4. **Wait for upstream fix** — the pattern of `Uint` + silent `-1` failure is reported across multiple open issues; unclear if anyone is working on it.

**Proposed fix in herd (already planned):** see `docs/plans/hot-fleet-health-checks.md` — adds six health checks that would surface this failure mode within one heartbeat interval instead of requiring trace DB archaeology. Specifically check #3 (`ollama_max_loaded_models_observed`) infers the effective cap from observed behavior rather than reading the unreliable env var.

**Files (herd-side mitigation only):** `server/health_engine.py`, `node/collector.py` (optional: report observed cap in heartbeat payload)
