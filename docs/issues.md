# Known Issues & Improvements

Identified via code review of the full codebase. Organized by priority.

---

## Performance (Will Bite at Scale)

### 1. `LatencyStore.get_percentile()` — Unbounded Memory Growth

**File:** `src/fleet_manager/server/latency_store.py`
**Severity:** High

`get_percentile()` loads ALL historical latency rows into memory every time a latency observation is recorded. For a high-traffic deployment with thousands of observations per `(node, model)` pair, this grows without bound.

**Fix:** Cap to the last N observations (e.g., 100–1000) using a `LIMIT` clause, or use SQLite's `percent_rank()` window function to compute percentiles in SQL.

---

### 2. `_refresh_cache()` — N+1 Query Pattern

**File:** `src/fleet_manager/server/latency_store.py`
**Severity:** Medium

On startup, `_refresh_cache()` first queries all distinct `(node_id, model_name)` pairs, then issues a separate `get_percentile()` call for each pair. For a fleet with many node/model combinations, this means dozens of sequential SQLite round-trips.

**Fix:** Replace with a single query that computes all percentiles at once, or fetch all observations in one `SELECT` and compute percentiles in Python.

---

### 3. `in_flight` List — O(n) Membership and Removal

**File:** `src/fleet_manager/server/queue_manager.py` (lines ~117–129)
**Severity:** Low–Medium

The `in_flight` field on each queue is a `list`. Both `in` checks and `.remove()` are O(n). Under high concurrency with deep queues, this becomes a bottleneck.

**Fix:** Use a `set` or `dict` keyed by `request_id` for O(1) operations.

---

## Code Quality

### 4. `_request_tokens` Dict — Leaking Internal State

**File:** `src/fleet_manager/server/streaming.py`
**Severity:** Low

Route handlers in `openai_compat.py` and `ollama_compat.py` access the private `proxy._request_tokens` dict directly via `.pop()`. This breaks encapsulation and couples route logic to an internal implementation detail.

**Fix:** Expose a public method like `pop_token_counts(request_id)` on `StreamingProxy`.

---

### 5. `asyncio.ensure_future` — Deprecated API

**File:** `src/fleet_manager/common/discovery.py` (line ~65)
**Severity:** Low

`asyncio.ensure_future()` has been deprecated since Python 3.10 in favor of `asyncio.create_task()`. The project requires Python 3.11+, so this should be updated.

**Fix:** Replace `asyncio.ensure_future(...)` with `asyncio.create_task(...)`.

---

### 6. Unused Dependencies and Imports

**Files:** `pyproject.toml`, `src/fleet_manager/server/app.py`
**Severity:** Low

- `sse-starlette` is listed in `pyproject.toml` but never imported in the source code.
- `pyyaml` is listed in `pyproject.toml` but never imported in the source code.
- `StaticFiles` is imported in `app.py` but never used.

**Fix:** Remove unused dependencies from `pyproject.toml` and the dead import from `app.py`.

---

### 7. `HeartbeatPayload.arch` — Hardcoded Default

**File:** `src/fleet_manager/models/` (HeartbeatPayload definition)
**Severity:** Low

The `arch` field defaults to `"apple_silicon"`, which is incorrect for non-Mac nodes (e.g., Linux/x86 or Linux/ARM).

**Fix:** Default to `platform.machine()` or similar runtime detection.

---

## Test Coverage Gaps

### 8. Untested Modules

**Severity:** Medium

The following modules have zero test coverage:

- `server/rebalancer.py` — pre-warm trigger and queue move logic
- `node/agent.py` — mDNS discovery, heartbeat loop, signal handling
- `common/discovery.py` — mDNS advertise and browse
- `common/system_metrics.py` — psutil metric collection
- `common/ollama_client.py` — Ollama HTTP client

The rebalancer in particular has meaningful logic (deciding when to move pending requests, triggering pre-warm) that warrants unit tests.

---

### 9. `test_move_pending` — Tautological Assertion

**File:** `tests/test_server/test_queue_manager.py`
**Severity:** Low

The test asserts `moved >= 0`, which is always true for a non-negative integer. This assertion provides no verification that entries were actually moved.

**Fix:** Assert `moved >= 1` or verify the target queue received the expected entries.

---

### 10. `test_shutdown` — Vacuous Test

**File:** `tests/test_server/test_queue_manager.py`
**Severity:** Low

The test body is `pass  # No assertion needed`. It only verifies that no exception is raised, which provides minimal confidence.

**Fix:** Assert post-shutdown state — e.g., that worker tasks are cancelled, queues are empty, or new enqueues are rejected.

---

## Future Considerations

- **Dashboard size** — `routes/dashboard.py` is 1,076 lines with inline HTML/CSS/JS. As the UI grows, extract templates or move to a separate frontend.
- **`event_stream()` re-fetches `request.app.state` on every tick** — capture references once before the loop.
- **`collector.py` catch-all** — silently returns empty metrics when Ollama is unreachable, which could mask bugs during development. Consider logging at `WARNING` level.
