# TraceStore + LatencyStore: dedicated read connections + periodic WAL checkpoint

**Status**: Planning → Implementing
**Date**: 2026-05-16
**Targets**: 0.6.2 (before publish)
**Related**: `docs/observations.md` 2026-05-15, `docs/issues.md` § "TraceStore write-storm under WAL contention"

## Problem

0.6.2 shipped four layered fixes for the 2026-05-10 trace_store write-storm: longer busy_timeout, retry-on-locked, aggressive autocheckpoint, and a new health check. **Verified post-restart**: trace writes resumed and the WAL drained from 2.5 GB → 0.

**But 11 hours into the soak the failure pattern recurred at a lower amplitude.** Between 2026-05-16 06:48 UTC and 19:00 UTC, ~4,470 background `record_trace` tasks failed with `database is locked` (~370/hour, sustained). Health check fires WARNING as designed; retry loop absorbs some but not all; WAL grew from 0 → 103 MB; main DB mtime stuck at the last successful checkpoint 12 hours earlier.

**Why retries + busy_timeout + autocheckpoint aren't enough:** the root cause isn't transient contention — it's structural.

`TraceStore` opens a single `aiosqlite.Connection`. Both `record_trace` (write) and the dashboard's analytics methods (`get_recent_traces`, `get_overall_stats_24h`, `get_stream_reliability_24h`, etc.) use that same connection. Two things follow:

1. **Operations serialize through one thread.** aiosqlite runs every connection on a single background thread. A 200ms dashboard query blocks all queued writes on that connection for 200ms.
2. **The reader's transaction pins the WAL checkpoint barrier.** Even with WAL mode (where readers don't *block* writers in the traditional sense), an open reader transaction sits at a snapshot point — the checkpointer can only advance to the oldest reader's snapshot. With dashboard polling every 15-30s across multiple endpoints, snapshots are continuously open, and checkpoints can never catch up.

Result: WAL grows monotonically; eventually a write attempt finds the lock held past 30s + 3 retries → trace lost.

`LatencyStore` has the same access pattern (reads from scoring decisions, writes from `record_latency` calls) on `latency.db`. Same DB file, same WAL, same contention.

## Solution

Two changes in one release:

### Part C — Dedicated read connection per store

Each store opens **two** `aiosqlite` connections to its DB file:

- `_db` (write connection): used by `record_trace` / `record_latency` / migrations / table creation.
- `_read_db` (read connection): used by every analytics / dashboard / scoring method.

The two connections execute concurrently in separate threads. Read snapshots are held only by the read connection, so the write connection's view of the WAL can advance independently. Checkpoints triggered from `_db` can complete past the read connection's snapshot once the read connection finishes its current query — which, for short analytics queries, happens within tens of milliseconds.

PRAGMAs on both connections at init:

```
PRAGMA journal_mode=WAL      -- already set; needs to be set on each connection
PRAGMA busy_timeout=30000    -- already set; needs to be set on each connection
PRAGMA wal_autocheckpoint=100  -- already set; only meaningful on writer
```

Read connection additionally sets `PRAGMA query_only=1` as a defense-in-depth — any accidental write attempt on `_read_db` errors out immediately rather than silently working against the writer's expectations.

### Part A — Periodic explicit `wal_checkpoint(PASSIVE)`

A background asyncio task in `app.py` lifespan calls `PRAGMA wal_checkpoint(PASSIVE)` on each store's `_db` every 10 seconds.

Why 10 seconds:

- Faster than the dashboard's 15-30s poll cadence, so a checkpoint typically lands in the gap between two reads.
- `PASSIVE` mode is non-blocking — it advances whatever it can and returns immediately without waiting for readers. Safe to run on a tight cadence.
- The checkpoint return value `(busy, log_pages_total, log_pages_checkpointed)` is captured to a logger so we can see whether checkpoints are advancing (good) or constantly blocked (signal that the read-connection split didn't help and we need Part B).

This is defense-in-depth on top of `wal_autocheckpoint=100`:

| Mechanism | Trigger | Behavior |
|---|---|---|
| `wal_autocheckpoint=100` | After every 100 WAL page writes | Passive; tied to write volume |
| Explicit periodic checkpoint (new) | Every 10s wall-clock | Passive; tied to time |

Together: checkpoints fire on whichever trigger comes first.

### What's intentionally NOT in this plan

- **Part B — splitting `trace_store` onto its own `traces.db`** is a deeper refactor (DB path change, migration of ~340K historical rows, every caller's expectations about file paths). We're deferring it pending evidence from the C+A soak. If C+A alone resolves the contention (the mechanism analysis predicts it will), B becomes "nice to have" rather than urgent. We'd rather ship one fast change with real data behind it than a bigger one on speculation.

## Implementation

### Files changed

1. **`src/fleet_manager/server/trace_store.py`**
   - Add `_read_db: aiosqlite.Connection | None = None` field.
   - In `initialize()`: after the writer connection is set up, open a second `aiosqlite.connect(str(self._db_path))` for reads, apply `PRAGMA journal_mode=WAL`, `PRAGMA busy_timeout=30000`, `PRAGMA query_only=1`.
   - Every read method (`get_recent_traces`, `get_trace_by_request_id`, `get_usage_by_node_model_day`, `get_cold_loads_24h`, `get_error_rates_24h`, `get_retry_stats_24h`, `get_overall_stats_24h`, `get_stream_reliability_24h`, `get_model_timeouts_24h`, `get_prompt_token_stats`, `get_model_priority_scores`, `get_request_count_by_model`, `get_recent_benchmark_runs`, `get_benchmark_run`, `get_fleet_briefings`) switches from `self._db.execute` → `self._read_db.execute`.
   - In `close()`: close `_read_db` after `_db`.
   - Add `async def checkpoint_passive() -> tuple[int, int, int]` method that runs `PRAGMA wal_checkpoint(PASSIVE)` on `_db` and returns `(busy, log_pages, checkpointed)` for logging.

2. **`src/fleet_manager/server/latency_store.py`**
   - Same shape — add `_read_db`, route reads (`get_p75`, the cache-refresh queries, etc.), add `checkpoint_passive()`.

3. **`src/fleet_manager/server/app.py`**
   - In `lifespan()`: after stores initialize, start a new background task `_checkpoint_task` that loops every 10s, calling `await trace_store.checkpoint_passive()` and `await latency_store.checkpoint_passive()`.
   - Log the checkpoint result at DEBUG (every tick) and at INFO when a non-zero `busy` value is returned (signal of contention).
   - In shutdown: cancel and await `_checkpoint_task` like the other lifespan tasks.

4. **Tests** in `tests/test_server/test_trace_store_resilience.py`:
   - `test_read_methods_use_read_connection` — patch both connections, call a read method, assert only the read connection's `execute` was called.
   - `test_write_methods_use_write_connection` — converse.
   - `test_read_connection_is_query_only` — attempt an INSERT on `_read_db`, expect `OperationalError`.
   - `test_checkpoint_passive_runs` — call the new method, assert the PRAGMA was issued and a tuple was returned.

### Verification plan

1. Run full test suite (expect 979 + 4 new = 983 pass).
2. Run ruff. Clean.
3. Restart local fleet from dev source.
4. Drive 30 minutes of mixed traffic (chat completions + dashboard polling).
5. Check:
   - `grep -c '"level": "ERROR"' ~/.fleet-manager/logs/herd.jsonl` should stay at 0.
   - `ls -lh ~/.fleet-manager/latency.db-wal` should stay under ~10 MB (vs 103 MB pre-fix).
   - `sqlite3 latency.db "SELECT datetime(timestamp,'unixepoch','localtime') FROM request_traces ORDER BY timestamp DESC LIMIT 1"` should match wall-clock within seconds.
   - Dashboard `reqs_24h` increments in real time as traffic flows.
   - Health check `trace_store_write_failures` stays silent.
6. If clean: commit and the change ships as part of 0.6.2 before PyPI publish.
7. If failures still appear: keep the change (it's still an improvement), proceed to Part B as a follow-up.

### Rollback

If something breaks:

- Revert the commit. Both stores fall back to single-connection mode. The retry-loop + 30s busy_timeout from earlier 0.6.2 work still applies, so the failure mode regresses to "slow trickle" rather than "complete outage."

## Risks

| Risk | Mitigation |
|---|---|
| Read connection picks up changes more slowly than write connection (snapshot lag) | Documented in this plan; analytics tolerate eventual consistency. Audit confirms no "read-your-write" paths in the dashboard or scoring code. |
| Missing one read method during the routing — silent contention on the write connection | Tests verify routing for the most-called methods; any missed method causes the same observable symptom we already have, so it'll be caught fast in soak. |
| `aiosqlite.connect` failures during init now have to handle two connections | Close `_db` on `_read_db` failure to avoid leaks. |
| Periodic checkpoint task crashes silently | Wrap loop body in try/except, log + continue. Failing forever should be loud — bubble up via a new check_id if it ever happens. |

## Expected outcome

Failure rate drops to zero under sustained traffic + dashboard polling. WAL stays under ~5 MB during normal operation (autocheckpoint at 100 pages × ~50 bytes per page = ~5 KB minimum; with explicit 10s checkpoints it'll oscillate between near-zero and a few hundred KB). Health check stays silent. Dashboard `reqs_24h` tracks wall-clock traffic without lag.

If after a 30-minute live soak the failure rate is non-zero, Part B (split `trace_store` to `traces.db`) is the next step.
