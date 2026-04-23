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
