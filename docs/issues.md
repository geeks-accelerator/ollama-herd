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

### 1. `LatencyStore.get_percentile()` — Unbounded Memory Growth `OPEN`

**File:** `src/fleet_manager/server/latency_store.py`
**Severity:** High

`get_percentile()` loads ALL historical latency rows into memory every time a latency observation is recorded. For a high-traffic deployment with thousands of observations per `(node, model)` pair, this grows without bound.

**Fix:** Cap to the last N observations (e.g., 100–1000) using a `LIMIT` clause, or use SQLite's `percent_rank()` window function to compute percentiles in SQL.

---

### 2. `_refresh_cache()` — N+1 Query Pattern `OPEN`

**File:** `src/fleet_manager/server/latency_store.py`
**Severity:** Medium

On startup, `_refresh_cache()` first queries all distinct `(node_id, model_name)` pairs, then issues a separate `get_percentile()` call for each pair. For a fleet with many node/model combinations, this means dozens of sequential SQLite round-trips.

**Fix:** Replace with a single query that computes all percentiles at once, or fetch all observations in one `SELECT` and compute percentiles in Python.

---

### 3. `in_flight` List — O(n) Membership and Removal `OPEN`

**File:** `src/fleet_manager/server/queue_manager.py` (lines ~117–129)
**Severity:** Low–Medium

The `in_flight` field on each queue is a `list`. Both `in` checks and `.remove()` are O(n). Under high concurrency with deep queues, this becomes a bottleneck.

**Fix:** Use a `set` or `dict` keyed by `request_id` for O(1) operations.

---

## Code Quality

### 4. `_request_tokens` Dict — Leaking Internal State `OPEN`

**File:** `src/fleet_manager/server/streaming.py`
**Severity:** Low

Route handlers in `openai_compat.py` and `ollama_compat.py` access the private `proxy._request_tokens` dict directly via `.pop()`. This breaks encapsulation and couples route logic to an internal implementation detail.

**Fix:** Expose a public method like `pop_token_counts(request_id)` on `StreamingProxy`.

---

### 5. `asyncio.ensure_future` — Deprecated API `OPEN`

**File:** `src/fleet_manager/common/discovery.py` (line ~65)
**Severity:** Low

`asyncio.ensure_future()` has been deprecated since Python 3.10 in favor of `asyncio.create_task()`. The project requires Python 3.11+, so this should be updated.

**Fix:** Replace `asyncio.ensure_future(...)` with `asyncio.create_task(...)`.

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

### 16. OLLAMA_NUM_PARALLEL Auto-Calculation Causes KV Cache Bloat and Model Thrashing `OPEN`

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

**Potential fix (Herd-side):** The router could detect this condition by comparing a node's reported VRAM against loaded model count and warn in the dashboard. Or Herd could inject `num_ctx` overrides in proxied requests to cap context windows and prevent KV cache explosion.

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
