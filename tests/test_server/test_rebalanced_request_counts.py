"""A request the rebalancer moved is counted on the queue it ran in.

Run 6: herd's dashboard showed "6 failed" on one Mac for a batch whose 96
requests all succeeded.  The rebalancer had moved six pending requests from s1
to s2 (``move_pending`` updates ``assigned_node``), but the worker's process
function still held s1's queue key from enqueue time: each request ran on s2,
was booked completed on s1, and the exit guard then found it unsettled in s2's
in-flight list and booked it failed there.
"""

from __future__ import annotations

import asyncio

import pytest

from fleet_manager.models.config import ServerSettings
from fleet_manager.models.request import InferenceRequest, QueueEntry
from fleet_manager.server.queue_manager import QueueManager
from fleet_manager.server.registry import NodeRegistry
from fleet_manager.server.streaming import StreamingProxy

MODEL = "qwen2.5:1.5b"


def _entry(node_id: str) -> QueueEntry:
    return QueueEntry(
        request=InferenceRequest(model=MODEL, messages=[{"role": "user", "content": "hi"}]),
        assigned_node=node_id,
    )


@pytest.mark.asyncio
async def test_a_moved_request_is_completed_on_its_new_queue_only():
    qm = QueueManager()
    proxy = StreamingProxy(NodeRegistry(ServerSettings()))
    release = asyncio.Event()
    ran_on: list[str] = []

    async def tracking(entry, queue_key, queue_manager):
        # What the real _stream_with_tracking does at the end of a stream.
        ran_on.append(entry.assigned_node)
        if entry.request.request_id == first.request.request_id:
            await release.wait()
        yield "chunk"
        queue_manager.mark_completed(queue_key, entry, latency_ms=1.0)

    proxy._stream_with_tracking = tracking
    first, second = _entry("s1"), _entry("s1")
    fn = proxy.make_process_fn(f"s1:{MODEL}", qm)

    async def consume(fut):
        async for _ in await fut:
            pass

    t1 = asyncio.create_task(consume(await qm.enqueue(first, fn)))
    await asyncio.sleep(0.05)  # the only s1 worker is now busy with `first`
    t2_future = await qm.enqueue(second, fn)
    assert qm._queues[f"s1:{MODEL}"].pending.qsize() == 1

    assert await qm.move_pending(f"s1:{MODEL}", f"s2:{MODEL}", 1) == 1
    await consume(t2_future)
    release.set()
    await t1

    info = qm.get_queue_info()
    assert ran_on == ["s1", "s2"]
    assert info[f"s2:{MODEL}"]["completed"] == 1
    assert info[f"s2:{MODEL}"]["failed"] == 0
    assert info[f"s1:{MODEL}"]["completed"] == 1  # `first`, not `second` as well
    assert info[f"s1:{MODEL}"]["failed"] == 0
    await qm.shutdown()
