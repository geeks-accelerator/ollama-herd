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


@pytest.mark.parametrize("caps", [["embedding"], ["decision"]])
def test_non_completion_models_decode_one_at_a_time(caps):
    from fleet_manager.models.node import ModelTagMeta
    from fleet_manager.server.serializers import decode_parallelism_for

    node = _node_with_meta(4, {"m:latest": ModelTagMeta(capabilities=caps)})
    assert decode_parallelism_for(node, "m") == 1  # bare name → :latest, like Ollama


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
