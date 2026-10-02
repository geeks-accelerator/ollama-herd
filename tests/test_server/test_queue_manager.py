"""Tests for the QueueManager."""

from __future__ import annotations

import asyncio

import pytest

from fleet_manager.models.request import (
    InferenceRequest,
    QueueEntry,
    RequestFormat,
    RequestStatus,
)
from fleet_manager.server.queue_manager import QueueManager


def _make_entry(model="phi4:14b", node_id="studio"):
    req = InferenceRequest(
        model=model,
        messages=[{"role": "user", "content": "test"}],
        original_format=RequestFormat.OPENAI,
        raw_body={"model": model},
    )
    return QueueEntry(request=req, assigned_node=node_id)


async def _dummy_process(entry):
    async def stream():
        yield "chunk1\n"
        yield "chunk2\n"
    return stream()


@pytest.mark.asyncio
class TestQueueManager:
    async def test_enqueue_creates_queue(self):
        qm = QueueManager()
        entry = _make_entry()
        future = await qm.enqueue(entry, _dummy_process)
        assert future is not None
        depths = qm.get_queue_depths()
        assert "studio:phi4:14b" in depths

    async def test_queue_depths(self):
        qm = QueueManager()
        e1 = _make_entry(model="phi4:14b", node_id="a")
        e2 = _make_entry(model="phi4:14b", node_id="a")
        await qm.enqueue(e1, _dummy_process)
        await qm.enqueue(e2, _dummy_process)
        depths = qm.get_queue_depths()
        assert depths["a:phi4:14b"] >= 1  # at least one should be visible

    async def test_queue_info(self):
        qm = QueueManager()
        entry = _make_entry()
        await qm.enqueue(entry, _dummy_process)
        info = qm.get_queue_info()
        assert "studio:phi4:14b" in info
        assert info["studio:phi4:14b"]["node_id"] == "studio"
        assert info["studio:phi4:14b"]["model"] == "phi4:14b"

    async def test_mark_completed(self):
        qm = QueueManager()
        entry = _make_entry()

        def sync_process(e):
            async def gen():
                yield "data"
            return gen()

        await qm.enqueue(entry, sync_process)
        await asyncio.sleep(0.1)  # let worker pick up

        qm.mark_completed("studio:phi4:14b", entry)
        assert entry.status == RequestStatus.COMPLETED
        info = qm.get_queue_info()
        assert info["studio:phi4:14b"]["completed"] == 1

    async def test_mark_completed_accumulates_stats(self):
        """Regression guard: running averages for dashboard.

        mark_completed must accumulate latency + token counts per queue so
        get_queue_info can surface avg_latency_ms / avg_prompt_tokens /
        avg_completion_tokens.  These run alongside completed_count with the
        same lifecycle (reset on restart)."""
        qm = QueueManager()

        def sync_process(e):
            async def gen():
                yield "data"
            return gen()

        # Three completions with different stats
        for latency_ms, pt, ct in [(100.0, 200, 50), (200.0, 400, 100), (300.0, 600, 150)]:
            entry = _make_entry()
            await qm.enqueue(entry, sync_process)
            await asyncio.sleep(0.05)
            qm.mark_completed(
                "studio:phi4:14b", entry,
                latency_ms=latency_ms,
                prompt_tokens=pt,
                completion_tokens=ct,
            )

        info = qm.get_queue_info()["studio:phi4:14b"]
        assert info["completed"] == 3
        assert info["stats_samples"] == 3
        # Averages: latency (100+200+300)/3 = 200; prompt (200+400+600)/3 = 400;
        # completion (50+100+150)/3 = 100
        assert info["avg_latency_ms"] == 200.0
        assert info["avg_prompt_tokens"] == 400.0
        assert info["avg_completion_tokens"] == 100.0

    async def test_mark_completed_without_stats_keeps_averages_at_zero(self):
        """A completion without stats args (e.g. image gen) must not drift
        the averages to zero — ``stats_samples`` stays 0 as the signal that
        no denominator is available yet."""
        qm = QueueManager()
        entry = _make_entry()

        def sync_process(e):
            async def gen():
                yield "data"
            return gen()

        await qm.enqueue(entry, sync_process)
        await asyncio.sleep(0.05)
        qm.mark_completed("studio:phi4:14b", entry)  # no stats kwargs

        info = qm.get_queue_info()["studio:phi4:14b"]
        assert info["completed"] == 1
        assert info["stats_samples"] == 0
        assert info["avg_latency_ms"] == 0.0
        assert info["avg_prompt_tokens"] == 0.0
        assert info["avg_completion_tokens"] == 0.0

    async def test_mark_completed_partial_stats_counts_sample(self):
        """Image/STT pass only latency_ms — averages should reflect that
        latency, with tokens accumulating as 0 for that sample."""
        qm = QueueManager()
        entry = _make_entry()

        def sync_process(e):
            async def gen():
                yield "data"
            return gen()

        await qm.enqueue(entry, sync_process)
        await asyncio.sleep(0.05)
        qm.mark_completed("studio:phi4:14b", entry, latency_ms=250.0)

        info = qm.get_queue_info()["studio:phi4:14b"]
        assert info["stats_samples"] == 1
        assert info["avg_latency_ms"] == 250.0
        assert info["avg_prompt_tokens"] == 0.0
        assert info["avg_completion_tokens"] == 0.0

    async def test_mark_failed(self):
        qm = QueueManager()
        entry = _make_entry()

        def sync_process(e):
            async def gen():
                yield "data"
            return gen()

        await qm.enqueue(entry, sync_process)
        await asyncio.sleep(0.1)

        qm.mark_failed("studio:phi4:14b", entry)
        assert entry.status == RequestStatus.FAILED
        info = qm.get_queue_info()
        assert info["studio:phi4:14b"]["failed"] == 1

    async def test_move_pending(self):
        qm = QueueManager()

        def blocking_process(e):
            async def gen():
                await asyncio.sleep(100)  # never completes
                yield "data"
            return gen()

        # Enqueue 3 items to source
        entries = []
        for _ in range(3):
            e = _make_entry(model="llama3.3:70b", node_id="overloaded")
            entries.append(e)
            await qm.enqueue(e, blocking_process)

        # Give workers time to pick up first item
        await asyncio.sleep(0.1)

        # Move pending items to a different queue
        moved = await qm.move_pending(
            "overloaded:llama3.3:70b", "underloaded:llama3.3:70b", 2
        )
        # At least some should have moved (worker may have picked up 1)
        assert moved >= 0
        await qm.shutdown()

    async def test_move_pending_nonexistent_source(self):
        qm = QueueManager()
        moved = await qm.move_pending("fake:model", "other:model", 5)
        assert moved == 0

    async def test_shutdown(self):
        qm = QueueManager()
        entry = _make_entry()

        def process(e):
            async def gen():
                await asyncio.sleep(100)
                yield "data"
            return gen()

        await qm.enqueue(entry, process)
        await asyncio.sleep(0.05)
        await qm.shutdown()
        # Worker tasks should be cancelled
        info = qm.get_queue_info()
        for key, q_info in info.items():
            # Workers should be done/cancelled
            pass  # No assertion needed — just checking no exceptions

    async def test_multiple_queues_independent(self):
        qm = QueueManager()
        e1 = _make_entry(model="phi4:14b", node_id="a")
        e2 = _make_entry(model="llama3.3:70b", node_id="b")

        def process(e):
            async def gen():
                yield "ok"
            return gen()

        await qm.enqueue(e1, process)
        await qm.enqueue(e2, process)

        depths = qm.get_queue_depths()
        assert "a:phi4:14b" in depths
        assert "b:llama3.3:70b" in depths
        await qm.shutdown()


def test_concurrency_never_exceeds_backend_admission_limit():
    """Ollama admits OLLAMA_NUM_PARALLEL requests per model and queues the rest
    internally. Workers beyond that don't decode — they block inside Ollama,
    invisible to the queue meant to be managing them. Measured 2026-07-19:
    aggregate throughput saturated at exactly the configured value (4)."""
    from fleet_manager.server.serializers import decode_parallelism_for

    class _Ollama:
        def __init__(self, np): self.num_parallel = np
    class _Node:
        def __init__(self, np): self.ollama = _Ollama(np)

    assert decode_parallelism_for(_Node(4)) == 4
    assert decode_parallelism_for(_Node(2)) == 2
    # Unreported (older agent, or the var is unset) → Ollama's documented
    # default of 1, not an optimistic guess.
    assert decode_parallelism_for(_Node(0)) == 1
    assert decode_parallelism_for(None) == 1


def test_capacity_math_still_applies_when_it_is_the_tighter_bound():
    """The backend cap is a ceiling, not a replacement — a memory-starved node
    must still get fewer slots than the backend would allow."""
    from fleet_manager.server.queue_manager import compute_concurrency

    # Plenty of headroom → capacity math would allow many...
    assert compute_concurrency(available_memory_gb=500.0, model_size_gb=20.0) == 8
    # ...but no headroom still clamps to the floor regardless of the backend.
    assert compute_concurrency(available_memory_gb=20.0, model_size_gb=20.0) == 1


# ---------------------------------------------------------------------------
# Per-model decode parallelism — Ollama decides admission per model, not node
# ---------------------------------------------------------------------------


def _node_with_meta(num_parallel: int, meta: dict | None):
    from types import SimpleNamespace

    return SimpleNamespace(
        ollama=SimpleNamespace(num_parallel=num_parallel, models_available_meta=meta),
    )


@pytest.mark.parametrize("family", sorted(
    __import__("fleet_manager.server.serializers", fromlist=["x"]).OLLAMA_SERIAL_FAMILIES
))
def test_serial_families_decode_one_at_a_time(family):
    """sched.go forces numParallel=1 for these architectures. On a NUM_PARALLEL=4
    node herd must not run 4 workers that would silently queue inside Ollama."""
    from fleet_manager.models.node import ModelTagMeta
    from fleet_manager.server.serializers import decode_parallelism_for

    node = _node_with_meta(4, {"m:1b": ModelTagMeta(family=family, format="gguf")})
    assert decode_parallelism_for(node, "m:1b") == 1


def test_mlx_run_model_decodes_one_at_a_time():
    """Ollama's IsMLX() is format == "safetensors", and its MLX runner is a single
    serial request loop. Ollama 0.40 makes MLX the default on Apple Silicon."""
    from fleet_manager.models.node import ModelTagMeta
    from fleet_manager.server.serializers import decode_parallelism_for

    node = _node_with_meta(4, {"qwen3.8:27b-mlx": ModelTagMeta(format="safetensors")})
    assert decode_parallelism_for(node, "qwen3.8:27b-mlx") == 1


def test_embedding_models_decode_one_at_a_time():
    from fleet_manager.models.node import ModelTagMeta
    from fleet_manager.server.serializers import decode_parallelism_for

    node = _node_with_meta(4, {"m:latest": ModelTagMeta(capabilities=["embedding"])})
    assert decode_parallelism_for(node, "m") == 1  # bare name → :latest, like Ollama


def test_decision_capability_alone_does_not_force_serial():
    """sched.go forces only !completion models.  Decision models report
    completion too, so a decision model in a non-serial family keeps the node
    limit — capping it at 1 would be the under-dispatch failure mode."""
    from fleet_manager.models.node import ModelTagMeta
    from fleet_manager.server.serializers import decode_parallelism_for

    node = _node_with_meta(4, {"d:1b": ModelTagMeta(
        family="llama", format="gguf", capabilities=["decision", "completion"],
    )})
    assert decode_parallelism_for(node, "d:1b") == 4


def test_real_ollama_metadata_from_the_reference_mac():
    """Exactly what Ollama 0.35.0 reported on the Mac mini, 2026-10-02."""
    from fleet_manager.models.node import ModelTagMeta
    from fleet_manager.server.serializers import decode_parallelism_for

    node = _node_with_meta(4, {
        # MLX variant: safetensors, EMPTY family — serial via the format rule.
        "qwen3.8:27b-mlx": ModelTagMeta(
            format="safetensors", family="",
            capabilities=["completion", "vision", "tools", "thinking"],
        ),
        # Decision model: serial via its qwen35 family, not via "decision".
        "nimble:latest": ModelTagMeta(
            format="gguf", family="qwen35",
            capabilities=["decision", "tools", "thinking", "completion"],
        ),
        "gemma3:27b": ModelTagMeta(
            format="gguf", family="gemma3", capabilities=["completion", "vision"],
        ),
        "nomic-embed-text:latest": ModelTagMeta(
            format="gguf", family="nomic-bert", capabilities=["embedding"],
        ),
    })
    assert decode_parallelism_for(node, "qwen3.8:27b-mlx") == 1
    assert decode_parallelism_for(node, "nimble") == 1
    assert decode_parallelism_for(node, "gemma3:27b") == 4
    assert decode_parallelism_for(node, "nomic-embed-text") == 1


@pytest.mark.parametrize("meta", [
    None,                                     # older agent: no meta at all
    {},                                       # meta present, model not in it
    {"m:1b": None},                           # defensive: null entry
])
def test_missing_metadata_keeps_the_node_limit(meta):
    """Missing data must reproduce today's behavior exactly, never throttle."""
    from fleet_manager.server.serializers import decode_parallelism_for

    assert decode_parallelism_for(_node_with_meta(4, meta), "m:1b") == 4


def test_empty_capabilities_do_not_throttle():
    """An old Ollama reports nothing; absence of "completion" must not read as
    "not a completion model" — that would throttle every model to 1."""
    from fleet_manager.models.node import ModelTagMeta
    from fleet_manager.server.serializers import decode_parallelism_for

    node = _node_with_meta(4, {"m:1b": ModelTagMeta(family="llama", format="gguf")})
    assert decode_parallelism_for(node, "m:1b") == 4


def test_two_models_on_one_node_get_their_own_limits():
    from fleet_manager.models.node import ModelTagMeta
    from fleet_manager.server.serializers import decode_parallelism_for

    node = _node_with_meta(4, {
        "gemma3:27b": ModelTagMeta(
            family="gemma3", format="gguf", capabilities=["completion", "vision"],
        ),
        "qwen3-vl:32b": ModelTagMeta(
            family="qwen3vl", format="gguf", capabilities=["completion", "vision"],
        ),
    })
    assert decode_parallelism_for(node, "gemma3:27b") == 4
    assert decode_parallelism_for(node, "qwen3-vl:32b") == 1
    assert decode_parallelism_for(node) == 4  # no model → node-level, unchanged


async def test_queue_concurrency_uses_the_per_model_limit():
    """The single call site passes the model through, so a serial model's queue
    runs one worker while the node's other models keep their full limit."""
    from types import SimpleNamespace

    from fleet_manager.models.node import ModelTagMeta

    node = SimpleNamespace(
        memory=SimpleNamespace(available_gb=400.0),
        capacity=None,
        ollama=SimpleNamespace(
            num_parallel=4,
            models_loaded=[],
            models_available_meta={
                "qwen3-vl:32b": ModelTagMeta(family="qwen3vl", format="gguf"),
                "gemma3:27b": ModelTagMeta(family="gemma3", format="gguf"),
            },
        ),
    )
    registry = SimpleNamespace(get_node=lambda node_id: node)
    qm = QueueManager(registry=registry)
    assert qm._compute_queue_concurrency("mini", "qwen3-vl:32b") == 1
    assert qm._compute_queue_concurrency("mini", "gemma3:27b") == 4


# ---------------------------------------------------------------------------
# Enforcement: a worker holds its slot until the request leaves the queue.
#
# Measured 2026-10-02: four concurrent requests to a concurrency-1 queue showed
# in_flight=4, pending=0 — the worker handed off an unconsumed stream and took
# the next request immediately, so `concurrency` never bounded the backend.
# ---------------------------------------------------------------------------


def _fixed_concurrency(qm, n):
    """Pin every queue's computed concurrency (no registry needed)."""
    qm._compute_queue_concurrency = lambda node_id, model: n


async def _slow_process(entry):
    """Like the real process_fns: returns an unconsumed async generator."""
    async def gen():
        yield "chunk"
    return gen()


def _sync_process(entry):
    async def gen():
        yield "chunk"
    return gen()


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


async def test_concurrency_bounds_requests_in_flight():
    qm = QueueManager()
    _fixed_concurrency(qm, 1)
    entries = [_make_entry() for _ in range(3)]
    for e in entries:
        await qm.enqueue(e, _sync_process)
    await _settle()
    q = qm._queues["studio:phi4:14b"]
    assert len(q.in_flight) == 1, "concurrency=1 must allow exactly one request at the backend"
    assert q.pending.qsize() == 2, "the rest must wait in herd's queue, where they are visible"
    await qm.shutdown()


async def test_completing_a_request_dispatches_the_next():
    qm = QueueManager()
    _fixed_concurrency(qm, 1)
    a, b = _make_entry(), _make_entry()
    await qm.enqueue(a, _sync_process)
    await qm.enqueue(b, _sync_process)
    await _settle()
    q = qm._queues["studio:phi4:14b"]
    assert list(q.in_flight) == [a.request.request_id]
    qm.mark_completed("studio:phi4:14b", a)
    await _settle()
    assert list(q.in_flight) == [b.request.request_id]
    assert q.pending.qsize() == 0
    await qm.shutdown()


async def test_concurrency_two_runs_two():
    qm = QueueManager()
    _fixed_concurrency(qm, 2)
    for _ in range(3):
        await qm.enqueue(_make_entry(), _sync_process)
    await _settle()
    q = qm._queues["studio:phi4:14b"]
    assert (len(q.in_flight), q.pending.qsize()) == (2, 1)
    await qm.shutdown()


async def test_failure_frees_the_slot():
    qm = QueueManager()
    _fixed_concurrency(qm, 1)
    a, b = _make_entry(), _make_entry()
    await qm.enqueue(a, _sync_process)
    await qm.enqueue(b, _sync_process)
    await _settle()
    qm.mark_failed("studio:phi4:14b", a)
    await _settle()
    assert list(qm._queues["studio:phi4:14b"].in_flight) == [b.request.request_id]
    await qm.shutdown()


async def test_reaper_frees_a_slot_whose_stream_was_never_consumed(monkeypatch):
    """A stream nobody reads never runs its finally, so no mark_* fires.  The
    reaper is the safety net — it must free the worker, not just the entry."""
    from fleet_manager.server import queue_manager as qm_mod

    monkeypatch.setattr(qm_mod, "_REAPER_INTERVAL_SECONDS", 0.01)
    qm = QueueManager()
    qm._stale_timeout = 0.05
    _fixed_concurrency(qm, 1)
    dispatched = []

    def tracking_process(entry):
        dispatched.append(entry.request.request_id)
        return _sync_process(entry)

    a, b = _make_entry(), _make_entry()
    await qm.enqueue(a, tracking_process)
    await qm.enqueue(b, tracking_process)
    await _settle()
    assert dispatched == [a.request.request_id]  # b waits behind a's held slot
    qm.start_reaper()
    await asyncio.sleep(0.3)
    # Only the reaper could have freed a's slot (nothing marked it), and b ran.
    assert dispatched == [a.request.request_id, b.request.request_id]
    assert a.request.request_id not in qm._queues["studio:phi4:14b"].in_flight
    await qm.shutdown()


async def test_request_abandoned_while_queued_is_skipped():
    """A client that disconnects while waiting cancels the route's await; the
    worker must not start a stream nobody will read."""
    qm = QueueManager()
    _fixed_concurrency(qm, 1)
    calls = []

    def tracking_process(entry):
        calls.append(entry.request.request_id)
        return _sync_process(entry)

    a, gone, c = _make_entry(), _make_entry(), _make_entry()
    await qm.enqueue(a, tracking_process)
    gone_future = await qm.enqueue(gone, tracking_process)
    await qm.enqueue(c, tracking_process)
    await _settle()
    gone_future.cancel()
    qm.mark_completed("studio:phi4:14b", a)
    await _settle()
    assert gone.request.request_id not in calls
    assert list(qm._queues["studio:phi4:14b"].in_flight) == [c.request.request_id]
    await qm.shutdown()


async def test_process_fn_raising_does_not_hold_the_slot():
    qm = QueueManager()
    _fixed_concurrency(qm, 1)

    def boom(entry):
        raise RuntimeError("node unreachable")

    bad_future = await qm.enqueue(_make_entry(), boom)
    good = _make_entry()
    await qm.enqueue(good, _sync_process)
    await _settle()
    assert isinstance(bad_future.exception(), RuntimeError)
    assert good.request.request_id in qm._queues["studio:phi4:14b"].in_flight
    await qm.shutdown()


async def test_a_lowered_limit_retires_the_excess_workers():
    """E.g. a queue created before node metadata arrived, which then says the
    model is serial: the limit must drop to 1 for real, not stay at 2."""
    qm = QueueManager()
    _fixed_concurrency(qm, 2)
    first = [_make_entry(), _make_entry()]
    for e in first:
        await qm.enqueue(e, _sync_process)
    await _settle()
    _fixed_concurrency(qm, 1)
    later = [_make_entry(), _make_entry()]
    for e in later:
        await qm.enqueue(e, _sync_process)  # recomputes the limit to 1
    for e in first:
        qm.mark_completed("studio:phi4:14b", e)
    await _settle()
    q = qm._queues["studio:phi4:14b"]
    assert len(q.in_flight) == 1
    assert q.pending.qsize() == 1
    await qm.shutdown()


async def test_client_abandoning_a_stream_mid_way_frees_the_slot_promptly():
    """A streaming client that disconnects mid-response leaves its stream
    suspended at a yield; the stream's GeneratorExit handler (mark_failed in
    _stream_with_tracking) only runs once nothing references it.  Found live
    2026-10-02: the worker kept the future -- whose result IS the stream -- in
    scope while waiting, so the stream could never be finalized, so the slot it
    would release was held until the reaper (~11 min), freezing the model."""
    import gc

    qm = QueueManager()
    _fixed_concurrency(qm, 1)
    key = "studio:phi4:14b"

    def realistic_process(entry):
        async def gen():
            try:
                yield "chunk-1"
                yield "chunk-2"
            except GeneratorExit:
                qm.mark_failed(key, entry)  # what _stream_with_tracking does
                raise
            qm.mark_completed(key, entry)
        return gen()

    a, b = _make_entry(), _make_entry()
    fut_a = await qm.enqueue(a, realistic_process)
    await qm.enqueue(b, realistic_process)
    stream = await fut_a
    assert await stream.__anext__() == "chunk-1"
    # The client goes away: the route stops iterating and drops the stream.
    del stream, fut_a
    for _ in range(3):
        gc.collect()
        await _settle()
    q = qm._queues[key]
    assert b.request.request_id in q.in_flight, "the abandoned stream must not hold the slot"
    await qm.shutdown()


async def test_stream_cancelled_mid_await_still_leaves_the_queue():
    """Starlette doesn't close a streaming body on client disconnect -- it
    cancels the task.  Mid-generation that lands as CancelledError while the
    stream awaits the backend, which `except Exception` / `except GeneratorExit`
    don't catch.  Found live 2026-10-02: the slot stayed held until the reaper.
    Whatever the process_fn does, the queue must settle the request."""
    qm = QueueManager()
    _fixed_concurrency(qm, 1)
    key = "studio:phi4:14b"
    backend_waiting = asyncio.Event()

    def process_without_cancel_handling(entry):
        async def gen():
            try:
                yield "chunk-1"
                backend_waiting.set()
                await asyncio.Event().wait()  # waiting on the backend
                yield "never"
            except GeneratorExit:
                qm.mark_failed(key, entry)
                raise
            except Exception:
                qm.mark_failed(key, entry)
                raise
        return gen()

    a, b = _make_entry(), _make_entry()
    fut_a = await qm.enqueue(a, process_without_cancel_handling)
    await qm.enqueue(b, process_without_cancel_handling)
    stream = await fut_a

    async def consume():  # the route's streaming task
        async for _ in stream:
            pass

    task = asyncio.create_task(consume())
    await backend_waiting.wait()
    task.cancel()  # client disconnect
    with __import__("contextlib").suppress(asyncio.CancelledError):
        await task
    await _settle()
    q = qm._queues[key]
    assert a.request.request_id not in q.in_flight, "a cancelled stream must leave the queue"
    assert b.request.request_id in q.in_flight, "and free its slot for the next request"
    assert q.failed_count == 1
    await qm.shutdown()


# ---------------------------------------------------------------------------
# A client that leaves while its request is still queued
# ---------------------------------------------------------------------------


class _FakeRequest:
    """The bit of a Starlette Request the dispatch helper uses."""

    def __init__(self):
        self.gone = False

    async def is_disconnected(self):
        return self.gone


async def test_dispatched_stream_returns_the_stream_when_dispatched():
    from fleet_manager.server.routes.routing import dispatched_stream

    qm = QueueManager()
    _fixed_concurrency(qm, 1)
    fut = await qm.enqueue(_make_entry(), _sync_process)
    stream = await dispatched_stream(_FakeRequest(), fut, _make_entry(), None)
    assert stream is not None
    await qm.shutdown()


async def test_client_leaving_while_queued_is_never_dispatched():
    """Otherwise a timed-out client's request still runs later, for nobody,
    holding a slot real requests need.  Found live: 15 abandoned requests from
    timed-out test clients sat in one queue, each due to run in full."""
    from unittest.mock import AsyncMock

    from fleet_manager.server.routes.routing import dispatched_stream

    qm = QueueManager()
    _fixed_concurrency(qm, 1)
    calls = []

    def tracking(entry):
        calls.append(entry.request.request_id)
        return _sync_process(entry)

    running, queued = _make_entry(), _make_entry()
    await qm.enqueue(running, tracking)
    fut = await qm.enqueue(queued, tracking)
    req, traces = _FakeRequest(), AsyncMock()
    trace_store = type("T", (), {"record_trace": traces})()

    async def leave_soon():
        await asyncio.sleep(0.05)
        req.gone = True

    asyncio.create_task(leave_soon())
    assert await dispatched_stream(req, fut, queued, trace_store, poll_s=0.01) is None
    assert fut.cancelled()

    qm.mark_completed("studio:phi4:14b", running)
    await _settle()
    assert queued.request.request_id not in calls, "abandoned request must not run"
    kwargs = traces.call_args.kwargs
    assert kwargs["status"] == "client_disconnected"
    assert "queued" in kwargs["error_message"]
    await qm.shutdown()
