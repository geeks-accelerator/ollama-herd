"""Queue Manager — per node:model queues with dynamic concurrent workers."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from fleet_manager.models.request import QueueEntry, RequestStatus
from fleet_manager.server.serializers import decode_parallelism_for

logger = logging.getLogger(__name__)

# Zombie reaper event tracking for health visibility
_reaper_events: list[dict] = []


class ClientConcurrencyExceeded(Exception):
    """Raised by :meth:`QueueManager.enqueue` when a client is over its
    per-client in-flight cap.  Carries ``retry_after`` so the route can return
    ``429`` + ``Retry-After`` instead of piling the request onto the queue."""

    def __init__(self, client_ip: str, limit: int, retry_after: int):
        self.client_ip = client_ip
        self.limit = limit
        self.retry_after = retry_after
        super().__init__(
            f"client {client_ip or '<anon>'} exceeded max in-flight={limit}"
        )


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

# Fallback only: an in-flight entry that has made no PROGRESS for this long is
# considered wedged.  `ServerSettings.stale_timeout` always wins when present,
# which it is, so this value was dead code -- and it read 900 with a "15 minutes"
# comment while the effective threshold was 600.  Kept aligned so the two cannot
# disagree again.
_STALE_NO_PROGRESS_SECONDS = 600.0

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
    # Set when the request leaves this queue (completed, failed, reaped).  The
    # worker that dispatched it waits on this, so it holds its slot for the
    # request's whole duration — that is what makes `concurrency` a real limit.
    slots: dict[str, asyncio.Event] = field(default_factory=dict)  # keyed by request_id
    worker_tasks: list[asyncio.Task] = field(default_factory=list)
    concurrency: int = _MIN_CONCURRENCY
    completed_count: int = 0
    failed_count: int = 0
    # Running sums for dashboard averages — same lifecycle as the counts
    # above (reset on restart, no persistence).  Only incremented on
    # successful completions where stats are available; ``stats_samples``
    # is the denominator for averages and may be ≤ ``completed_count`` if a
    # completion didn't yield token counts (e.g. non-streaming image gen).
    total_latency_ms: float = 0.0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    stats_samples: int = 0


class QueueManager:
    def __init__(self, registry=None, settings=None):
        self._queues: dict[str, DeviceModelQueue] = {}
        self._lock = asyncio.Lock()
        self._registry = registry
        self._settings = settings
        self._stale_timeout = (
            settings.stale_timeout if settings and hasattr(settings, "stale_timeout")
            else _STALE_NO_PROGRESS_SECONDS
        )
        self._reaper_task: asyncio.Task | None = None
        # Per-client concurrency accounting.  ``_client_in_flight`` counts
        # reserved slots per client IP; ``_counted_requests`` makes release
        # idempotent (a request is released exactly once no matter which
        # terminal path — complete / fail / reap — fires).  All ops are
        # sync + await-free, so they're atomic under asyncio without a lock.
        self._client_in_flight: dict[str, int] = {}
        self._counted_requests: set[str] = set()

    def _acquire_client(self, entry: QueueEntry) -> None:
        """Reserve a client-concurrency slot, or raise ClientConcurrencyExceeded.

        No-op when the cap is 0 (disabled, default) or the caller is anonymous
        (no IP to bound).  Check-and-increment is a single await-free sequence,
        so it's atomic under asyncio's single-threaded scheduling.
        """
        limit = getattr(self._settings, "client_max_in_flight", 0) if self._settings else 0
        if limit <= 0:
            return
        ip = entry.request.client_ip or ""
        if not ip:
            return
        if self._client_in_flight.get(ip, 0) >= limit:
            retry_after = getattr(self._settings, "client_concurrency_retry_after", 2)
            raise ClientConcurrencyExceeded(ip, limit, retry_after)
        self._client_in_flight[ip] = self._client_in_flight.get(ip, 0) + 1
        self._counted_requests.add(entry.request.request_id)

    def _release_client(self, entry: QueueEntry) -> None:
        """Release a client-concurrency slot.  Idempotent per request."""
        rid = entry.request.request_id
        if rid not in self._counted_requests:
            return
        self._counted_requests.discard(rid)
        ip = entry.request.client_ip or ""
        remaining = self._client_in_flight.get(ip, 0) - 1
        if remaining > 0:
            self._client_in_flight[ip] = remaining
        else:
            self._client_in_flight.pop(ip, None)

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
                    # Stale means "produced nothing recently", NOT "has been
                    # running a long time".  The old test was
                    # `now - started_at > timeout`, which cannot distinguish a
                    # slow stream from a dead one -- and on a saturated box the
                    # two are easy to confuse: gpt-oss decode fell 76 -> 24
                    # tok/s under load here, so the same 8,000-token reply went
                    # from ~106s to ~330s of entirely healthy work.  On
                    # 2026-10-06 that reaped the slot of a request still
                    # mid-stream, which then completed normally with 9,610
                    # tokens, releasing a concurrency slot the request still
                    # held and recording a failure the trace store denied.
                    #
                    # `last_progress_at` falls back to `started_at` so a request
                    # that has produced *nothing* is still reaped on age: that is
                    # the genuine zombie this exists for -- a stream that was
                    # never consumed, so no mark_* will ever run.
                    stale = [
                        (rid, e)
                        for rid, e in q.in_flight.items()
                        if (e.last_progress_at or e.started_at)
                        and (now - (e.last_progress_at or e.started_at))
                        > self._stale_timeout
                    ]
                    for rid, entry in stale:
                        del q.in_flight[rid]
                        # The safety net for a request whose stream was never
                        # consumed (so no mark_* ever runs): without this the
                        # worker holding its slot would wait forever.
                        self._release_slot(q, rid)
                        self._release_client(entry)
                        entry.status = RequestStatus.FAILED
                        entry.completed_at = now
                        q.failed_count += 1
                        last = entry.last_progress_at or entry.started_at
                        idle = int(now - last)
                        age = int(now - entry.started_at) if entry.started_at else idle
                        _reaper_events.append({
                            "timestamp": now,
                            "request_id": entry.request.request_id,
                            "queue_key": key,
                            "stuck_seconds": idle,
                            # Both, because they differ and the difference is the
                            # diagnosis: idle ~= age means it never produced
                            # anything; idle << age means it stalled mid-stream.
                            "age_seconds": age,
                            "produced_output": entry.last_progress_at is not None,
                        })
                        if len(_reaper_events) > 100:
                            _reaper_events.pop(0)
                        logger.warning(
                            f"Reaped stale in-flight {entry.request.request_id[:8]} "
                            f"from {key} (no output for {idle}s, age {age}s, "
                            f"produced_output={entry.last_progress_at is not None})"
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
            # Running averages since process start.  ``stats_samples`` is the
            # honest denominator — may be below completed_count when some
            # completions didn't carry tokens (e.g. image gen).  Expose it
            # alongside the averages so the dashboard can decide whether to
            # render or suppress (e.g. "—" when samples == 0).
            samples = q.stats_samples
            avg_latency_ms = (q.total_latency_ms / samples) if samples else 0.0
            avg_prompt_tokens = (q.total_prompt_tokens / samples) if samples else 0.0
            avg_completion_tokens = (q.total_completion_tokens / samples) if samples else 0.0
            info[key] = {
                "node_id": q.node_id,
                "model": q.model,
                "pending": q.pending.qsize(),
                "in_flight": len(q.in_flight),
                "completed": q.completed_count,
                "failed": q.failed_count,
                "concurrency": q.concurrency,
                "request_type": request_type,
                "avg_latency_ms": round(avg_latency_ms, 1),
                "avg_prompt_tokens": round(avg_prompt_tokens, 1),
                "avg_completion_tokens": round(avg_completion_tokens, 1),
                "stats_samples": samples,
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

        # Never exceed what the backend will actually decode concurrently.
        # `compute_concurrency` answers "how many KV caches fit in RAM?", which
        # on a large-memory node is always the ceiling — but the binding limit
        # is Ollama's own admission cap.  Workers beyond it don't decode; they
        # block inside Ollama, invisible to the queue that is supposed to be
        # managing them.
        backend_limit = decode_parallelism_for(node, model)
        if backend_limit > 0:
            concurrency = min(concurrency, backend_limit)
        return max(_MIN_CONCURRENCY, concurrency)

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

        Raises :class:`ClientConcurrencyExceeded` if the caller is already at
        its per-client in-flight cap — the route turns that into 429 rather
        than queueing the request (backpressure, not amplification).
        """
        # Reserve the client-concurrency slot first — reject fast, before any
        # queue/worker work, so an over-cap caller can't grow the backlog.
        self._acquire_client(entry)

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
        """Worker loop for a single queue: one request at a time, start to finish.

        ``process_fn`` returns an *unconsumed* stream; the request only reaches
        the backend as the route reads it.  So the worker can't treat handing
        the stream off as "done" — it holds its slot until the request leaves
        this queue (``mark_completed`` / ``mark_failed`` / the reaper).  Before
        this, a worker handed off and immediately took the next request, so one
        worker dispatched the whole queue and ``concurrency`` bounded nothing.
        """
        me = asyncio.current_task()
        while True:
            # Retire if the limit dropped below the live worker count (e.g. node
            # metadata arrived after this queue was created and says the model
            # is serial).  Rank among live workers is unique and stable, so
            # exactly the excess retire — never all of them at once.
            live = [t for t in q.worker_tasks if not t.done()]
            if me in live and live.index(me) >= q.concurrency:
                logger.debug(f"Queue {q.node_id}:{q.model} worker {worker_id} retiring")
                break

            try:
                entry, future, process_fn = await asyncio.wait_for(q.pending.get(), timeout=300.0)
            except TimeoutError:
                logger.debug(f"Queue {q.node_id}:{q.model} worker {worker_id} idle, stopping")
                break

            if future.cancelled():
                # The caller went away while queued (a client disconnect cancels
                # the route awaiting this future).  Don't start work nobody will
                # read — its stream would never be consumed, so no mark_* would
                # ever free the slot.
                self._release_client(entry)
                continue

            rid = entry.request.request_id
            entry.status = RequestStatus.IN_FLIGHT
            entry.started_at = time.time()
            q.in_flight[rid] = entry
            released = asyncio.Event()
            q.slots[rid] = released

            try:
                stream = process_fn(entry)
                if hasattr(stream, "__aiter__"):
                    stream = self._settle_on_exit(q, entry, stream)
                if not future.done():
                    future.set_result(stream)
            except Exception as e:
                entry.status = RequestStatus.FAILED
                if not future.done():
                    future.set_exception(e)
                logger.error(f"Queue worker error for {rid}: {e}")
                q.slots.pop(rid, None)
                continue  # nothing was dispatched, so nothing will release it

            # Drop our references to the stream before waiting.  The future's
            # result IS the stream, and a stream is only finalized -- its
            # GeneratorExit handler calls mark_failed, which releases this very
            # slot -- once nothing references it.  A streaming client that
            # disconnects mid-response leaves its stream suspended at a yield;
            # if this frame still held it, the slot would wait for the reaper
            # (~11 min) and the model's queue would freeze.  Found live.
            stream = future = None

            # Belt and braces for a request that never leaves the queue at
            # all.  This MUST use the same no-progress rule as the reaper.
            #
            # It used to be a single `wait_for(..., stale_timeout + interval)`,
            # sized to fire 60 s *after* the reaper so the reaper would always
            # act first and this path stayed dead code.  Making the reaper
            # progress-aware (2026-10-06) removed its false positives and
            # promoted this one: on 10-06 20:42 and 10-07 02:42 it released the
            # slots of two requests still mid-stream, which went on to return
            # 6,557 and 12,809 tokens.  Fixing one timeout and leaving the other
            # just moved the bug 60 s later.
            #
            # So: wake on the reaper's cadence, and only give up when the entry
            # has produced nothing for `stale_timeout`.  `last_progress_at`
            # falls back to `started_at`, so a request that never produced
            # anything is still released on age -- the genuine stuck case.
            try:
                while True:
                    try:
                        await asyncio.wait_for(
                            released.wait(), timeout=_REAPER_INTERVAL_SECONDS
                        )
                        break
                    except TimeoutError:
                        last = (
                            entry.last_progress_at
                            or entry.started_at
                            or time.time()
                        )
                        idle = time.time() - last
                        if idle > self._stale_timeout:
                            logger.warning(
                                f"Queue {q.node_id}:{q.model} worker {worker_id}: "
                                f"{rid[:8]} produced nothing for {int(idle)}s "
                                f"— releasing the slot"
                            )
                            break
            finally:
                q.slots.pop(rid, None)

    def mark_completed(
        self,
        queue_key: str,
        entry: QueueEntry,
        *,
        latency_ms: float | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
    ):
        """Remove an entry from in-flight and mark completed.

        Optional ``latency_ms``/``prompt_tokens``/``completion_tokens`` feed
        the per-queue running averages surfaced on the dashboard.  Pass all
        three when available — partial updates are accepted (missing values
        treated as 0 for that sample) to keep the happy path simple for
        callers that don't always have token counts (e.g. image generation).
        """
        self._release_client(entry)
        if queue_key in self._queues:
            q = self._queues[queue_key]
            entry.status = RequestStatus.COMPLETED
            entry.completed_at = time.time()
            q.in_flight.pop(entry.request.request_id, None)
            self._release_slot(q, entry.request.request_id)
            q.completed_count += 1
            # Accumulate stats only when at least one value is provided,
            # otherwise the averages would drift to zero for completions
            # that never reported tokens (e.g. image generation).
            if (
                latency_ms is not None
                or prompt_tokens is not None
                or completion_tokens is not None
            ):
                q.total_latency_ms += latency_ms or 0.0
                q.total_prompt_tokens += prompt_tokens or 0
                q.total_completion_tokens += completion_tokens or 0
                q.stats_samples += 1
            logger.debug(f"Completed {entry.request.request_id[:8]} on {queue_key}")

    async def _settle_on_exit(self, q: DeviceModelQueue, entry: QueueEntry, stream):
        """Pass ``stream`` through, guaranteeing the request leaves this queue.

        The process_fns settle requests themselves (mark_completed/mark_failed)
        on completion, errors and GeneratorExit -- but not on CancelledError,
        which is how Starlette ends a streaming response when the client
        disconnects: it cancels the task, and mid-generation that lands while
        the stream awaits the backend.  Neither ``except Exception`` nor
        ``except GeneratorExit`` catches it, so the request never left
        ``in_flight``, and with workers holding their slot the model's queue
        froze until the reaper (measured: 641 s).  One guarantee here covers
        every process_fn, present and future, instead of each re-learning it.
        """
        rid = entry.request.request_id
        try:
            async for chunk in stream:
                yield chunk
        finally:
            if rid in q.in_flight:  # nothing settled it
                self.mark_failed(f"{q.node_id}:{q.model}", entry)
            self._release_slot(q, rid)

    @staticmethod
    def _release_slot(q: DeviceModelQueue, request_id: str) -> None:
        """Free the worker holding ``request_id`` — every exit path calls this."""
        event = q.slots.pop(request_id, None)
        if event is not None:
            event.set()

    def mark_failed(self, queue_key: str, entry: QueueEntry):
        """Remove an entry from in-flight and mark failed."""
        self._release_client(entry)
        if queue_key in self._queues:
            q = self._queues[queue_key]
            entry.status = RequestStatus.FAILED
            entry.completed_at = time.time()
            q.in_flight.pop(entry.request.request_id, None)
            self._release_slot(q, entry.request.request_id)
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
