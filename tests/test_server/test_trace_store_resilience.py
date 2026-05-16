"""Tests for TraceStore lock-retry resilience + the trace_store_write_failures health check.

These tests cover the 2026-05-15 regression: WAL contention under a stuck
read caused ~40K background trace-record tasks to fail with ``database is
locked`` over ~4 days while the operator's dashboard quietly showed
``reqs_24h=0``.  The fixes verified here:

  1. ``TraceStore.record_trace`` retries on locked errors (3 backoffs).
  2. After all retries are exhausted the failure is counted in
     ``_write_failure_times`` for the health check to read.
  3. ``HealthEngine`` emits a ``trace_store_write_failures`` recommendation
     when the count is non-zero in the last 5 minutes (WARNING at 1+,
     CRITICAL at 50+).
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest
import pytest_asyncio

from fleet_manager.server.health_engine import HealthEngine, Severity
from fleet_manager.server.trace_store import TraceStore


@pytest_asyncio.fixture
async def store(tmp_path):
    s = TraceStore(data_dir=str(tmp_path))
    await s.initialize()
    yield s
    await s.close()


# -- TraceStore retry-on-locked --


@pytest.mark.asyncio
async def test_record_trace_retries_then_succeeds(store: TraceStore, monkeypatch):
    """A transient lock should be retried and the trace eventually written."""
    real_execute = store._db.execute
    call_count = {"n": 0}

    async def flaky_execute(sql, params=None):
        if "INSERT INTO request_traces" in sql:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise aiosqlite.OperationalError("database is locked")
        # Other calls (commits, queries, init pragmas) pass through.
        if params is None:
            return await real_execute(sql)
        return await real_execute(sql, params)

    monkeypatch.setattr(store._db, "execute", flaky_execute)

    # Speed up the retry sleep so the test stays fast.
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    await store.record_trace(
        request_id="r1",
        model="m",
        original_model="m",
        node_id="n",
    )

    assert call_count["n"] == 2  # 1 failure, 1 success
    # No failure should be counted — the retry succeeded.
    assert store.get_write_failure_count() == 0


@pytest.mark.asyncio
async def test_record_trace_records_failure_after_all_retries(
    store: TraceStore, monkeypatch
):
    """Persistent lock → all retries exhausted → counter increments + raises."""
    async def always_locked(sql, params=None):
        if "INSERT INTO request_traces" in sql:
            raise aiosqlite.OperationalError("database is locked")
        return MagicMock()

    monkeypatch.setattr(store._db, "execute", always_locked)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    with pytest.raises(aiosqlite.OperationalError):
        await store.record_trace(
            request_id="r2",
            model="m",
            original_model="m",
            node_id="n",
        )

    assert store.get_write_failure_count() == 1


@pytest.mark.asyncio
async def test_record_trace_non_lock_error_not_retried(
    store: TraceStore, monkeypatch
):
    """A non-lock OperationalError (e.g. constraint violation) should propagate immediately."""
    call_count = {"n": 0}

    async def constraint_violation(sql, params=None):
        if "INSERT INTO request_traces" in sql:
            call_count["n"] += 1
            raise aiosqlite.OperationalError("UNIQUE constraint failed")
        return MagicMock()

    monkeypatch.setattr(store._db, "execute", constraint_violation)
    sleep_mock = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep_mock)

    with pytest.raises(aiosqlite.OperationalError):
        await store.record_trace(
            request_id="r3", model="m", original_model="m", node_id="n",
        )

    assert call_count["n"] == 1  # tried once, gave up
    sleep_mock.assert_not_called()  # no retry backoff
    # Failure counter still records this — operator should know even if
    # the cause isn't a lock.
    assert store.get_write_failure_count() == 1


# -- Failure-count windowing --


def test_get_write_failure_count_window():
    """Only failures within ``window_s`` should count.

    Note the deque is pruned destructively on each call (entries older than
    the window are dropped), so we test each window on a fresh store
    rather than asserting both on the same one — see ``test_get_write_failure_count_prunes_old``
    for the prune semantics.
    """
    now = time.time()

    s1 = TraceStore()
    s1._write_failure_times.extend(
        [now - 700, now - 650, now - 600, now - 200, now - 30]
    )
    # 5-min window → only the two most recent
    assert s1.get_write_failure_count(window_s=300) == 2

    s2 = TraceStore()
    s2._write_failure_times.extend(
        [now - 700, now - 650, now - 600, now - 200, now - 30]
    )
    # 1-hour window → all five
    assert s2.get_write_failure_count(window_s=3600) == 5


def test_get_write_failure_count_prunes_old():
    """Calling the accessor twice should prune old entries from the deque."""
    s = TraceStore()
    now = time.time()
    s._write_failure_times.extend([now - 1000, now - 500, now - 100])
    s.get_write_failure_count(window_s=300)  # prunes the two older
    assert len(s._write_failure_times) == 1


# -- Health engine check --


def _make_engine() -> HealthEngine:
    """Build a HealthEngine without invoking the full constructor path."""
    eng = HealthEngine.__new__(HealthEngine)
    return eng


def test_check_trace_store_write_failures_silent_when_clean():
    """Zero failures → no recommendation."""
    eng = _make_engine()
    store = TraceStore()  # empty deque
    recs = eng._check_trace_store_write_failures(store)
    assert recs == []


def test_check_trace_store_write_failures_warning_at_low_count():
    """1+ failures in window → WARNING."""
    eng = _make_engine()
    store = TraceStore()
    store._write_failure_times.append(time.time())
    recs = eng._check_trace_store_write_failures(store)
    assert len(recs) == 1
    assert recs[0].check_id == "trace_store_write_failures"
    assert recs[0].severity == Severity.WARNING
    assert recs[0].data["failures_5m"] == 1


def test_check_trace_store_write_failures_critical_at_50():
    """≥50 failures in 5 min → CRITICAL (matches 2026-05-10 incident scale)."""
    eng = _make_engine()
    store = TraceStore()
    now = time.time()
    for _ in range(60):
        store._write_failure_times.append(now)
    recs = eng._check_trace_store_write_failures(store)
    assert recs[0].severity == Severity.CRITICAL
    assert recs[0].data["failures_5m"] == 60


def test_check_trace_store_write_failures_handles_no_store():
    """Defensive: ``None`` trace_store → no recommendation, no crash."""
    eng = _make_engine()
    assert eng._check_trace_store_write_failures(None) == []


def test_check_trace_store_write_failures_handles_pre_062_store():
    """A trace_store without ``get_write_failure_count`` (pre-0.6.2) is silently skipped."""
    eng = _make_engine()
    legacy = object()  # no methods at all
    assert eng._check_trace_store_write_failures(legacy) == []


# -- Part C: dedicated read connection (TraceStore) --
#
# Adding a second aiosqlite connection lets dashboard analytics run
# concurrently with writes and prevents read snapshots from pinning the
# WAL checkpoint barrier on the writer's view.  See
# docs/plans/trace-store-read-connection-and-checkpoint.md.


@pytest.mark.asyncio
async def test_initialize_opens_separate_read_connection(tmp_path):
    """initialize() should set up both _db and _read_db as distinct connections."""
    s = TraceStore(data_dir=str(tmp_path))
    await s.initialize()
    try:
        assert s._db is not None
        assert s._read_db is not None
        assert s._db is not s._read_db
    finally:
        await s.close()


@pytest.mark.asyncio
async def test_read_methods_use_read_connection(store: TraceStore, monkeypatch):
    """get_recent_traces() and friends should query the read connection only."""
    read_calls = {"n": 0}
    write_calls = {"n": 0}
    real_read_execute = store._read_db.execute
    real_write_execute = store._db.execute

    async def counted_read(sql, *args, **kwargs):
        if sql.strip().upper().startswith("SELECT"):
            read_calls["n"] += 1
        return await real_read_execute(sql, *args, **kwargs)

    async def counted_write(sql, *args, **kwargs):
        if sql.strip().upper().startswith("SELECT"):
            write_calls["n"] += 1
        return await real_write_execute(sql, *args, **kwargs)

    monkeypatch.setattr(store._read_db, "execute", counted_read)
    monkeypatch.setattr(store._db, "execute", counted_write)

    await store.get_recent_traces(limit=10)
    await store.get_overall_stats_24h()

    assert read_calls["n"] >= 2  # at least one SELECT per method
    assert write_calls["n"] == 0  # writer connection saw no SELECTs


@pytest.mark.asyncio
async def test_write_methods_use_write_connection(store: TraceStore, monkeypatch):
    """record_trace() should hit the writer connection only."""
    read_calls = {"n": 0}
    write_calls = {"n": 0}
    real_read_execute = store._read_db.execute
    real_write_execute = store._db.execute

    async def counted_read(sql, *args, **kwargs):
        if sql.strip().upper().startswith("INSERT"):
            read_calls["n"] += 1
        return await real_read_execute(sql, *args, **kwargs)

    async def counted_write(sql, *args, **kwargs):
        if sql.strip().upper().startswith("INSERT"):
            write_calls["n"] += 1
        return await real_write_execute(sql, *args, **kwargs)

    monkeypatch.setattr(store._read_db, "execute", counted_read)
    monkeypatch.setattr(store._db, "execute", counted_write)

    await store.record_trace(
        request_id="r", model="m", original_model="m", node_id="n",
    )

    assert write_calls["n"] == 1  # one INSERT on the writer
    assert read_calls["n"] == 0  # read connection saw no INSERTs


@pytest.mark.asyncio
async def test_read_connection_is_query_only(store: TraceStore):
    """PRAGMA query_only=1 should reject writes through the read connection."""
    with pytest.raises(aiosqlite.OperationalError):
        await store._read_db.execute(
            "INSERT INTO request_traces "
            "(request_id, model, original_model, node_id, status, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("x", "m", "m", "n", "completed", time.time()),
        )
        await store._read_db.commit()


# -- Part A: periodic wal_checkpoint(PASSIVE) --


@pytest.mark.asyncio
async def test_checkpoint_passive_returns_tuple(store: TraceStore):
    """checkpoint_passive() returns the SQLite (busy, log_pages, checkpointed) tuple."""
    # Drive a write so there's something to checkpoint
    await store.record_trace(
        request_id="r", model="m", original_model="m", node_id="n",
    )
    result = await store.checkpoint_passive()
    assert result is not None
    busy, log_pages, checkpointed = result
    assert isinstance(busy, int)
    assert isinstance(log_pages, int)
    assert isinstance(checkpointed, int)


@pytest.mark.asyncio
async def test_checkpoint_passive_none_when_closed(tmp_path):
    """checkpoint_passive() returns None when the writer connection is closed."""
    s = TraceStore(data_dir=str(tmp_path))
    await s.initialize()
    await s.close()
    assert await s.checkpoint_passive() is None


@pytest.mark.asyncio
async def test_checkpoint_passive_swallows_errors(store: TraceStore, monkeypatch):
    """A failure in PRAGMA wal_checkpoint must never raise — background-task safety."""
    async def boom(sql, *args, **kwargs):
        raise aiosqlite.OperationalError("simulated checkpoint failure")
    monkeypatch.setattr(store._db, "execute", boom)
    # Should return None, not raise.
    assert await store.checkpoint_passive() is None
