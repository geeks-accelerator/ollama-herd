"""Tests for the QueueManager."""

from __future__ import annotations

import asyncio
import time

import pytest

from fleet_manager.models.request import (
    InferenceRequest,
    QueueEntry,
    RequestFormat,
    RequestStatus,
)
from fleet_manager.server import queue_manager
from fleet_manager.server.queue_manager import _LOAD_WAIT_SECONDS as _LOAD_WAIT
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
                "studio:phi4:14b",
                entry,
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

        src_key, dst_key = "overloaded:llama3.3:70b", "underloaded:llama3.3:70b"
        pending_before = qm._queues[src_key].pending.qsize()

        moved = await qm.move_pending(src_key, dst_key, 2)

        # `assert moved >= 0` was the original assertion and is vacuous for a
        # non-negative int.  It was written that way because how many entries a
        # worker has already claimed is genuinely nondeterministic -- so assert
        # the invariants that hold regardless of that race, not a fixed count.
        assert moved <= 2, "must respect the count cap"
        assert moved <= pending_before, "cannot move more than was pending"
        # Conservation: every entry that left the source arrived at the target.
        assert qm._queues[src_key].pending.qsize() == pending_before - moved
        assert qm._queues[dst_key].pending.qsize() == moved
        # The target queue is created on demand, with the node parsed from its key.
        assert dst_key in qm._queues
        assert qm._queues[dst_key].node_id == "underloaded"
        assert qm._queues[dst_key].model == "llama3.3:70b"
        # And the point of moving: each entry is now assigned to the new node,
        # which is what the router acts on.  Checked by draining, so a move that
        # copied the entry without reassigning it would fail here.
        for _ in range(moved):
            entry, _future, _fn = qm._queues[dst_key].pending.get_nowait()
            assert entry.assigned_node == "underloaded"
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

        # Capture the tasks before shutdown -- afterwards is the only time their
        # state is meaningful, and the body used to be `pass  # No assertion
        # needed`, which verified only that nothing raised.
        workers = [t for q in qm._queues.values() for t in q.worker_tasks]
        assert workers, "fixture precondition: at least one worker was started"
        # The reaper is started by an explicit `start_reaper()` (the app lifespan
        # calls it), not by `enqueue`, so start it here deliberately -- shutdown
        # cancels it too, and that is worth covering rather than skipping.
        qm.start_reaper()
        reaper = qm._reaper_task
        assert reaper is not None

        await qm.shutdown()
        # cancel() only *requests* cancellation; the task settles on the next
        # loop pass, so yield before asserting or this races the event loop.
        await asyncio.sleep(0)

        assert all(t.cancelled() or t.done() for t in workers), (
            "every worker task must be cancelled or finished after shutdown"
        )
        assert reaper.cancelled() or reaper.done(), "the reaper must be cancelled too"
        # The entry never completed (its process sleeps 100s), so shutdown must
        # not have counted it as a success.
        for q in qm._queues.values():
            assert q.completed_count == 0

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
        def __init__(self, np):
            self.num_parallel = np

    class _Node:
        def __init__(self, np):
            self.ollama = _Ollama(np)

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


@pytest.mark.parametrize(
    "family",
    sorted(__import__("fleet_manager.server.serializers", fromlist=["x"]).OLLAMA_SERIAL_FAMILIES),
)
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

    node = _node_with_meta(
        4,
        {
            "d:1b": ModelTagMeta(
                family="llama",
                format="gguf",
                capabilities=["decision", "completion"],
            )
        },
    )
    assert decode_parallelism_for(node, "d:1b") == 4


def test_real_ollama_metadata_from_the_reference_mac():
    """Exactly what Ollama 0.35.0 reported on the Mac mini, 2026-10-02."""
    from fleet_manager.models.node import ModelTagMeta
    from fleet_manager.server.serializers import decode_parallelism_for

    node = _node_with_meta(
        4,
        {
            # MLX variant: safetensors, EMPTY family — serial via the format rule.
            "qwen3.8:27b-mlx": ModelTagMeta(
                format="safetensors",
                family="",
                capabilities=["completion", "vision", "tools", "thinking"],
            ),
            # Decision model: serial via its qwen35 family, not via "decision".
            "nimble:latest": ModelTagMeta(
                format="gguf",
                family="qwen35",
                capabilities=["decision", "tools", "thinking", "completion"],
            ),
            "gemma3:27b": ModelTagMeta(
                format="gguf",
                family="gemma3",
                capabilities=["completion", "vision"],
            ),
            "nomic-embed-text:latest": ModelTagMeta(
                format="gguf",
                family="nomic-bert",
                capabilities=["embedding"],
            ),
        },
    )
    assert decode_parallelism_for(node, "qwen3.8:27b-mlx") == 1
    assert decode_parallelism_for(node, "nimble") == 1
    assert decode_parallelism_for(node, "gemma3:27b") == 4
    assert decode_parallelism_for(node, "nomic-embed-text") == 1


@pytest.mark.parametrize(
    "meta",
    [
        None,  # older agent: no meta at all
        {},  # meta present, model not in it
        {"m:1b": None},  # defensive: null entry
    ],
)
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

    node = _node_with_meta(
        4,
        {
            "gemma3:27b": ModelTagMeta(
                family="gemma3",
                format="gguf",
                capabilities=["completion", "vision"],
            ),
            "qwen3-vl:32b": ModelTagMeta(
                family="qwen3vl",
                format="gguf",
                capabilities=["completion", "vision"],
            ),
        },
    )
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


def _small_mac(loaded: list[tuple[str, float]], available_gb: float = 8.47):
    """Run 6's 24 GB M4, as its heartbeat reported it with qwen2.5:7b loaded."""
    from types import SimpleNamespace

    node = SimpleNamespace(
        memory=SimpleNamespace(available_gb=available_gb),
        capacity=None,
        ollama=SimpleNamespace(
            num_parallel=4,
            models_loaded=[SimpleNamespace(name=n, size_gb=s) for n, s in loaded],
            models_available_meta={},
        ),
    )
    return QueueManager(registry=SimpleNamespace(get_node=lambda node_id: node))


def test_resident_model_gets_every_slot_ollama_admits():
    """A resident model's weights and KV for all slots are already out of
    `available_gb`; charging them again held a 24 GB Mac to one request."""
    qm = _small_mac([("qwen2.5:7b", 5.22)])
    assert qm._compute_queue_concurrency("s1", "qwen2.5:7b") == 4


def test_resident_model_never_exceeds_the_backend_limit():
    qm = _small_mac([("qwen2.5:7b", 5.22)], available_gb=400.0)
    assert qm._compute_queue_concurrency("s1", "qwen2.5:7b") == 4


def test_cold_and_mlx_models_keep_the_memory_bound():
    """Not resident: the load still has to fit, so memory math applies.
    mlx_lm.server grows its cache per request, so it does too."""
    qm = _small_mac([("mlx:some/model", 5.22)], available_gb=8.47)
    assert qm._compute_queue_concurrency("s1", "mlx:some/model") == 1
    qm = _small_mac([], available_gb=3.0)
    assert qm._compute_queue_concurrency("s1", "qwen2.5:7b") == 1


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

    bad = _make_entry()
    bad_future = await qm.enqueue(bad, boom)
    good = _make_entry()
    await qm.enqueue(good, _sync_process)
    await _settle()
    assert isinstance(bad_future.exception(), RuntimeError)
    q = qm._queues["studio:phi4:14b"]
    assert list(q.in_flight) == [good.request.request_id], "no phantom in-flight entry"
    assert bad.status == RequestStatus.FAILED and q.failed_count == 1
    await qm.shutdown()


async def test_process_fn_raising_is_traced():
    """The worker answers it with a 502; the stream that would have traced the
    attempt never existed (``StreamingProxy._traced``)."""
    from unittest.mock import MagicMock

    from fleet_manager.server.streaming import StreamingProxy

    qm = QueueManager()
    _fixed_concurrency(qm, 1)
    proxy = StreamingProxy(MagicMock())
    proxy._stream_with_tracking = MagicMock(side_effect=RuntimeError("bad request body"))
    proxy._record_trace = MagicMock()
    entry = _make_entry()
    future = await qm.enqueue(entry, proxy.make_process_fn("studio:phi4:14b", qm))
    await _settle()
    assert isinstance(future.exception(), RuntimeError)
    proxy._record_trace.assert_called_once()
    args, kwargs = proxy._record_trace.call_args
    assert args[0] is entry and args[1] == "studio" and args[4] == "failed"
    assert "bad request body" in kwargs["error_message"]
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


# ---------------------------------------------------------------------------
# A load that must evict a busy model gets its turn, after a bounded wait.
#
# Ollama evicts a busy model by waiting for its requests to end, while it
# serves new ones on its fast path.  So when herd kept sending phi4 to the card
# it had to leave, the qwen3-coder:30b load sent there never happened, and 25
# of 27 coder requests timed out (2026-11-20 fleet benchmark, run 4).
# ---------------------------------------------------------------------------


def _node(node_id, resident, *, available_gb=8.0, gpu_total_gb=0.0, sizes=None):
    """A node with ``resident`` loaded (name -> GB, or names at 15.6 GB)."""
    from fleet_manager.models.node import (
        HardwareProfile,
        LoadedModel,
        MemoryMetrics,
        NodeState,
        OllamaMetrics,
    )

    if not isinstance(resident, dict):
        resident = dict.fromkeys(resident, 15.6)
    on_gpu = gpu_total_gb > 0
    return NodeState(
        node_id=node_id,
        hardware=HardwareProfile(node_id=node_id, memory_total_gb=64.0),
        memory=MemoryMetrics(
            total_gb=64.0,
            used_gb=64.0 - available_gb,
            available_gb=available_gb,
            gpu_total_gb=gpu_total_gb,
        ),
        ollama=OllamaMetrics(
            num_parallel=4,
            models_loaded=[
                LoadedModel(name=m, size_gb=gb, size_vram_gb=gb if on_gpu else 0.0)
                for m, gb in resident.items()
            ],
            models_available_sizes={"qwen3-coder:30b": 18.6, **(sizes or {})},
        ),
    )


def _card(*resident):
    """Card m1 (unified memory, 8 GB free) with ``resident`` at 15.6 GB each."""
    from types import SimpleNamespace

    node = _node("m1", resident)
    return node, SimpleNamespace(get_node=lambda node_id: node)


def _recording(calls):
    def process(entry):
        calls.append(entry.request.model)
        return _sync_process(entry)

    return process


def _coder(*, eviction=-40.0, waited=0.0):
    """A coder request routed to card m1 to load there, evicting phi4."""
    load = _make_entry(model="qwen3-coder:30b", node_id="m1")
    load.routing_breakdown = {"eviction": eviction}
    load.enqueued_at -= waited
    return load


async def _busy_card(*resident):
    """Card m1 with ``resident`` loaded, each serving one request."""
    node, registry = _card(*resident)
    qm = QueueManager(registry=registry)
    _fixed_concurrency(qm, 4)
    calls = []
    running = {}
    for m in resident:
        running[m] = _make_entry(model=m, node_id="m1")
        await qm.enqueue(running[m], _recording(calls))
    await _settle()
    return node, qm, calls, running


@pytest.fixture(autouse=True)
def _fast_hold_poll(monkeypatch):
    monkeypatch.setattr(queue_manager, "_LOAD_HOLD_POLL_SECONDS", 0.005)


async def _wait_a_poll():
    await asyncio.sleep(0.03)


async def test_a_load_that_must_evict_a_busy_model_waits_its_turn():
    node, qm, calls, running = await _busy_card("phi4:14b")
    await qm.enqueue(_coder(), _recording(calls))
    await _wait_a_poll()
    assert calls == ["phi4:14b"], "the coder load waits while phi4 is busy"
    assert qm.get_queue_depths()["m1:qwen3-coder:30b"] == 1, "a held request still counts"
    qm.mark_completed("m1:phi4:14b", running["phi4:14b"])  # phi4 goes idle
    await _wait_a_poll()
    assert calls[-1] == "qwen3-coder:30b"
    await qm.shutdown()


async def test_the_wait_is_bounded():
    node, qm, calls, running = await _busy_card("phi4:14b")
    await qm.enqueue(_coder(waited=_LOAD_WAIT), _recording(calls))
    await _wait_a_poll()
    assert calls[-1] == "qwen3-coder:30b", "phi4 never went idle, and the coder went anyway"
    await qm.shutdown()


async def test_requests_for_a_load_under_way_join_it():
    node, qm, calls, running = await _busy_card("phi4:14b")
    await qm.enqueue(_coder(waited=_LOAD_WAIT), _recording(calls))
    await _settle()
    await qm.enqueue(_coder(), _recording(calls))  # just arrived
    await _wait_a_poll()
    assert calls.count("qwen3-coder:30b") == 2
    await qm.shutdown()


async def test_then_the_busy_model_makes_room():
    node, qm, calls, running = await _busy_card("phi4:14b")
    load = _coder(waited=_LOAD_WAIT)
    await qm.enqueue(load, _recording(calls))
    await _settle()
    await qm.enqueue(_make_entry(model="phi4:14b", node_id="m1"), _recording(calls))
    await _wait_a_poll()
    assert calls == ["phi4:14b", "qwen3-coder:30b"], "no new phi4 while the coder waits for it"
    assert qm.get_queue_depths()["m1:phi4:14b"] == 2, "held requests still count"
    qm.mark_completed("m1:phi4:14b", running["phi4:14b"])  # Ollama can evict phi4 now
    load.last_progress_at = 1.0  # ...and the coder's first token arrives
    await _wait_a_poll()
    assert calls[-1] == "phi4:14b"
    await qm.shutdown()


async def test_the_room_is_given_back_if_the_load_leaves():
    node, qm, calls, running = await _busy_card("phi4:14b")
    load = _coder(waited=_LOAD_WAIT)
    await qm.enqueue(load, _recording(calls))
    await _settle()
    await qm.enqueue(_make_entry(model="phi4:14b", node_id="m1"), _recording(calls))
    await _wait_a_poll()
    assert calls.count("phi4:14b") == 1
    qm.mark_failed("m1:qwen3-coder:30b", load)  # e.g. the client gave up
    await _wait_a_poll()
    assert calls.count("phi4:14b") == 2
    await qm.shutdown()


async def test_a_request_routed_hot_whose_model_was_evicted_since_is_a_load():
    """It was sent to its resident model, which another load evicted before it
    reached the card: loading it back evicts the model that took its place."""
    node, qm, calls, running = await _busy_card("phi4:14b")
    reload = _make_entry(model="qwen3-coder:30b", node_id="m1")
    reload.routing_breakdown = {"thermal": 50.0, "eviction": 0.0}
    await qm.enqueue(reload, _recording(calls))
    await _wait_a_poll()
    assert calls == ["phi4:14b"]
    await qm.shutdown()


@pytest.mark.parametrize(
    "case", ["load_fits", "idle_resident", "load_resident", "other_node", "load_has_output"]
)
async def test_nothing_waits_for_a_load_that_waits_for_nothing(case):
    """Ollama starts these loads at once: holding anything would only delay it."""
    resident = ("phi4:14b",) if case == "idle_resident" else ("phi4:14b", "qwen2.5:14b")
    node, qm, calls, running = await _busy_card(*resident)
    if case == "idle_resident":
        from fleet_manager.models.node import LoadedModel

        node.ollama.models_loaded.append(LoadedModel(name="qwen2.5:14b", size_gb=15.7))
    load = _coder(eviction=0.0 if case == "load_fits" else -40.0, waited=_LOAD_WAIT)
    await qm.enqueue(load, _recording(calls))
    await _settle()
    if case == "load_resident":
        from fleet_manager.models.node import LoadedModel

        node.ollama.models_loaded.append(LoadedModel(name="qwen3-coder:30b", size_gb=18.6))
    if case == "load_has_output":
        load.last_progress_at = 1.0
    target = "m2" if case == "other_node" else "m1"
    await qm.enqueue(_make_entry(model="phi4:14b", node_id=target), _recording(calls))
    await _wait_a_poll()
    assert calls.count("phi4:14b") == 2
    await qm.shutdown()


async def test_a_fresh_load_does_not_wait_beside_an_idle_model():
    node, qm, calls, running = await _busy_card("phi4:14b")
    from fleet_manager.models.node import LoadedModel

    node.ollama.models_loaded.append(LoadedModel(name="qwen2.5:14b", size_gb=15.7))
    await qm.enqueue(_coder(), _recording(calls))
    await _wait_a_poll()
    assert calls[-1] == "qwen3-coder:30b"
    await qm.shutdown()


async def test_the_room_is_bounded(monkeypatch):
    """A load stuck at its node stops holding a healthy resident model's traffic
    after ``_LOAD_ROOM_SECONDS``, not after the stale reaper's ten minutes."""
    node, qm, calls, running = await _busy_card("phi4:14b")
    load = _coder(waited=_LOAD_WAIT)
    await qm.enqueue(load, _recording(calls))
    await _settle()
    await qm.enqueue(_make_entry(model="phi4:14b", node_id="m1"), _recording(calls))
    await _wait_a_poll()
    assert calls.count("phi4:14b") == 1, "held while the load is young"
    load.started_at -= queue_manager._LOAD_ROOM_SECONDS  # still no first token
    await _wait_a_poll()
    assert calls.count("phi4:14b") == 2, "released once the load is more likely stuck"
    await qm.shutdown()


# ---------------------------------------------------------------------------
# should_pin: keep_alive -1 unless it could cancel a waiting load's eviction.
# A request that is itself a load pins its own new runner, or Ollama gives it
# OLLAMA_KEEP_ALIVE (five minutes by default) and herd's model unloads.
# ---------------------------------------------------------------------------


async def test_pins_when_no_load_is_waiting():
    node, registry = _card("phi4:14b")
    qm = QueueManager(registry=registry)
    assert qm.should_pin("m1", "phi4:14b")


async def test_resident_model_is_not_pinned_while_a_load_may_wait():
    node, qm, calls, running = await _busy_card("phi4:14b", "gemma3:12b")
    # phi4's request is at the node with no first token: possibly a load
    assert not qm.should_pin("m1", "gemma3:12b")
    await qm.shutdown()


async def test_a_load_pins_its_own_runner():
    node, qm, calls, running = await _busy_card("phi4:14b")
    assert qm.should_pin("m1", "qwen3-coder:30b"), "not resident and herd never sent it"
    await qm.shutdown()


async def test_absent_from_a_heartbeat_is_not_enough(monkeypatch):
    """Heartbeats lag a load: a model herd finished a request for seconds ago
    may be resident though not listed, and pinning it would re-arm it."""
    node, qm, calls, running = await _busy_card("phi4:14b")
    coder = _coder(eviction=0.0)
    await qm.enqueue(coder, _recording(calls))
    await _settle()
    qm.mark_completed("m1:qwen3-coder:30b", coder)
    assert not qm.should_pin("m1", "qwen3-coder:30b"), "finished just now"
    qm._queues["m1:qwen3-coder:30b"].last_finished_at -= queue_manager._RESIDENCY_LAG_SECONDS + 1
    assert qm.should_pin("m1", "qwen3-coder:30b"), "a heartbeat since would list it"
    await qm.shutdown()


async def test_a_model_producing_tokens_is_resident_whatever_the_heartbeat_says():
    node, qm, calls, running = await _busy_card("phi4:14b")
    coder = _coder(eviction=0.0)
    await qm.enqueue(coder, _recording(calls))
    await _settle()
    coder.last_progress_at = 1.0
    assert not qm.should_pin("m1", "qwen3-coder:30b")
    await qm.shutdown()


async def test_an_embedding_at_the_node_counts_as_activity():
    node, qm, calls, running = await _busy_card("phi4:14b")
    with qm.direct("m1", "nomic-embed-text:latest"):
        assert not qm.should_pin("m1", "nomic-embed-text:latest")
    assert not qm.should_pin("m1", "nomic-embed-text:latest"), "and just finished"
    await qm.shutdown()


# ---------------------------------------------------------------------------
# An idle model makes room for a load only if evicting it frees enough.
# ---------------------------------------------------------------------------


async def _gpu_card(busy_gb, idle):
    """A 24 GB card m1: phi4 (``busy_gb``) serving one request, ``idle`` loaded
    and doing nothing."""
    from types import SimpleNamespace

    node = _node("m1", {"phi4:14b": busy_gb, **idle}, gpu_total_gb=24.0)
    qm = QueueManager(registry=SimpleNamespace(get_node=lambda node_id: node))
    _fixed_concurrency(qm, 4)
    calls = []
    running = _make_entry(model="phi4:14b", node_id="m1")
    await qm.enqueue(running, _recording(calls))
    await _settle()
    return qm, calls, running


async def test_a_small_idle_model_does_not_cancel_the_wait():
    """10 GB free plus a 1 GB idle model is not room for an 18.6 GB load: it
    still has to evict phi4, so it waits its turn."""
    qm, calls, running = await _gpu_card(13.0, {"qwen2.5:0.5b": 1.0})
    await qm.enqueue(_coder(), _recording(calls))
    await _wait_a_poll()
    assert calls == ["phi4:14b"]
    qm.mark_completed("m1:phi4:14b", running)
    await _wait_a_poll()
    assert calls[-1] == "qwen3-coder:30b"
    await qm.shutdown()


async def test_an_idle_model_that_frees_enough_lets_the_load_go():
    qm, calls, running = await _gpu_card(4.0, {"qwen2.5:14b": 10.0})
    await qm.enqueue(_coder(), _recording(calls))
    await _wait_a_poll()
    assert calls[-1] == "qwen3-coder:30b"
    await qm.shutdown()


async def test_a_small_idle_model_does_not_cancel_the_room():
    qm, calls, running = await _gpu_card(13.0, {"qwen2.5:0.5b": 1.0})
    await qm.enqueue(_coder(waited=_LOAD_WAIT), _recording(calls))
    await _settle()
    await qm.enqueue(_make_entry(model="phi4:14b", node_id="m1"), _recording(calls))
    await _wait_a_poll()
    assert calls == ["phi4:14b", "qwen3-coder:30b"], "phi4 makes room for the coder"
    await qm.shutdown()


# ---------------------------------------------------------------------------
# A retry moves the request to the next node's queue (admit), so its load
# counts there and not on the node that failed, inside that node's slots.
# ---------------------------------------------------------------------------


async def _two_cards():
    """m1 and m2, phi4 resident on m1 and gemma3 on m2, each serving one
    request; a coder load in flight on m1, about to be retried on m2."""
    from types import SimpleNamespace

    nodes = {"m1": _node("m1", ["phi4:14b"]), "m2": _node("m2", ["gemma3:12b"])}
    qm = QueueManager(registry=SimpleNamespace(get_node=nodes.get))
    _fixed_concurrency(qm, 4)
    calls = []
    running = {}
    for node_id, m in (("m1", "phi4:14b"), ("m2", "gemma3:12b")):
        running[m] = _make_entry(model=m, node_id=node_id)
        await qm.enqueue(running[m], _recording(calls))
    load = _coder(waited=_LOAD_WAIT)
    await qm.enqueue(load, _recording(calls))
    await _settle()
    for e in running.values():
        e.last_progress_at = 1.0  # producing tokens: not loads
    return qm, calls, load


async def test_retry_moves_the_request_and_its_load_clock():
    qm, calls, load = await _two_cards()
    rid = load.request.request_id
    load.started_at -= 100
    assert await qm.admit(load, "m2:qwen3-coder:30b")
    assert load.assigned_node == "m2"
    assert rid not in qm._queues["m1:qwen3-coder:30b"].in_flight
    assert rid in qm._queues["m2:qwen3-coder:30b"].in_flight
    assert time.time() - load.started_at < 5, "the room counts from the new node"
    depths = qm.get_queue_depths()
    assert depths["m1:qwen3-coder:30b"] == 0 and depths["m2:qwen3-coder:30b"] == 1
    load.assigned_node = "m1"  # not in m1's queue any more
    assert not await qm.admit(load, "m2:qwen3-coder:30b")
    await qm.shutdown()


async def test_retry_moves_the_pin():
    """The node the retry loads on stops pinning its resident model; the node
    that failed pins again."""
    qm, calls, load = await _two_cards()
    assert not qm.should_pin("m1", "phi4:14b") and qm.should_pin("m2", "gemma3:12b")
    await qm.admit(load, "m2:qwen3-coder:30b")
    assert qm.should_pin("m1", "phi4:14b"), "no phantom load on the node that failed"
    assert not qm.should_pin("m2", "gemma3:12b"), "a keep_alive would cancel the eviction"
    await qm.shutdown()


async def test_retry_moves_the_hold():
    """New requests for the resident model are held on the node the retry loads
    on, and no longer on the node that failed."""
    qm, calls, load = await _two_cards()
    await qm.admit(load, "m2:qwen3-coder:30b")
    await qm.enqueue(_make_entry(model="phi4:14b", node_id="m1"), _recording(calls))
    await qm.enqueue(_make_entry(model="gemma3:12b", node_id="m2"), _recording(calls))
    await _wait_a_poll()
    assert calls.count("phi4:14b") == 2, "m1 has no load to make room for"
    assert calls.count("gemma3:12b") == 1, "m2 makes room for the coder"
    qm.mark_failed("m2:qwen3-coder:30b", load)
    await _wait_a_poll()
    assert calls.count("gemma3:12b") == 2
    await qm.shutdown()


async def test_a_moved_request_still_leaves_the_queue_when_its_stream_ends():
    """``_settle_on_exit`` settles it in the queue it is in now, and the failed
    node's worker slot is free at once."""
    qm = QueueManager()
    _fixed_concurrency(qm, 1)
    a, b = _make_entry(node_id="m1"), _make_entry(node_id="m1")
    moved = asyncio.Event()

    async def gen():
        await qm.admit(a, "m2:phi4:14b")
        moved.set()
        yield "chunk"

    fut = await qm.enqueue(a, lambda e: gen())
    await qm.enqueue(b, _sync_process)
    stream = await fut
    async for _ in stream:
        pass
    await _settle()
    assert b.request.request_id in qm._queues["m1:phi4:14b"].in_flight, "m1's slot freed"
    m2 = qm._queues["m2:phi4:14b"]
    assert not m2.in_flight and m2.failed_count == 1, "settled where it ran"
    await qm.shutdown()


async def test_a_stream_handed_over_as_the_client_leaves_still_frees_its_slot():
    """The race in ``dispatched_stream``: the route awaits ``is_disconnected()``
    and the worker hands the stream over during that await, so cancelling the
    future does nothing.  The stream is never started -- and an async
    generator that never started ran no ``finally`` on ``aclose()`` -- so the
    request stayed in flight, holding its slot and counting as a pending load,
    until the reaper ten minutes later."""
    from fleet_manager.server.routes.routing import dispatched_stream

    qm = QueueManager()
    entry = _make_entry()
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def process(e):
        async def gen():
            yield "never read"

        return gen()

    await qm.enqueue(entry, lambda e: process(e))
    await asyncio.sleep(0.05)  # the worker dispatches: the request is in flight
    key = "studio:phi4:14b"
    assert entry.request.request_id in qm._queues[key].in_flight

    # Rebuild the hand-over exactly: the stream arrives during the await.
    stream = qm._settle_on_exit(qm._queues[key], entry, process(entry))

    class Request:
        async def is_disconnected(self):
            future.set_result(stream)
            return True

    assert await dispatched_stream(Request(), future, entry, None, poll_s=0.01) is None
    assert entry.request.request_id not in qm._queues[key].in_flight
    assert entry.request.request_id not in qm._queues[key].slots


class TestCapacityBackOff:
    """A resident model gets every slot Ollama allocated (memory: ``6dbf115``),
    and the capacity learner's back-off caps that as its own rule rather than
    through the memory ceiling, which had made it vanish for resident models."""

    async def _qm(self, mode=None):
        from fleet_manager.models.config import ServerSettings
        from fleet_manager.models.node import CapacityMetrics
        from fleet_manager.server.registry import NodeRegistry
        from fleet_manager.testing.heartbeats import make_heartbeat

        registry = NodeRegistry(ServerSettings())
        hb = make_heartbeat(
            node_id="studio",
            memory_total=24.0,
            memory_used=15.0,
            loaded_models=[("phi4:14b", 9.0)],
            available_models=["phi4:14b", "other:7b"],
            num_parallel=4,
        )
        if mode is not None:
            hb.capacity = CapacityMetrics(mode=mode, ceiling_gb=6.0, availability_score=0.5)
        await registry.update_from_heartbeat(hb)
        return QueueManager(registry=registry, settings=ServerSettings())

    @pytest.mark.parametrize(
        "mode,slots",
        [(None, 4), ("full", 4), ("learned_high", 4), ("learned_medium", 2), ("learned_low", 1)],
    )
    async def test_resident_model_slots_follow_the_learner(self, mode, slots):
        qm = await self._qm(mode)
        assert qm._compute_queue_concurrency("studio", "phi4:14b") == slots

    async def test_a_cold_model_is_capped_by_the_same_rule(self):
        qm = await self._qm("learned_low")
        assert qm._compute_queue_concurrency("studio", "other:7b") == 1


async def test_a_retry_waits_for_a_slot_on_its_new_node():
    """B4: a retried request runs inside the new node's concurrency.  It used to
    be placed straight into ``in_flight``, so a node at its limit ran one more
    request per retry than Ollama admits."""
    qm = QueueManager()
    _fixed_concurrency(qm, 1)
    busy = _make_entry(node_id="m2")
    await qm.enqueue(busy, _sync_process)
    retried = _make_entry(node_id="m1")
    await qm.enqueue(retried, _sync_process)
    await _settle()

    admitted = asyncio.create_task(qm.admit(retried, "m2:phi4:14b"))
    await _settle()
    m2 = qm._queues["m2:phi4:14b"]
    assert not admitted.done(), "m2's only slot is busy"
    assert retried.request.request_id not in m2.in_flight
    assert retried.request.request_id not in qm._queues["m1:phi4:14b"].in_flight

    qm.mark_completed("m2:phi4:14b", busy)
    assert await asyncio.wait_for(admitted, 2)
    assert retried.request.request_id in m2.in_flight
    await qm.shutdown()


class TestPreWarmAsLoad:
    """B2: a pre-warm is a load herd sends itself.  Outside the queue it was
    invisible, so the node kept pinning and feeding the model it would evict,
    and Ollama loaded nothing else there for up to fifteen minutes (run 6)."""

    async def _qm(self, busy: bool):
        from fleet_manager.models.config import ServerSettings
        from fleet_manager.server.registry import NodeRegistry
        from fleet_manager.testing.heartbeats import make_heartbeat

        registry = NodeRegistry(ServerSettings())
        await registry.update_from_heartbeat(
            make_heartbeat(
                node_id="s1",
                memory_total=24.0,
                memory_used=21.0,
                loaded_models=[("qwen2.5:7b", 9.0)],
                available_models=["qwen2.5:7b", "gemma3:12b"],
                num_parallel=4,
            )
        )
        qm = QueueManager(registry=registry, settings=ServerSettings())
        if busy:
            running = _make_entry(model="qwen2.5:7b", node_id="s1")
            qm._queues["s1:qwen2.5:7b"] = queue_manager.DeviceModelQueue(
                node_id="s1", model="qwen2.5:7b"
            )
            running.last_progress_at = time.time()
            qm._queues["s1:qwen2.5:7b"].in_flight[running.request.request_id] = running
        return qm

    def _warm(self, evicting: bool):
        entry = _make_entry(model="gemma3:12b", node_id="s1")
        entry.routing_breakdown = {"eviction": -40.0} if evicting else {"eviction": 0.0}
        return entry

    async def test_while_it_runs_the_node_stops_pinning_what_it_may_evict(self):
        qm = await self._qm(busy=False)
        assert qm.should_pin("s1", "qwen2.5:7b")
        entry = self._warm(evicting=False)
        with qm.loading(entry):
            assert qm.load_pending_on("s1", "qwen2.5:7b")
            assert not qm.should_pin("s1", "qwen2.5:7b")
        assert qm.should_pin("s1", "qwen2.5:7b")
        info = qm.get_queue_info()["s1:gemma3:12b"]
        assert info["completed"] == 0 and info["failed"] == 0, "not a client request"

    async def test_one_that_would_wait_for_a_busy_model_is_not_sent(self):
        qm = await self._qm(busy=True)
        assert not qm.speculative_load_allowed(self._warm(evicting=True))

    async def test_one_that_evicts_nothing_busy_goes(self):
        assert (await self._qm(busy=False)).speculative_load_allowed(self._warm(evicting=True))
        assert (await self._qm(busy=True)).speculative_load_allowed(self._warm(evicting=False))


async def test_free_slots_is_concurrency_less_what_is_already_there():
    qm = QueueManager()
    _fixed_concurrency(qm, 4)
    assert qm.free_slots("m2:phi4:14b") == 4, "an empty node can start four"
    for _ in range(3):
        await qm.enqueue(_make_entry(node_id="m2"), _sync_process)
    await _settle()
    assert qm.free_slots("m2:phi4:14b") == 1
    await qm.shutdown()


async def test_depths_say_which_queues_have_a_load_under_way():
    """QueueDepths.loading: an in-flight request that has produced nothing.
    A request producing tokens, or one only queued, is not a load."""
    qm = QueueManager()
    _fixed_concurrency(qm, 1)
    loading, running, queued = (
        _make_entry(node_id="m1"),
        _make_entry(model="gemma3:12b", node_id="m1"),
        _make_entry(node_id="m1"),
    )
    for e in (loading, running, queued):
        await qm.enqueue(e, _sync_process)
    await _settle()
    running.last_progress_at = time.time()
    depths = qm.get_queue_depths()
    assert depths["m1:phi4:14b"] == 2, "one in flight, one queued behind it"
    assert depths.loading == frozenset({"m1:phi4:14b"})
    loading.last_progress_at = time.time()
    assert qm.get_queue_depths().loading == frozenset()
    await qm.shutdown()
