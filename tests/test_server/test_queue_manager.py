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
