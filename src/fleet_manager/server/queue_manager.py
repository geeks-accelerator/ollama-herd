"""Queue Manager — per node:model queues with async workers."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from fleet_manager.models.request import QueueEntry, RequestStatus

logger = logging.getLogger(__name__)


@dataclass
class DeviceModelQueue:
    node_id: str
    model: str
    pending: asyncio.Queue = field(default_factory=asyncio.Queue)
    in_flight: list[QueueEntry] = field(default_factory=list)
    worker_task: asyncio.Task | None = None
    completed_count: int = 0
    failed_count: int = 0


class QueueManager:
    def __init__(self):
        self._queues: dict[str, DeviceModelQueue] = {}
        self._lock = asyncio.Lock()

    def get_queue_depths(self) -> dict[str, int]:
        """Return current depth (pending + in_flight) for all queues."""
        return {
            key: q.pending.qsize() + len(q.in_flight)
            for key, q in self._queues.items()
        }

    def get_queue_info(self) -> dict[str, dict]:
        """Return detailed queue information for the fleet status endpoint."""
        info = {}
        for key, q in self._queues.items():
            info[key] = {
                "node_id": q.node_id,
                "model": q.model,
                "pending": q.pending.qsize(),
                "in_flight": len(q.in_flight),
                "completed": q.completed_count,
                "failed": q.failed_count,
            }
        return info

    async def enqueue(
        self,
        entry: QueueEntry,
        process_fn,
    ) -> asyncio.Future:
        """
        Add a request to the appropriate queue.
        Returns a Future that resolves to an async generator of response chunks.
        """
        queue_key = f"{entry.assigned_node}:{entry.request.model}"

        async with self._lock:
            if queue_key not in self._queues:
                q = DeviceModelQueue(node_id=entry.assigned_node, model=entry.request.model)
                self._queues[queue_key] = q
            else:
                q = self._queues[queue_key]

        loop = asyncio.get_running_loop()
        response_future = loop.create_future()

        await q.pending.put((entry, response_future, process_fn))
        logger.debug(
            f"Enqueued {entry.request.request_id[:8]} to {queue_key} "
            f"(depth={q.pending.qsize() + len(q.in_flight)})"
        )

        # Ensure worker is running
        if q.worker_task is None or q.worker_task.done():
            q.worker_task = asyncio.create_task(self._worker(q))
            logger.debug(f"Started worker for queue {queue_key}")

        return response_future

    async def _worker(self, q: DeviceModelQueue):
        """Worker loop for a single queue."""
        while True:
            try:
                entry, future, process_fn = await asyncio.wait_for(
                    q.pending.get(), timeout=300.0
                )
            except asyncio.TimeoutError:
                logger.debug(f"Queue {q.node_id}:{q.model} idle, stopping worker")
                break

            entry.status = RequestStatus.IN_FLIGHT
            entry.started_at = time.time()
            q.in_flight.append(entry)

            try:
                stream = process_fn(entry)
                if not future.done():
                    future.set_result(stream)
            except Exception as e:
                entry.status = RequestStatus.FAILED
                if not future.done():
                    future.set_exception(e)
                logger.error(f"Queue worker error for {entry.request.request_id}: {e}")

    def mark_completed(self, queue_key: str, entry: QueueEntry):
        """Remove an entry from in-flight and mark completed."""
        if queue_key in self._queues:
            q = self._queues[queue_key]
            entry.status = RequestStatus.COMPLETED
            entry.completed_at = time.time()
            if entry in q.in_flight:
                q.in_flight.remove(entry)
            q.completed_count += 1
            logger.debug(f"Completed {entry.request.request_id[:8]} on {queue_key}")

    def mark_failed(self, queue_key: str, entry: QueueEntry):
        """Remove an entry from in-flight and mark failed."""
        if queue_key in self._queues:
            q = self._queues[queue_key]
            entry.status = RequestStatus.FAILED
            entry.completed_at = time.time()
            if entry in q.in_flight:
                q.in_flight.remove(entry)
            q.failed_count += 1
            logger.warning(f"Failed {entry.request.request_id[:8]} on {queue_key}")

    async def move_pending(self, source_key: str, target_key: str, count: int) -> int:
        """Move up to `count` pending requests from source queue to target queue.
        Returns the number actually moved."""
        async with self._lock:
            if source_key not in self._queues:
                return 0

            source = self._queues[source_key]
            if target_key not in self._queues:
                # Parse node_id and model from queue key
                parts = target_key.split(":", 1)
                if len(parts) != 2:
                    return 0
                self._queues[target_key] = DeviceModelQueue(node_id=parts[0], model=parts[1])
            target = self._queues[target_key]

        moved = 0
        temp = []
        # Drain pending items from source
        while not source.pending.empty() and moved < count:
            try:
                item = source.pending.get_nowait()
                entry, future, process_fn = item
                # Update the entry's assigned node
                new_node = target.node_id
                entry.assigned_node = new_node
                await target.pending.put((entry, future, process_fn))
                moved += 1
            except asyncio.QueueEmpty:
                break

        # Ensure target worker is running
        if moved > 0 and (target.worker_task is None or target.worker_task.done()):
            target.worker_task = asyncio.create_task(self._worker(target))

        return moved

    async def shutdown(self):
        """Cancel all worker tasks."""
        for q in self._queues.values():
            if q.worker_task and not q.worker_task.done():
                q.worker_task.cancel()
