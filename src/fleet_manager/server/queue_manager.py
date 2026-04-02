"""Queue Manager — per node:model queues with dynamic concurrent workers."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from fleet_manager.models.request import QueueEntry, RequestStatus

logger = logging.getLogger(__name__)

# Zombie reaper event tracking for health visibility
_reaper_events: list[dict] = []


def get_reaper_events(hours: float = 24) -> list[dict]:
    """Return zombie reaper events from the last N hours."""
    cutoff = time.time() - (hours * 3600)
    return [e for e in _reaper_events if e["timestamp"] >= cutoff]


# Estimated KV cache memory per concurrent request (GB).
# Conservative: large models need more, small models less, but 2GB is a
# reasonable middle ground that prevents over-subscription.
_KV_CACHE_PER_REQUEST_GB = 2.0

# Bounds for auto-calculated concurrency per queue.
_MIN_CONCURRENCY = 1
_MAX_CONCURRENCY = 8

# In-flight entries older than this are considered stale/zombied (seconds).
# Ollama's read timeout is 600s; add headroom for slow generation.
_STALE_IN_FLIGHT_SECONDS = 900  # 15 minutes

# How often to run the stale reaper (seconds).
_REAPER_INTERVAL_SECONDS = 60


def compute_concurrency(available_memory_gb: float, model_size_gb: float) -> int:
    """Calculate how many concurrent requests a node can handle for a model.

    Uses the memory headroom after the model is loaded divided by an estimated
    per-request KV cache cost.  Clamped to [1, 8].
    """
    headroom = available_memory_gb - model_size_gb
    if headroom <= 0:
        return _MIN_CONCURRENCY
    slots = int(headroom / _KV_CACHE_PER_REQUEST_GB)
    return max(_MIN_CONCURRENCY, min(_MAX_CONCURRENCY, slots))


@dataclass
class DeviceModelQueue:
    node_id: str
    model: str
    pending: asyncio.Queue = field(default_factory=asyncio.Queue)
    in_flight: dict[str, QueueEntry] = field(default_factory=dict)  # keyed by request_id
    worker_tasks: list[asyncio.Task] = field(default_factory=list)
    concurrency: int = _MIN_CONCURRENCY
    completed_count: int = 0
    failed_count: int = 0


class QueueManager:
    def __init__(self, registry=None, settings=None):
        self._queues: dict[str, DeviceModelQueue] = {}
        self._lock = asyncio.Lock()
        self._registry = registry
        self._settings = settings
        self._stale_timeout = (
            settings.stale_timeout if settings and hasattr(settings, "stale_timeout")
            else _STALE_IN_FLIGHT_SECONDS
        )
        self._reaper_task: asyncio.Task | None = None

    def start_reaper(self):
        """Start the background stale in-flight reaper."""
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._reap_stale_in_flight())

    async def _reap_stale_in_flight(self):
        """Periodically remove in-flight entries that have been stuck too long."""
        while True:
            try:
                await asyncio.sleep(_REAPER_INTERVAL_SECONDS)
                now = time.time()
                for key, q in list(self._queues.items()):
                    stale = [
                        (rid, e) for rid, e in q.in_flight.items()
                        if e.started_at and (now - e.started_at) > self._stale_timeout
                    ]
                    for rid, entry in stale:
                        del q.in_flight[rid]
                        entry.status = RequestStatus.FAILED
                        entry.completed_at = now
                        q.failed_count += 1
                        age = int(now - entry.started_at)
                        _reaper_events.append({
                            "timestamp": now,
                            "request_id": entry.request.request_id,
                            "queue_key": key,
                            "stuck_seconds": age,
                        })
                        if len(_reaper_events) > 100:
                            _reaper_events.pop(0)
                        logger.warning(
                            f"Reaped stale in-flight {entry.request.request_id[:8]} "
                            f"from {key} (stuck for {age}s)"
                        )
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("Error in stale in-flight reaper")

    def get_queue_depths(self) -> dict[str, int]:
        """Return current depth (pending + in_flight) for all queues."""
        return {key: q.pending.qsize() + len(q.in_flight) for key, q in self._queues.items()}

    def get_queue_info(self) -> dict[str, dict]:
        """Return detailed queue information for the fleet status endpoint."""
        info = {}
        for key, q in self._queues.items():
            # Infer request type from in-flight entries or model knowledge
            request_type = "text"
            if q.in_flight:
                first_entry = next(iter(q.in_flight.values()))
                request_type = getattr(first_entry.request, "request_type", "text")
            else:
                from fleet_manager.server.model_knowledge import ModelCategory, classify_model

                category = classify_model(q.model)
                if category == ModelCategory.IMAGE:
                    request_type = "image"
                elif "asr" in q.model or "whisper" in q.model:
                    request_type = "stt"
                elif "embed" in q.model:
                    request_type = "embed"
            info[key] = {
                "node_id": q.node_id,
                "model": q.model,
                "pending": q.pending.qsize(),
                "in_flight": len(q.in_flight),
                "completed": q.completed_count,
                "failed": q.failed_count,
                "concurrency": q.concurrency,
                "request_type": request_type,
            }
        return info

    def _compute_queue_concurrency(self, node_id: str, model: str) -> int:
        """Determine concurrency for a queue based on live node metrics."""
        if self._registry is None:
            return _MIN_CONCURRENCY

        node = self._registry.get_node(node_id)
        if node is None or node.memory is None or node.ollama is None:
            return _MIN_CONCURRENCY

        available_gb = node.memory.available_gb

        # Find the model's loaded size, fall back to 0 (small model on disk)
        model_size_gb = 0.0
        for m in node.ollama.models_loaded:
            if m.name == model:
                model_size_gb = m.size_gb
                break

        # If capacity learning is active, respect the ceiling
        if node.capacity and node.capacity.ceiling_gb > 0:
            available_gb = min(available_gb, node.capacity.ceiling_gb)

        concurrency = compute_concurrency(available_gb, model_size_gb)
        return concurrency

    def _ensure_workers(self, q: DeviceModelQueue, queue_key: str):
        """Ensure the right number of workers are running for a queue."""
        # Recalculate concurrency from live node data
        target = self._compute_queue_concurrency(q.node_id, q.model)
        q.concurrency = target

        # Clean up finished workers
        q.worker_tasks = [t for t in q.worker_tasks if not t.done()]

        # Spawn more workers if needed
        while len(q.worker_tasks) < target:
            worker_id = len(q.worker_tasks)
            task = asyncio.create_task(self._worker(q, worker_id))
            q.worker_tasks.append(task)

        if target > 1:
            logger.debug(f"Queue {queue_key}: {len(q.worker_tasks)} workers (target={target})")

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

        # Ensure correct number of workers are running
        self._ensure_workers(q, queue_key)

        return response_future

    async def _worker(self, q: DeviceModelQueue, worker_id: int = 0):
        """Worker loop for a single queue."""
        while True:
            try:
                entry, future, process_fn = await asyncio.wait_for(q.pending.get(), timeout=300.0)
            except TimeoutError:
                logger.debug(f"Queue {q.node_id}:{q.model} worker {worker_id} idle, stopping")
                break

            entry.status = RequestStatus.IN_FLIGHT
            entry.started_at = time.time()
            q.in_flight[entry.request.request_id] = entry

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
            q.in_flight.pop(entry.request.request_id, None)
            q.completed_count += 1
            logger.debug(f"Completed {entry.request.request_id[:8]} on {queue_key}")

    def mark_failed(self, queue_key: str, entry: QueueEntry):
        """Remove an entry from in-flight and mark failed."""
        if queue_key in self._queues:
            q = self._queues[queue_key]
            entry.status = RequestStatus.FAILED
            entry.completed_at = time.time()
            q.in_flight.pop(entry.request.request_id, None)
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

        # Ensure target workers are running
        if moved > 0:
            self._ensure_workers(target, target_key)

        return moved

    async def shutdown(self):
        """Cancel all worker tasks and the reaper."""
        if self._reaper_task and not self._reaper_task.done():
            self._reaper_task.cancel()
        for q in self._queues.values():
            for task in q.worker_tasks:
                if not task.done():
                    task.cancel()
